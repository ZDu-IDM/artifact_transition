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


def _is_mutating(args: list[str]) -> bool:
    mutating = {"create", "upload", "edit", "delete"}
    return bool(set(args) & mutating)
