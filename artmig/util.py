"""Shared helpers: logging, checksums, path sanitization, retry."""
from __future__ import annotations

import hashlib
import logging
import re
import sys
import time
from collections.abc import Callable
from pathlib import Path
from typing import TypeVar

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


def resolve_exe(name: str) -> str:
    """Resolve a tool to a runnable path (finds npm.cmd / docker.exe on Windows).

    subprocess on Windows won't find `npm` because it's `npm.cmd`; shutil.which
    honours PATHEXT and returns the real path. Falls back to the bare name.
    """
    import shutil
    return shutil.which(name) or name


# ---- verification probes (read-only, used by each type's verify()) -----------
def http_reachable(url: str, timeout: int = 30) -> tuple[bool, str]:
    """GET a URL (follows redirects); return (ok, 'HTTP <status>' | 'error: ...')."""
    import requests
    try:
        r = requests.get(url, timeout=timeout, stream=True)
        r.close()
        return (200 <= r.status_code < 300, f"HTTP {r.status_code}")
    except Exception as exc:
        return (False, f"error: {exc}")


def http_size(url: str, timeout: int = 30) -> tuple[int | None, object]:
    """HEAD a URL; return (content-length | None, status). None size = unreachable."""
    import requests
    try:
        r = requests.head(url, timeout=timeout, allow_redirects=True)
        if not (200 <= r.status_code < 300):
            return (None, r.status_code)
        return (int(r.headers.get("Content-Length", 0) or 0), r.status_code)
    except Exception as exc:
        return (None, str(exc))


def sha256_url(url: str, timeout: int = 120) -> str | None:
    """Stream a URL and return its sha256, or None if it couldn't be fetched."""
    import requests
    try:
        h = hashlib.sha256()
        with requests.get(url, stream=True, timeout=timeout) as r:
            if not (200 <= r.status_code < 300):
                return None
            for chunk in r.iter_content(1 << 20):
                h.update(chunk)
        return h.hexdigest()
    except Exception:
        return None


def probe_asset(label: str, url: str, sha256: str | None = None,
                sha_max_bytes: int = 8_000_000) -> tuple[str, str, str]:
    """Probe a release asset URL. Verifies sha256 if the asset is small enough.

    Returns (status, label, detail) with status in OK/FAIL.
    """
    size, status = http_size(url)
    if size is None:
        return ("FAIL", label, f"unreachable ({status})")
    if sha256 and 0 < size <= sha_max_bytes:
        actual = sha256_url(url)
        if actual is None:
            return ("FAIL", label, "download failed during sha256 check")
        if actual.lower() == sha256.lower():
            return ("OK", label, f"sha256 match ({size} B)")
        return ("FAIL", label, "sha256 MISMATCH")
    note = ", sha256 skipped (large)" if sha256 else ""
    return ("OK", label, f"reachable ({size} B){note}")


def report_probes(type_name: str, results: list[tuple[str, str, str]]) -> None:
    """Log OK/FAIL/SKIP probe results and raise if any hard FAILs occurred."""
    marks = {"OK": "[ OK ]", "FAIL": "[FAIL]", "SKIP": "[SKIP]"}
    log = get_logger()
    for status, label, detail in results:
        log.info("%s %s: %s — %s", marks.get(status, status), type_name, label, detail)
    oks = sum(1 for s, _, _ in results if s == "OK")
    fails = sum(1 for s, _, _ in results if s == "FAIL")
    skips = sum(1 for s, _, _ in results if s == "SKIP")
    log.info("%s verify: %d ok, %d failed, %d skipped", type_name, oks, fails, skips)
    if fails:
        raise RuntimeError(f"{type_name}: {fails} verification probe(s) failed")


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
