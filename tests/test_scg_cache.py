"""Differential and integrity tests for the same-model SCG cache.

The correctness requirement is strong: a cold construction, an in-memory
prebuilt SCG and a serialized-then-loaded SCG must produce identical semantic
results, certificates, SCG flags and workload counters.  Only runtime and cache
metadata are allowed to differ.
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from src import scg as scg_package  # noqa: E402,F401
from src.benchmarks.v6_families import generate_v6_model  # noqa: E402
from src.scg import cache as cache_module  # noqa: E402
from src.scg.builder import build_scg  # noqa: E402
from src.scg.cache import (  # noqa: E402
    SCGCache,
    build_key_material,
    compute_cache_key,
)
from src.verifier import verify  # noqa: E402


RUNTIME_PREFIX = "runtime_"


def _models():
    for family, seed in (("F01_sequential_sparse", 11), ("F04_alternative_routes", 23)):
        tpn, default_l, default_h, _ = generate_v6_model(family, seed)
        yield family, tpn, default_l, default_h


def _canonical_result(result):
    """Strip runtime/cache metadata, keep every semantic and workload output."""
    certificates = [
        {
            "phi": tuple(cert.phi),
            "sigma_N": tuple(cert.sigma_N),
            "sigma_F": tuple(cert.sigma_F),
            "overlap": tuple(str(value) for value in cert.overlap),
        }
        for cert in result.get("certificates", [])
    ]
    stats = {
        key: value
        for key, value in result.get("stats", {}).items()
        if not key.startswith(RUNTIME_PREFIX)
    }
    return {
        "result": result.get("result"),
        "inconclusive_reason": result.get("inconclusive_reason"),
        "certificate_mode": result.get("certificate_mode"),
        "enumeration_complete": result.get("enumeration_complete"),
        "certificates": certificates,
        "stats": stats,
    }


class CacheKeyTests(unittest.TestCase):
    def _material(self, **overrides):
        base = build_key_material(
            model_hash="a" * 64,
            source_sha="b" * 40,
            scg_builder_sha="c" * 64,
            python="3.13",
        )
        base.update(overrides)
        return base

    def test_key_is_deterministic(self):
        first = compute_cache_key(self._material())
        second = compute_cache_key(self._material())
        self.assertEqual(first, second)
        self.assertEqual(len(first), 64)

    def test_every_component_changes_the_key(self):
        base = compute_cache_key(self._material())
        variants = {
            "model_hash": "d" * 64,
            "source_sha": "e" * 40,
            "scg_builder_sha": "f" * 64,
            "semantics_version": "other-semantics",
            "python": "3.14",
            "serialization": "other-format",
            "format_version": 3,
        }
        for name, value in variants.items():
            material = self._material()
            material[name] = value
            self.assertNotEqual(compute_cache_key(material), base, name)

    def test_builder_sha_covers_the_builder_file(self):
        import hashlib

        expected = cache_module.builder_content_sha(ROOT)
        manual = hashlib.sha256((ROOT / "src" / "scg" / "builder.py").read_bytes()).hexdigest()
        self.assertEqual(expected, manual)
        self.assertEqual(len(expected), 64)


class EncodingRoundTripTests(unittest.TestCase):
    def test_round_trip_preserves_the_whole_graph(self):
        for family, tpn, _, _ in _models():
            with self.subTest(family=family):
                original = build_scg(tpn)
                material = build_key_material(model_hash=family, source_sha="test")
                key = compute_cache_key(material)
                with tempfile.TemporaryDirectory() as directory:
                    scache = SCGCache(directory)
                    provenance = scache.store(key, material, original)
                    self.assertTrue(provenance["store_ok"], provenance)
                    loaded = scache.load(key)
                    self.assertIsNotNone(loaded)
                    restored, load_provenance = loaded
                    self.assertEqual(load_provenance["cache_status"], "hit")
                self.assertEqual(original.classes, restored.classes)
                self.assertEqual(original.edges, restored.edges)
                self.assertEqual(original.initial_id, restored.initial_id)
                self.assertEqual(original.completion_ids, restored.completion_ids)
                self.assertEqual(
                    original.scg_state_limit_reached, restored.scg_state_limit_reached
                )
                self.assertEqual(
                    original.has_zero_time_unobservable_cycle,
                    restored.has_zero_time_unobservable_cycle,
                )


class DifferentialVerificationTests(unittest.TestCase):
    """Cold build vs in-memory prebuilt vs serialized load must agree exactly."""

    def _check_model(self, family, tpn, default_l, default_h, mode):
        complete_set = mode == "complete"
        cold = verify(tpn, default_l, default_h, complete_set=complete_set)
        in_memory_scg = build_scg(tpn)
        prebuilt = verify(
            tpn,
            default_l,
            default_h,
            complete_set=complete_set,
            prebuilt_scg=in_memory_scg,
        )
        with tempfile.TemporaryDirectory() as directory:
            scache = SCGCache(directory)
            material = build_key_material(model_hash=family, source_sha="test")
            key = compute_cache_key(material)
            built = build_scg(tpn)
            self.assertTrue(scache.store(key, material, built)["store_ok"])
            loaded_scg, _ = scache.load(key)
        restored = verify(
            tpn,
            default_l,
            default_h,
            complete_set=complete_set,
            prebuilt_scg=loaded_scg,
        )
        expected = _canonical_result(cold)
        self.assertEqual(_canonical_result(prebuilt), expected)
        self.assertEqual(_canonical_result(restored), expected)
        # A cached construction must never be reported as construction time.
        self.assertEqual(restored["stats"]["runtime_scg"], 0.0)
        self.assertGreater(cold["stats"]["runtime_scg"], 0.0)

    def test_first_and_complete_modes(self):
        for family, tpn, default_l, default_h in _models():
            for mode in ("first", "complete"):
                with self.subTest(family=family, mode=mode):
                    self._check_model(family, tpn, default_l, default_h, mode)

    def test_externally_measured_build_time_is_attributed_honestly(self):
        family, tpn, default_l, default_h = next(iter(_models()))
        scg = build_scg(tpn)
        measured = 1.25
        result = verify(
            tpn,
            default_l,
            default_h,
            prebuilt_scg=scg,
            scg_build_seconds=measured,
        )
        self.assertEqual(result["stats"]["runtime_scg"], measured)


class CacheFailureModeTests(unittest.TestCase):
    def _cache(self, directory):
        material = build_key_material(model_hash="m" * 64, source_sha="s" * 40)
        return material, compute_cache_key(material), SCGCache(directory)

    def test_missing_entry_is_a_miss(self):
        with tempfile.TemporaryDirectory() as directory:
            material, key, scache = self._cache(directory)
            self.assertIsNone(scache.load(key))
            self.assertEqual(scache.stats["hits"], 0)

    def test_corrupt_entry_is_a_miss_not_an_error(self):
        family, tpn, _, _ = next(iter(_models()))
        with tempfile.TemporaryDirectory() as directory:
            material, key, scache = self._cache(directory)
            self.assertTrue(scache.store(key, material, build_scg(tpn))["store_ok"])
            scache.path_for_key(key).write_bytes(b"{ this is not json")
            self.assertIsNone(scache.load(key))
            self.assertEqual(scache.stats["load_errors"], 1)

    def test_tampered_payload_is_rejected(self):
        family, tpn, _, _ = next(iter(_models()))
        with tempfile.TemporaryDirectory() as directory:
            material, key, scache = self._cache(directory)
            self.assertTrue(scache.store(key, material, build_scg(tpn))["store_ok"])
            path = scache.path_for_key(key)
            raw = bytearray(path.read_bytes())
            # Flip one byte inside the class payload; the integrity trailer no
            # longer matches, so the entry must be rejected rather than used.
            raw[len(raw) // 2] ^= 0x01
            path.write_bytes(bytes(raw))
            self.assertIsNone(scache.load(key))
            self.assertEqual(scache.stats["load_errors"], 1)

    def test_oversized_entry_is_not_written(self):
        family, tpn, _, _ = next(iter(_models()))
        with tempfile.TemporaryDirectory() as directory:
            material, key, scache = self._cache(directory)
            with mock.patch.object(cache_module, "MAX_ENTRY_BYTES", 128):
                provenance = scache.store(key, material, build_scg(tpn))
            self.assertFalse(provenance["store_ok"])
            self.assertIn("entry_too_large", provenance["reason"])
            self.assertIsNone(scache.load(key))

    def test_cache_is_disabled_without_a_directory(self):
        scache = SCGCache.from_environment()
        previous = os.environ.pop(cache_module.ENV_CACHE_DIR, None)
        try:
            scache = SCGCache.from_environment()
            self.assertFalse(scache.enabled)
            self.assertIsNone(scache.load("0" * 64))
        finally:
            if previous is not None:
                os.environ[cache_module.ENV_CACHE_DIR] = previous

    def test_atomic_write_leaves_no_temporary_files(self):
        family, tpn, _, _ = next(iter(_models()))
        with tempfile.TemporaryDirectory() as directory:
            material, key, scache = self._cache(directory)
            self.assertTrue(scache.store(key, material, build_scg(tpn))["store_ok"])
            leftovers = [name for name in os.listdir(directory) if name.endswith(".tmp")]
            self.assertEqual(leftovers, [])
            self.assertEqual(os.listdir(directory), [f"{key}.json"])


class RunnerIntegrationTests(unittest.TestCase):
    """End-to-end: one cell misses, the next identical cell hits and agrees."""

    def _run(self, spec, cache_dir):
        environment = dict(os.environ)
        environment["SCG_CACHE_DIR"] = cache_dir
        environment["EXECUTED_SOURCE_SHA"] = "test-source-sha"
        environment["CHECKPOINT_PATH"] = os.path.join(cache_dir, "checkpoint.json")
        completed = subprocess.run(
            [sys.executable, str(ROOT / "runner" / "run_cell.py"), json.dumps(spec)],
            capture_output=True,
            text=True,
            env=environment,
            timeout=300,
        )
        self.assertEqual(completed.returncode, 0, completed.stderr[-2000:])
        return json.loads(completed.stdout.strip().splitlines()[-1])

    def test_second_cell_is_a_verified_hit(self):
        spec = {
            "tag": "cache-e2e",
            "experiment": "cache_e2e",
            "model": "F01_sequential_sparse",
            "seed": 11,
            "replicate": 0,
            "L": 14,
            "H": 54.0,
            "mode": "first",
            "timeout_s": 120,
            "params": {"v6_family": "F01_sequential_sparse", "seed": 11},
        }
        with tempfile.TemporaryDirectory() as cache_dir:
            first = self._run(spec, cache_dir)
            second = self._run(spec, cache_dir)
        self.assertEqual(first["scg_cache_status"], "miss")
        self.assertEqual(second["scg_cache_status"], "hit")
        self.assertEqual(first["verdict"], second["verdict"])
        self.assertEqual(first["counters"]["scg_classes"], second["counters"]["scg_classes"])
        self.assertGreater(first["runtime_scg_build_s"], 0.0)
        self.assertEqual(second["runtime_scg_build_s"], 0.0)
        self.assertIsNotNone(second["runtime_scg_load_s"])
        self.assertEqual(second["runtime_scg_load_s"], second["scg_cache_provenance"]["load_s"])
        for key, value in first["counters"].items():
            if key.startswith(RUNTIME_PREFIX):
                continue
            self.assertEqual(value, second["counters"][key], key)
        self.assertEqual(first["scg_cache_key"], second["scg_cache_key"])


if __name__ == "__main__":
    unittest.main()
