"""Content-addressed cache for the same-model State Class Graph (SCG).

The SCG of a Time Petri Net is a pure function of the net: the observable
horizon ``L``, the horizon ``H`` and the analysis ``mode`` only enter later
(synchronizer / temporal expansion).  A benchmark therefore reconstructs the
same SCG many times when it sweeps ``L``/``H``/``mode`` for one fixed model.
This module stores that construction result on disk and reuses it.

Correctness contract
--------------------
* The cache key binds the full model hash, the executed source SHA, the SHA of
  ``src/scg/builder.py``, the strong-time semantics version, the Python
  major/minor version and the serialization format version.  A key is only
  ever read when every component matches.
* Serialization is a line-oriented canonical JSON container
  (``json-fraction-v2``): one header line, one line per state class, one line
  for the edge list and a trailing integrity line.  Rationals are stored as
  ``"num/den"`` strings and the two infinities as ``"inf"``/``"-inf"``.  No
  pickle is used, so a cache file can never execute code.
* The trailing line carries the SHA-256 of every preceding byte.  Any
  truncated/corrupted/mismatched file is treated as a cache *miss* (with a
  recorded reason), never as an error.
* Encoding is streamed class by class, so a very large SCG does not require a
  second in-memory copy of the whole graph (the monolithic encoder roughly
  doubled peak RSS on 50k-class SCGs).
* Writes are atomic (temporary file + ``os.replace``) so a killed worker can
  never leave a half-written entry behind.

Cache metadata is intentionally kept *outside* the algorithm counters: the
caller records ``runtime_scg_build_s`` / ``runtime_scg_load_s`` separately from
``runtime_scg`` so that a cache hit can never masquerade as construction work.
"""
from __future__ import annotations

import hashlib
import io
import json
import math
import os
import sys
import time
from fractions import Fraction
from pathlib import Path
from typing import Dict, Optional, Tuple

from .builder import DBM, SCG, StateClass

CACHE_FORMAT = "v6-scg-cache"
CACHE_FORMAT_VERSION = 2
SERIALIZATION = "json-fraction-v2"
SEMANTICS_VERSION = "v6-strong-time-1"

#: Entries larger than this are not written (they are recorded as ``miss`` with
#: ``store_ok=False``).  This is a resource guard, not a semantic filter.
MAX_ENTRY_BYTES = 64 * 1024 * 1024

#: Default location when ``SCG_CACHE_DIR`` is not provided.  Only used by
#: ``SCGCache.from_environment()``; an unset variable disables the cache.
ENV_CACHE_DIR = "SCG_CACHE_DIR"


class EntryTooLarge(ValueError):
    """Raised while streaming when an entry would exceed ``MAX_ENTRY_BYTES``."""


def repository_root() -> Path:
    """Return the benchmark repository root (``.../src/scg/cache.py`` -> root)."""
    return Path(__file__).resolve().parents[2]


def builder_content_sha(root: Optional[Path] = None) -> str:
    """SHA-256 of the SCG builder source, used as a semantic cache component."""
    path = (root or repository_root()) / "src" / "scg" / "builder.py"
    return hashlib.sha256(path.read_bytes()).hexdigest()


def python_major_minor() -> str:
    return f"{sys.version_info.major}.{sys.version_info.minor}"


def build_key_material(
    model_hash: str,
    source_sha: str,
    *,
    scg_builder_sha: Optional[str] = None,
    semantics_version: str = SEMANTICS_VERSION,
    python: Optional[str] = None,
    serialization: str = SERIALIZATION,
    format_version: int = CACHE_FORMAT_VERSION,
) -> Dict[str, object]:
    """Build the canonical cache-key material dict for one model/execution."""
    return {
        "format": CACHE_FORMAT,
        "format_version": int(format_version),
        "model_hash": str(model_hash),
        "source_sha": str(source_sha),
        "scg_builder_sha": scg_builder_sha or builder_content_sha(),
        "semantics_version": semantics_version,
        "python": python or python_major_minor(),
        "serialization": serialization,
    }


def compute_cache_key(key_material: Dict[str, object]) -> str:
    return hashlib.sha256(_canonical_json(key_material).encode("utf-8")).hexdigest()


def _canonical_json(value: object) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"))


def _encode_value(value: object) -> str:
    if isinstance(value, Fraction):
        return f"{value.numerator}/{value.denominator}"
    if isinstance(value, float) and math.isinf(value):
        return "inf" if value > 0 else "-inf"
    if isinstance(value, int):
        return f"{value}/1"
    raise TypeError(f"unsupported DBM value {value!r}")


def _decode_value(value: str) -> object:
    if value == "inf":
        return math.inf
    if value == "-inf":
        return -math.inf
    return Fraction(value)


def _header(scg: SCG, key_material: Dict[str, object], key: str) -> Dict[str, object]:
    return {
        "format": CACHE_FORMAT,
        "format_version": CACHE_FORMAT_VERSION,
        "serializer": SERIALIZATION,
        "semantics_version": SEMANTICS_VERSION,
        "key": key,
        "key_material": key_material,
        "class_count": len(scg.classes),
        "initial_id": int(scg.initial_id),
        "completion_ids": sorted(int(value) for value in scg.completion_ids),
        "has_zero_time_unobservable_cycle": bool(scg.has_zero_time_unobservable_cycle),
        "scg_state_limit_reached": bool(scg.scg_state_limit_reached),
    }


def _class_record(class_id: int, cls: StateClass) -> Dict[str, object]:
    variables = list(cls.dbm._vars_list)
    matrix = [
        [_encode_value(cls.dbm.get(i, j)) for j in variables]
        for i in variables
    ]
    return {
        "id": int(class_id),
        "marking": {name: int(count) for name, count in sorted(cls.marking.items())},
        "enabled": sorted(cls.enabled),
        "dbm": {"vars": variables, "matrix": matrix},
    }


def _decode_class_record(record: Dict[str, object]) -> Tuple[int, StateClass]:
    variables = frozenset(record["dbm"]["vars"])
    matrix = record["dbm"]["matrix"]
    dbm = DBM(variables)
    for i, name_i in enumerate(record["dbm"]["vars"]):
        for j, name_j in enumerate(record["dbm"]["vars"]):
            dbm.set(name_i, name_j, _decode_value(matrix[i][j]))
    marking = {str(name): int(count) for name, count in record["marking"].items()}
    cls = StateClass(
        marking_key=frozenset(marking.items()),
        marking=marking,
        dbm=dbm,
        enabled=frozenset(record["enabled"]),
    )
    return int(record["id"]), cls


class SCGCache:
    """File-backed cache of SCGs, safe to share between runner subprocesses."""

    def __init__(self, directory: Optional[os.PathLike | str], enabled: Optional[bool] = None):
        self.directory = Path(directory) if directory else None
        self.enabled = bool(self.directory) if enabled is None else bool(enabled and self.directory)
        self.stats = {"hits": 0, "misses": 0, "stores": 0, "load_errors": 0, "store_errors": 0}

    # -- construction helpers -------------------------------------------------
    @classmethod
    def from_environment(cls) -> "SCGCache":
        directory = os.environ.get(ENV_CACHE_DIR)
        return cls(directory)

    def path_for_key(self, key: str) -> Path:
        assert self.directory is not None
        return self.directory / f"{key}.json"

    # -- public API -----------------------------------------------------------
    def load(self, key: str) -> Optional[Tuple[SCG, Dict[str, object]]]:
        """Return ``(scg, provenance)`` on a verified hit, else ``None``.

        Every failure mode is a miss with a recorded reason.  A corrupt entry is
        left in place only until the next :meth:`store` replaces it.
        """
        if not self.enabled:
            return None
        provenance: Dict[str, object] = {"cache_status": "miss", "key": key}
        started = time.perf_counter()
        path = self.path_for_key(key)
        try:
            raw = path.read_bytes()
        except FileNotFoundError:
            provenance.update({"reason": "absent", "load_s": time.perf_counter() - started})
            return None
        except OSError as exc:  # pragma: no cover - filesystem level failure
            self.stats["load_errors"] += 1
            provenance.update({"reason": f"read_error: {exc}", "load_s": time.perf_counter() - started})
            return None
        try:
            scg, payload_sha, key_material = _decode_entry(raw, key)
        except Exception as exc:  # noqa: BLE001 - any corruption is a miss
            self.stats["load_errors"] += 1
            provenance.update({
                "cache_status": "miss",
                "reason": f"invalid_entry: {type(exc).__name__}: {exc}",
                "load_s": time.perf_counter() - started,
            })
            return None
        load_s = time.perf_counter() - started
        self.stats["hits"] += 1
        provenance.update({
            "cache_status": "hit",
            "reason": "verified",
            "load_s": load_s,
            "build_s": None,
            "store_s": None,
            "payload_sha256": payload_sha,
            "file_bytes": len(raw),
            "key_material": key_material,
            "serializer": SERIALIZATION,
        })
        return scg, provenance

    def store(
        self,
        key: str,
        key_material: Dict[str, object],
        scg: SCG,
        build_seconds: Optional[float] = None,
    ) -> Dict[str, object]:
        """Serialize and atomically persist one SCG; returns cache provenance."""
        provenance: Dict[str, object] = {
            "cache_status": "miss",
            "key": key,
            "key_material": key_material,
            "store_ok": False,
            "load_s": 0.0,
            "build_s": None if build_seconds is None else float(build_seconds),
            "serializer": SERIALIZATION,
        }
        if not self.enabled:
            provenance["reason"] = "disabled"
            return provenance
        started = time.perf_counter()
        target = self.path_for_key(key)
        temporary = target.with_name(f".{target.name}.{os.getpid()}.tmp")
        try:
            self.directory.mkdir(parents=True, exist_ok=True)  # type: ignore[union-attr]
            size, payload_sha = _write_entry(temporary, scg, key_material, key)
            if size > MAX_ENTRY_BYTES:
                temporary.unlink(missing_ok=True)
                self.stats["store_errors"] += 1
                provenance.update({
                    "reason": f"entry_too_large: {size} > {MAX_ENTRY_BYTES}",
                    "store_s": time.perf_counter() - started,
                    "payload_sha256": None,
                    "file_bytes": None,
                })
                return provenance
            os.replace(temporary, target)
        except Exception as exc:  # noqa: BLE001 - caching must never break a cell
            temporary.unlink(missing_ok=True)
            self.stats["store_errors"] += 1
            reason = (
                f"entry_too_large: {exc}"
                if isinstance(exc, EntryTooLarge)
                else f"store_error: {type(exc).__name__}: {exc}"
            )
            provenance.update({
                "reason": reason,
                "store_s": time.perf_counter() - started,
                "payload_sha256": None,
                "file_bytes": None,
            })
            return provenance
        store_s = time.perf_counter() - started
        self.stats["stores"] += 1
        self.stats["misses"] += 1
        provenance.update({
            "store_ok": True,
            "reason": "stored",
            "store_s": store_s,
            "payload_sha256": payload_sha,
            "file_bytes": size,
        })
        return provenance


def _write_entry(
    path: Path, scg: SCG, key_material: Dict[str, object], key: str
) -> Tuple[int, str]:
    """Stream one cache entry to ``path``; returns (bytes, payload SHA-256)."""
    hasher = hashlib.sha256()
    size = 0
    with path.open("wb") as handle:
        def emit(text: str) -> None:
            nonlocal size
            data = text.encode("utf-8")
            hasher.update(data)
            handle.write(data)
            size += len(data)
            if size > MAX_ENTRY_BYTES:
                raise EntryTooLarge(f"entry exceeds {MAX_ENTRY_BYTES} bytes")

        emit(_canonical_json(_header(scg, key_material, key)) + "\n")
        for class_id in sorted(scg.classes):
            emit(_canonical_json(_class_record(class_id, scg.classes[class_id])) + "\n")
        emit(_canonical_json({"edges": [
            [int(src), str(name), int(dst)] for (src, name, dst) in scg.edges
        ]}) + "\n")
        payload_sha = hasher.hexdigest()
        trailer = {"payload_sha256": payload_sha, "algorithm": "sha256"}
        handle.write(_canonical_json(trailer).encode("utf-8"))
    return size, payload_sha


def _decode_entry(raw: bytes, key: str) -> Tuple[SCG, str, Dict[str, object]]:
    """Parse and verify one cache entry; raises on any inconsistency."""
    if not raw:
        raise ValueError("empty entry")
    trailer_start = raw.rfind(b"\n", 0, len(raw) - 1)
    if trailer_start < 0:
        raise ValueError("missing integrity trailer")
    prefix = raw[: trailer_start + 1]
    trailer = json.loads(raw[trailer_start + 1:])
    payload_sha = hashlib.sha256(prefix).hexdigest()
    if trailer.get("payload_sha256") != payload_sha:
        raise ValueError("payload hash mismatch")
    if trailer.get("algorithm") != "sha256":
        raise ValueError("unexpected integrity algorithm")

    stream = io.BytesIO(prefix)
    header = json.loads(stream.readline())
    if header.get("format") != CACHE_FORMAT:
        raise ValueError("cache: unexpected format tag")
    if int(header.get("format_version", -1)) != CACHE_FORMAT_VERSION:
        raise ValueError("cache: unsupported format version")
    if header.get("serializer") != SERIALIZATION:
        raise ValueError("cache: unsupported serializer")
    if header.get("key") != key:
        raise ValueError("cache key mismatch")
    key_material = header.get("key_material") or {}
    if compute_cache_key(key_material) != key:
        raise ValueError("key material does not hash to key")

    scg = SCG()
    for _ in range(int(header["class_count"])):
        line = stream.readline()
        if not line:
            raise ValueError("truncated class list")
        class_id, cls = _decode_class_record(json.loads(line))
        scg.classes[class_id] = cls
        scg._class_to_id[cls] = class_id
    edge_line = stream.readline()
    if not edge_line:
        raise ValueError("missing edge list")
    edges = json.loads(edge_line)["edges"]
    if stream.readline():
        raise ValueError("trailing data after edge list")

    scg.initial_id = int(header["initial_id"])
    scg.completion_ids = frozenset(int(value) for value in header["completion_ids"])
    scg.has_zero_time_unobservable_cycle = bool(header["has_zero_time_unobservable_cycle"])
    scg.scg_state_limit_reached = bool(header["scg_state_limit_reached"])
    scg.edges = [(int(src), str(name), int(dst)) for (src, name, dst) in edges]
    scg._next_id = (max(scg.classes) + 1) if scg.classes else 0
    if int(header["class_count"]) != len(scg.classes):
        raise ValueError("cache: class count mismatch after decode")
    return scg, payload_sha, key_material
