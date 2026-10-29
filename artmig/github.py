"""Thin wrappers over the `gh` CLI for releases + asset upload.

We shell out to `gh` rather than hitting the REST API directly so authentication
piggybacks on the user's existing `gh auth login` (or GH_TOKEN).
"""
from __future__ import annotations

import json
import subprocess
import time
from pathlib import Path

from .util import get_logger

log = get_logger()

# gh/API output substrings meaning "slow down" (secondary/abuse limit on content creation).
_RATE_LIMIT = ("rate limit", "secondary rate", "abuse detection", "retry your request")


class GitHub:
    def __init__(self, slug: str, dry_run: bool = False):
        self.slug = slug            # "owner/repo"
        self.dry_run = dry_run

    def _run(self, args: list[str], capture: bool = False) -> str:
        cmd = ["gh", *args]
        if self.dry_run and _is_mutating(args):
            log.info("[dry-run] %s", " ".join(cmd))
            return ""
        log.info("$ %s", " ".join(cmd))
        res = subprocess.run(cmd, check=True, text=True,
                             capture_output=capture)
        return res.stdout if capture else ""

    # ---- releases ------------------------------------------------------------
    def release_exists(self, tag: str) -> bool:
        try:
            subprocess.run(["gh", "release", "view", tag, "-R", self.slug],
                           check=True, capture_output=True, text=True)
            return True
        except subprocess.CalledProcessError:
            return False

    def ensure_release(self, tag: str, title: str, notes: str = "") -> None:
        if self.release_exists(tag):
            log.info("release %s already exists", tag)
            return
        self._run(["release", "create", tag, "-R", self.slug,
                   "--title", title, "--notes", notes or title])

    def existing_asset_names(self, tag: str) -> set[str]:
        """Names of assets already on a release (for resumable, idempotent uploads)."""
        try:
            out = subprocess.run(["gh", "release", "view", tag, "-R", self.slug,
                                  "--json", "assets"],
                                 check=True, capture_output=True, text=True)
            return {a["name"] for a in json.loads(out.stdout).get("assets", [])}
        except subprocess.CalledProcessError:
            return set()

    def upload_assets(self, tag: str, files: list[Path], batch: int = 50) -> None:
        """Upload only assets not already present, in rate-limit-aware batches.

        Skipping existing assets makes reruns cheap and keeps us under GitHub's
        secondary (content-creation) rate limit; each batch retries with backoff
        when that limit is hit, so a large publish self-paces instead of failing.
        """
        if not files:
            return
        if self.dry_run:
            log.info("[dry-run] upload %d assets to release %s", len(files), tag)
            return
        existing = self.existing_asset_names(tag)
        todo = [f for f in files if f.name not in existing]
        if not todo:
            log.info("%s: all %d assets already present", tag, len(files))
            return
        if len(todo) != len(files):
            log.info("%s: %d assets already present, uploading %d",
                     tag, len(files) - len(todo), len(todo))
        for i in range(0, len(todo), batch):
            self._upload_batch(tag, todo[i:i + batch])

    def _upload_batch(self, tag: str, chunk: list[Path]) -> None:
        # --clobber so a retry after a partial batch is safe (overwrites, no dup error).
        args = ["gh", "release", "upload", tag, "-R", self.slug,
                *[str(f) for f in chunk], "--clobber"]
        waits = [30, 60, 120, 300, 600]
        for attempt, wait in enumerate([*waits, None], start=1):
            r = subprocess.run(args, capture_output=True, text=True)
            if r.returncode == 0:
                return
            blob = f"{r.stderr}\n{r.stdout}".lower()
            if any(s in blob for s in _RATE_LIMIT) and wait is not None:
                log.warning("%s: secondary rate limit — sleeping %ds (attempt %d)",
                            tag, wait, attempt)
                time.sleep(wait)
                continue
            raise RuntimeError(f"upload to {tag} failed: {(r.stderr or '').strip()[-300:]}")

    def asset_download_url(self, tag: str, asset_name: str) -> str:
        return f"https://github.com/{self.slug}/releases/download/{tag}/{asset_name}"

    # ---- cleanup / rollback --------------------------------------------------
    def delete_release(self, tag: str) -> bool:
        """Delete a release and its assets + the git tag. Returns False if absent."""
        if not self.release_exists(tag):
            log.info("release %s absent — nothing to delete", tag)
            return False
        self._run(["release", "delete", tag, "-R", self.slug, "--yes", "--cleanup-tag"])
        return True

    def delete_package(self, package_type: str, name: str) -> None:
        """Delete a whole GitHub Packages entry (npm/container). Best-effort.

        Needs a token with `delete:packages`. 404 / permission issues are logged
        as warnings rather than raised, so a partial rollback keeps going.
        """
        from urllib.parse import quote
        path = f"/user/packages/{package_type}/{quote(name, safe='')}"
        if self.dry_run:
            log.info("[dry-run] gh api --method DELETE %s", path)
            return
        try:
            subprocess.run(["gh", "api", "--method", "DELETE", path],
                           check=True, text=True, capture_output=True)
            log.info("deleted %s package %s", package_type, name)
        except subprocess.CalledProcessError as exc:
            tail = (exc.stderr or "").strip().splitlines()
            log.warning("could not delete %s package %s: %s",
                        package_type, name, tail[-1][:120] if tail else exc)


def _is_mutating(args: list[str]) -> bool:
    mutating = {"create", "upload", "edit", "delete"}
    return bool(set(args) & mutating)
