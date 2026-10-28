"""Shared helpers: logging, checksums, path sanitization, retry."""
from __future__ import annotations

import hashlib
import logging
import re
import sys
import time
from pathlib import Path
from typing import Callable, TypeVar

T = TypeVar("T")

_LOGGER_NAME = "artmig"


def get_logger() -> logging.Logger:
    log = logging.getLogger(_LOGGER_NAME)
    if not log.handlers:
        log.setLevel(logging.INFO)
        handler = logging.StreamHandler(sys.stdout)
        handler.setFormatter(logging.Formatter("%(asctime)s %(levelname)-7s %(message)s",
                                                datefmt="%H:%M:%S"))
        log.addHandler(handler)
    return log


def add_file_log(path: Path) -> None:
    """Also tee logs into a file (manifests/migration-*.log)."""
    path.parent.mkdir(parents=True, exist_ok=True)
    log = get_logger()
    fh = logging.FileHandler(path, encoding="utf-8")
    fh.setFormatter(logging.Formatter("%(asctime)s %(levelname)-7s %(message)s"))
    log.addHandler(fh)


def sha256_file(path: Path, chunk: int = 1 << 20) -> str:
    h = hashlib.sha256()
    with path.open("rb") as fh:
        for block in iter(lambda: fh.read(chunk), b""):
            h.update(block)
    return h.hexdigest()


def split_file(src: Path, chunk_bytes: int, out_dir: Path, base_name: str,
               read_size: int = 1 << 20) -> list[dict]:
    """Split ``src`` into ``out_dir/{base_name}.partNNN`` pieces of ``chunk_bytes``.

    Streams in small reads so memory stays flat regardless of part size.
    Returns one dict per part: {asset_name, sha256, size, path}. Reassemble with
    ``cat {base}.part* > file`` (Unix) or ``copy /b {base}.part* file`` (Windows).
    """
    out_dir.mkdir(parents=True, exist_ok=True)
    parts: list[dict] = []
    idx = 0
    with src.open("rb") as fh:
        while True:
            part_path = out_dir / f"{base_name}.part{idx:03d}"
            written = 0
            h = hashlib.sha256()
            with part_path.open("wb") as out:
                while written < chunk_bytes:
                    buf = fh.read(min(read_size, chunk_bytes - written))
                    if not buf:
                        break
                    out.write(buf)
                    h.update(buf)
                    written += len(buf)
            if written == 0:
                part_path.unlink()  # no trailing empty part
                break
            parts.append({"asset_name": part_path.name, "sha256": h.hexdigest(),
                          "size": written, "path": part_path})
            idx += 1
            if written < chunk_bytes:
                break  # last (short) part
    return parts


def plan_split(size: int, chunk_bytes: int, base_name: str) -> list[dict]:
    """Dry-run equivalent of split_file: predicted parts without touching bytes."""
    parts: list[dict] = []
    remaining, idx = size, 0
    while remaining > 0:
        take = min(chunk_bytes, remaining)
        parts.append({"asset_name": f"{base_name}.part{idx:03d}",
                      "sha256": None, "size": take})
        remaining -= take
        idx += 1
    return parts


# GitHub release assets cannot contain '/' and must be unique within a release.
# We flatten nested paths reversibly: a/b/c.bin -> a__b__c.bin (recorded in a manifest).
def sanitize_asset_name(path: str) -> str:
    flat = path.strip("/").replace("/", "__")
    # Keep it conservative: GitHub also rejects a few characters in asset names.
    return re.sub(r"[^A-Za-z0-9._-]", "_", flat)


# PEP 503 project-name normalization: lowercase, runs of [-_.] -> single '-'.
def normalize_pypi_name(name: str) -> str:
    return re.sub(r"[-_.]+", "-", name).lower()


def retry(fn: Callable[[], T], *, attempts: int, backoff: float,
          what: str = "operation",
          give_up: Callable[[Exception], bool] | None = None) -> T:
    """Call fn(), retrying transient failures with linear backoff.

    ``give_up(exc)`` returning True aborts immediately (e.g. non-transient HTTP
    4xx like 401/403/404 — retrying those just wastes backoff time).
    """
    log = get_logger()
    last: Exception | None = None
    for i in range(1, attempts + 1):
        try:
            return fn()
        except Exception as exc:  # noqa: BLE001 - we genuinely want to retry anything transient
            last = exc
            if give_up is not None and give_up(exc):
                raise
            if i == attempts:
                break
            wait = backoff * i
            log.warning("%s failed (attempt %d/%d): %s — retrying in %.1fs",
                        what, i, attempts, exc, wait)
            time.sleep(wait)
    assert last is not None
    raise last
