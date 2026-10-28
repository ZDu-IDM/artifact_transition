"""idm-data migration: generic data files -> GitHub Releases (one per folder).

Release assets can't contain '/', so nested paths are flattened reversibly
(a/b/c.bin -> a__b__c.bin) and a per-release manifest.json records the mapping
original-path -> asset-name + sha256 + size.

Files larger than the 2 GiB asset cap are streamed into ~1.9 GB parts
(<asset>.part000, .part001, ...). The manifest marks the entry ``split: true``
with per-part checksums and the reassembly command, so consumers can rebuild the
original file and verify it against the whole-file sha256.
"""
from __future__ import annotations

import json
import os
import shutil
from collections import defaultdict
from pathlib import Path

from .context import Context
from .util import (
    get_logger,
    http_reachable,
    plan_split,
    probe_asset,
    report_probes,
    sanitize_asset_name,
    split_file,
)

log = get_logger()
TYPE = "data"


def _cap(ctx: Context) -> int:
    return ctx.cfg.options.get("max_asset_bytes", 2 << 30)


def _chunk(ctx: Context) -> int:
    return ctx.cfg.options.get("split_chunk_bytes", _cap(ctx) - (128 << 20))


def inventory(ctx: Context) -> dict:
    repo = ctx.cfg.types[TYPE]["repo"]
    files: list[dict] = []
    for node in ctx.art.walk_files(repo):
        path = node["path"]
        info = ctx.art.file_info(repo, path)
        checks = info.get("checksums", {})
        files.append({
            "path": path,
            "folder": path.split("/", 1)[0],
            "size": int(info.get("size", 0) or 0),
            "sha256": checks.get("sha256"),
            "sha1": checks.get("sha1"),
            "created_by": info.get("createdBy"),
        })
        log.info("data: %s (%s bytes)", path, info.get("size"))

    by_folder: dict[str, int] = defaultdict(int)
    for f in files:
        by_folder[f["folder"]] += 1

    data = {"type": TYPE, "source_repo": repo, "file_count": len(files),
            "folders": dict(sorted(by_folder.items())), "files": files}
    expected = ctx.cfg.types[TYPE].get("expected_count")
    if expected and len(files) != expected:
        log.warning("data: found %d files, summary expected ~%d", len(files), expected)
    else:
        log.info("data: %d files across %d folders", len(files), len(by_folder))
    return data


def download(ctx: Context) -> None:
    """Download every file — including over-cap ones (they get split at publish)."""
    inv = ctx.read_inventory(TYPE)
    repo = inv["source_repo"]
    cap = _cap(ctx)
    for f in inv["files"]:
        key = f"data:{f['path']}"
        dest = ctx.staging(TYPE, f["path"])
        if ctx.state.is_done(key, "downloaded") and dest.exists():
            continue
        if ctx.dry_run:
            note = "  (over cap — will be split)" if f["size"] > cap else ""
            log.info("[dry-run] download %s%s", f["path"], note)
            continue
        ctx.art.download(repo, f["path"], dest, expected_sha256=f.get("sha256"))
        ctx.state.set(key, "downloaded", path=str(dest))


def publish(ctx: Context) -> None:
    inv = ctx.read_inventory(TYPE)
    cap, chunk = _cap(ctx), _chunk(ctx)
    # group files by top-level folder -> one release each
    groups: dict[str, list[dict]] = defaultdict(list)
    for f in inv["files"]:
        groups[f["folder"]].append(f)

    for folder, items in sorted(groups.items()):
        tag = f"data/{folder}"
        manifest = {"folder": folder, "source_repo": inv["source_repo"], "assets": []}
        out_dir = ctx.staging(TYPE, "_release", folder)
        out_dir.mkdir(parents=True, exist_ok=True)
        upload: list[Path] = []
        split_parts = 0

        for f in items:
            asset = sanitize_asset_name(f["path"])
            src = ctx.staging(TYPE, f["path"])
            if f["size"] > cap:
                entry, part_paths = _handle_split(ctx, f, asset, out_dir, chunk)
                manifest["assets"].append(entry)
                upload.extend(part_paths)
                split_parts += len(entry["parts"])
            else:
                dst = _stage_one(ctx, src, out_dir, asset)
                if dst is not None:
                    upload.append(dst)
                manifest["assets"].append({
                    "original_path": f["path"], "asset_name": asset,
                    "sha256": f.get("sha256"), "size": f["size"]})

        mpath = out_dir / "manifest.json"
        if not ctx.dry_run:
            mpath.write_text(json.dumps(manifest, indent=2), encoding="utf-8")
        upload.append(mpath)

        ctx.gh.ensure_release(tag, title=f"Data: {folder}",
                              notes=f"Migrated from Artifactory {inv['source_repo']}/{folder}.")
        ctx.gh.upload_assets(tag, upload)
        for f in items:
            ctx.state.set(f"data:{f['path']}", "published")
        extra = f", {split_parts} split-parts" if split_parts else ""
        log.info("data: published release %s (%d entries%s)",
                 tag, len(manifest["assets"]), extra)


def _stage_one(ctx: Context, src: Path, out_dir: Path, asset: str) -> Path | None:
    """Expose a downloaded file under its flattened asset name (hardlink, copy fallback)."""
    dst = out_dir / asset
    if ctx.dry_run:
        return dst
    if not src.exists():
        log.warning("data: missing download %s — skipping", src)
        return None
    if not dst.exists():
        try:
            os.link(src, dst)           # cheap: no byte copy on the same filesystem
        except OSError:
            shutil.copy2(src, dst)      # cross-device fallback
    return dst


def _handle_split(ctx: Context, f: dict, asset: str, out_dir: Path,
                  chunk: int) -> tuple[dict, list[Path]]:
    """Split an over-cap file into parts; return (manifest-entry, part-paths)."""
    src = ctx.staging(TYPE, f["path"])
    fname = Path(f["path"]).name
    if ctx.dry_run or not src.exists():
        if not ctx.dry_run:
            log.warning("data: missing download %s — recording planned split only", src)
        parts_meta = plan_split(f["size"], chunk, asset)
        part_paths: list[Path] = []
    else:
        raw = split_file(src, chunk, out_dir, asset)
        parts_meta = [{k: p[k] for k in ("asset_name", "sha256", "size")} for p in raw]
        part_paths = [p["path"] for p in raw]

    entry = {
        "original_path": f["path"], "split": True,
        "sha256": f.get("sha256"), "size": f["size"],
        "part_count": len(parts_meta), "parts": parts_meta,
        "reassemble_unix": f"cat {asset}.part* > {fname}",
        "reassemble_windows": f"copy /b {asset}.part* {fname}",
    }
    log.info("data: %s is over cap — split into %d parts (%d bytes)",
             f["path"], len(parts_meta), f["size"])
    return entry, part_paths


def transform(ctx: Context) -> None:
    log.info("data: transform == path flattening + manifest (handled during publish)")


def cleanup(ctx: Context) -> None:
    """Delete every data/<folder> release and forget its publish state."""
    inv = ctx.read_inventory(TYPE)
    folders = sorted({f["folder"] for f in inv["files"]})
    removed = sum(1 for folder in folders if ctx.gh.delete_release(f"data/{folder}"))
    for f in inv["files"]:
        ctx.state.clear(f"data:{f['path']}")
    log.info("data: rollback removed %d of %d folder release(s)", removed, len(folders))


def verify(ctx: Context) -> None:
    """Probe deployed data: sha256-check the smallest asset + its folder manifest."""
    inv = ctx.read_inventory(TYPE)
    cap = _cap(ctx)
    results: list[tuple[str, str, str]] = []

    # smallest under-cap file with a known checksum -> cheap, meaningful sha256 probe
    candidates = [f for f in inv["files"] if 0 < f["size"] <= cap and f.get("sha256")]
    if candidates:
        f = min(candidates, key=lambda x: x["size"])
        tag = f"data/{f['folder']}"
        asset = sanitize_asset_name(f["path"])
        results.append(probe_asset(f"{tag} asset {Path(f['path']).name}",
                                   ctx.gh.asset_download_url(tag, asset), f["sha256"]))
        ok, detail = http_reachable(ctx.gh.asset_download_url(tag, "manifest.json"))
        results.append(("OK" if ok else "FAIL", f"{tag} manifest.json", detail))
    else:
        results.append(("SKIP", "data asset", "no under-cap file with sha256 in inventory"))

    oversize = [f for f in inv["files"] if f["size"] > cap]
    for f in oversize:
        tag = f"data/{f['folder']}"
        part0 = f"{sanitize_asset_name(f['path'])}.part000"
        ok, detail = http_reachable(ctx.gh.asset_download_url(tag, part0))
        results.append(("OK" if ok else "FAIL", f"{tag} split part {part0}", detail))

    report_probes(TYPE, results)
