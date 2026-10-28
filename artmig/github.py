"""Thin wrappers over the `gh` CLI for releases + asset upload.

We shell out to `gh` rather than hitting the REST API directly so authentication
piggybacks on the user's existing `gh auth login` (or GH_TOKEN).
"""
from __future__ import annotations

import subprocess
from pathlib import Path

from .util import get_logger

log = get_logger()


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

    def upload_assets(self, tag: str, files: list[Path], clobber: bool = True) -> None:
        if not files:
            return
        args = ["release", "upload", tag, "-R", self.slug,
                *[str(f) for f in files]]
        if clobber:
            args.append("--clobber")
        self._run(args)

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
