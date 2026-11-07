#!/usr/bin/env python3
"""Modal App for the v6 scalability benchmark (formal 8,280-cell matrix).

One Modal CPU container executes one frozen shard of tasks.  The container
spawns ``runner/run_cell.py`` as a *subprocess* per task, enforces the frozen
60 s wall-clock cap (SIGKILL of the whole process group), persists an atomic
record per task and appends a completion-ledger entry, then commits the Modal
Volume so a killed or preempted container can resume exactly where it stopped.

The pure execution logic lives in ``modal_app/v6_shard_worker.py`` (no Modal
import) so it can also be exercised locally by the simulation command in
``scripts/run_v6_modal.py``.

Environment knobs (read at module import by the local orchestrator):
    V6_REPO_ROOT             local repository root to snapshot into the image
    V6_APP_NAME              Modal app name (default ``tii-v6-formal``)
    V6_VOLUME_NAME           Modal Volume name (default ``tii-v6-scalability``)
    V6_VOLUME_PREFIX         prefix inside the volume (default ``scalability_v6_formal``)
    V6_VOLUME_CREATE         ``1`` to create the volume if missing (only from the orchestrator)
    V6_CPU                   physical CPU cores (default ``1``)
    V6_MEMORY_MIB            memory in MiB (default ``4096``)
    V6_FUNCTION_TIMEOUT_S    Modal Function timeout (default ``5400``)
    V6_CELL_TIMEOUT_S        per-task wall-clock cap (default ``60``)
    V6_SCG_CACHE_DIR         per-container SCG cache dir (default ``/tmp/scg_cache``)
"""
from __future__ import annotations

import importlib.util
import json
import os
import sys
from pathlib import Path

import modal

HERE = Path(__file__).resolve().parent
REMOTE_REPO_ROOT = "/root/v6repo"
#: Inside a Modal container the entry file is mounted at /root, while the repo
#: snapshot copied into the image lives at REMOTE_REPO_ROOT.  Resolve the repo
#: root accordingly so importing the worker and reading the source tree works
#: in both places.
IN_CONTAINER = Path(REMOTE_REPO_ROOT).is_dir()
if IN_CONTAINER:
    LOCAL_REPO_ROOT = Path(REMOTE_REPO_ROOT)
else:
    LOCAL_REPO_ROOT = Path(os.environ.get("V6_REPO_ROOT") or HERE.parent).resolve()

APP_NAME = os.environ.get("V6_APP_NAME", "tii-v6-formal")
VOLUME_NAME = os.environ.get("V6_VOLUME_NAME", "tii-v6-scalability")
VOLUME_PREFIX = os.environ.get("V6_VOLUME_PREFIX", "scalability_v6_formal").strip("/")
VOLUME_ROOT = f"/vol/{VOLUME_PREFIX}"
CPU = float(os.environ.get("V6_CPU", "1"))
MEMORY_MIB = int(os.environ.get("V6_MEMORY_MIB", "4096"))
FUNCTION_TIMEOUT_S = int(os.environ.get("V6_FUNCTION_TIMEOUT_S", "5400"))
CELL_TIMEOUT_S = float(os.environ.get("V6_CELL_TIMEOUT_S", "60"))
SCG_CACHE_DIR = os.environ.get("V6_SCG_CACHE_DIR", "/tmp/scg_cache")
VOLUME_CREATE = os.environ.get("V6_VOLUME_CREATE", "0") == "1"

#: Exact dependency pins of the execution image.  Keep in sync with
#: ``requirements-modal.lock`` and ``docs/v6_modal_environment.md``.
IMAGE_PYTHON_VERSION = "3.13"
IMAGE_PACKAGES = ("numpy==2.3.4", "scipy==1.16.2", "modal==1.6.3")
IMAGE_BASE = f"modal.Image.debian_slim(python_version='{IMAGE_PYTHON_VERSION}')"

#: Top-level paths mounted into the image.  ``v6_shard_worker.repo_tree_sha256``
#: uses the same allow-list, so the recorded image spec hash is reproducible.
IMAGE_INCLUDE_TOP = ("src", "runner", "modal_app")
IMAGE_INCLUDE_FILES = ("requirements-v5.lock", "requirements-modal.lock")


def _load_worker_module():
    """Load ``modal_app/v6_shard_worker.py`` by path (avoids the ``modal`` namespace)."""
    module = sys.modules.get("v6_shard_worker")
    if module is not None:
        return module
    candidates = [
        HERE / "v6_shard_worker.py",
        Path(REMOTE_REPO_ROOT) / "modal_app" / "v6_shard_worker.py",
    ]
    path = next((candidate for candidate in candidates if candidate.is_file()), candidates[0])
    spec = importlib.util.spec_from_file_location("v6_shard_worker", path)
    if spec is None or spec.loader is None:
        raise ImportError(f"cannot load shard worker from {path}")
    module = importlib.util.module_from_spec(spec)
    sys.modules["v6_shard_worker"] = module
    spec.loader.exec_module(module)
    return module


worker = _load_worker_module()


def _image_ignore(path: Path) -> bool:
    """Exclude everything except the executed source tree from the image."""
    parts = Path(path).parts
    if not parts:
        return False
    top = parts[0]
    if top in IMAGE_INCLUDE_TOP:
        return False
    if len(parts) == 1 and top in IMAGE_INCLUDE_FILES:
        return False
    return True


def _build_image():
    # Inside the container the image is already materialised; do not try to
    # rebuild it from a mounted path (add_local_dir would not be valid there).
    if IN_CONTAINER:
        return modal.Image.debian_slim(python_version=IMAGE_PYTHON_VERSION)
    image = modal.Image.debian_slim(python_version=IMAGE_PYTHON_VERSION)
    image = image.pip_install(*IMAGE_PACKAGES)
    if LOCAL_REPO_ROOT.is_dir():
        image = image.add_local_dir(
            str(LOCAL_REPO_ROOT), REMOTE_REPO_ROOT, ignore=_image_ignore,
        )
    return image


IMAGE = _build_image()
VOLUME = modal.Volume.from_name(VOLUME_NAME, create_if_missing=VOLUME_CREATE)
app = modal.App(APP_NAME)

_RESOURCES = {"cpu": CPU, "memory_mib": MEMORY_MIB}


def image_spec_sha256() -> str:
    """Deterministic identifier of the execution image ("image digest")."""
    return worker.image_spec_sha256(
        LOCAL_REPO_ROOT,
        base=IMAGE_BASE,
        packages=IMAGE_PACKAGES,
        remote_repo_root=REMOTE_REPO_ROOT,
    )


def repo_tree_sha256() -> str:
    return worker.repo_tree_sha256(
        LOCAL_REPO_ROOT,
        include_top=IMAGE_INCLUDE_TOP,
        extra_files=IMAGE_INCLUDE_FILES,
    )


def _current_function_call_id():
    try:
        return modal.current_function_call_id()
    except Exception:  # noqa: BLE001 - only used for provenance
        return None


@app.function(
    name="run_shard",
    image=IMAGE,
    cpu=CPU,
    memory=MEMORY_MIB,
    timeout=FUNCTION_TIMEOUT_S,
    volumes={"/vol": VOLUME},
    # No automatic retries: the locally-run orchestrator retries only
    # infrastructure failures and never re-runs successful tasks.
    retries=0,
    # A fresh container per shard keeps the /tmp SCG cache from leaking across
    # shards and makes the recorded invocation id unambiguous.
    single_use_containers=True,
)
def run_shard(
    shard_id: str, run_id: str, attempt: int, volume_prefix: str = VOLUME_PREFIX
) -> dict:
    """Execute one frozen shard inside a Modal CPU container.

    ``volume_prefix`` must be passed explicitly by the orchestrator: the
    module-level ``VOLUME_PREFIX`` is fixed at import time in the *local*
    process, while the container copy would otherwise read its own default and
    look for run metadata under the wrong prefix.
    """
    invocation_id = _current_function_call_id()
    prefix = (volume_prefix or VOLUME_PREFIX).strip("/")
    volume_root = f"/vol/{prefix}"
    run_meta = worker.load_run_metadata(volume_root, run_id)
    config = {
        "source_sha": run_meta.get("source_sha"),
        "image_spec_sha256": run_meta.get("image_spec_sha256"),
        "shard_sha256": run_meta.get("shard_sha256") or {},
        "resources": {
            "cpu": run_meta.get("cpu", CPU),
            "memory_mib": run_meta.get("memory_mib", MEMORY_MIB),
        },
        "cell_timeout_override_s": run_meta.get("cell_timeout_override_s"),
        "scg_cache_dir": run_meta.get("scg_cache_dir") or SCG_CACHE_DIR,
        "function_timeout_s": FUNCTION_TIMEOUT_S,
        "volume_name": VOLUME_NAME,
        "volume_prefix": prefix,
        "app_name": APP_NAME,
        "executor": "modal",
    }

    def commit():
        # Modal Volumes require an explicit commit for writes to become visible
        # to other containers / to the orchestrator.  One commit per task keeps
        # the checkpoint granularity at "atomic task", as the protocol requires.
        VOLUME.commit()

    return worker.execute_shard(
        str(shard_id), str(run_id), int(attempt),
        volume_root=volume_root,
        repo_root=REMOTE_REPO_ROOT,
        config=config,
        invocation_id=invocation_id,
        log=lambda message: print(f"[shard {shard_id}] {message}", flush=True),
        commit=commit,
    )


@app.local_entrypoint()
def main(shard_id: str = "000", run_id: str = "manual", attempt: int = 0,
         volume_prefix: str = VOLUME_PREFIX):
    """Debug entry point: ``modal run modal_app/v6_modal_runner.py --shard-id 000 ...``"""
    print(json.dumps(
        run_shard.remote(shard_id, run_id, attempt, volume_prefix),
        indent=2, sort_keys=True,
    ))
