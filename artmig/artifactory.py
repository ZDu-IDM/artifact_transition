"""Authenticated Artifactory REST + Docker-registry client.

Covers only the read operations the migration needs:
  - repository / storage tree listing          (api/storage)
  - PyPI simple index                           (api/pypi/<repo>/simple)
  - npm package metadata                        (api/npm/<repo>/<pkg>)
  - Docker v2 catalog + tag list                (<registry>/v2)
  - raw artifact download (streamed, checksummed)
"""
from __future__ import annotations

from collections.abc import Iterator
from pathlib import Path
from typing import Any

import requests

from .config import Config
from .util import get_logger, retry, sha256_file

log = get_logger()


def _non_retryable_http(exc: Exception) -> bool:
    """Client errors (401/403/404) aren't transient — don't burn backoff on them.
    429 (rate limit) and 5xx stay retryable."""
    resp = getattr(exc, "response", None)
    code = getattr(resp, "status_code", None)
    return code is not None and 400 <= code < 500 and code != 429


class Artifactory:
    def __init__(self, cfg: Config):
        self.cfg = cfg
        self.base = cfg.artifactory.base_url
        self.registry = cfg.artifactory.docker_registry
        self.timeout = cfg.options.get("request_timeout_seconds", 60)
        self.attempts = cfg.options.get("retries", 4)
        self.backoff = cfg.options.get("retry_backoff_seconds", 3)

        self.session = requests.Session()
        auth = cfg.artifactory
        if auth.token and auth.user:
            self.session.auth = (auth.user, auth.token)
        elif auth.token:
            # Bare API key / identity token -> send as a bearer header.
            self.session.headers["Authorization"] = f"Bearer {auth.token}"

    # ---- low-level -----------------------------------------------------------
    def _get(self, url: str, **kw) -> requests.Response:
        def do() -> requests.Response:
            r = self.session.get(url, timeout=self.timeout, **kw)
            r.raise_for_status()
            return r
        return retry(do, attempts=self.attempts, backoff=self.backoff,
                     what=f"GET {url}", give_up=_non_retryable_http)

    def get_json(self, url: str) -> Any:
        return self._get(url).json()

    def get_text(self, url: str) -> str:
        return self._get(url).text

    # ---- storage tree (generic + docker fallback) ----------------------------
    def storage(self, repo: str, path: str = "") -> dict:
        """One level of the storage tree for <repo>/<path>."""
        url = f"{self.base}/api/storage/{repo}/{path}".rstrip("/")
        return self.get_json(url)

    def walk_files(self, repo: str, path: str = "") -> Iterator[dict]:
        """Depth-first yield of every file node under <repo>/<path>.

        Each yielded dict has: {'path': relative-path, 'uri', 'folder': False}.
        """
        node = self.storage(repo, path)
        for child in node.get("children", []):
            child_path = (path.rstrip("/") + child["uri"]).lstrip("/")
            if child.get("folder"):
                yield from self.walk_files(repo, child_path)
            else:
                yield {"path": child_path}

    def file_info(self, repo: str, path: str) -> dict:
        """FileInfo incl. checksums + createdBy (lastModified owner)."""
        url = f"{self.base}/api/storage/{repo}/{path}"
        return self.get_json(url)

    # ---- PyPI ---------------------------------------------------------------
    def pypi_simple_index(self, repo: str) -> str:
        return self.get_text(f"{self.base}/api/pypi/{repo}/simple/")

    def pypi_package_page(self, repo: str, name: str) -> str:
        return self.get_text(f"{self.base}/api/pypi/{repo}/simple/{name}/")

    # ---- npm ----------------------------------------------------------------
    def npm_metadata(self, repo: str, pkg: str) -> dict:
        from urllib.parse import quote
        # Scoped names like @scope/name must encode the slash (keep the @).
        enc = quote(pkg, safe="@")
        return self.get_json(f"{self.base}/api/npm/{repo}/{enc}")

    # ---- Docker v2 ----------------------------------------------------------
    def docker_catalog(self, repo: str) -> list[str]:
        """List image repositories. Artifactory exposes a per-repo v2 endpoint."""
        url = f"https://{self.registry}/artifactory/api/docker/{repo}/v2/_catalog"
        return self.get_json(url).get("repositories", [])

    def docker_tags(self, repo: str, image: str) -> list[str]:
        url = f"https://{self.registry}/artifactory/api/docker/{repo}/v2/{image}/tags/list"
        return self.get_json(url).get("tags", []) or []

    # ---- download -----------------------------------------------------------
    def download(self, repo: str, path: str, dest: Path,
                 expected_sha256: str | None = None) -> Path:
        """Stream <repo>/<path> to dest; verify sha256 if provided."""
        dest.parent.mkdir(parents=True, exist_ok=True)
        url = f"{self.base}/{repo}/{path}"

        def do() -> None:
            with self.session.get(url, stream=True, timeout=self.timeout) as r:
                r.raise_for_status()
                tmp = dest.with_suffix(dest.suffix + ".part")
                with tmp.open("wb") as fh:
                    for chunk in r.iter_content(chunk_size=1 << 20):
                        fh.write(chunk)
                tmp.replace(dest)

        retry(do, attempts=self.attempts, backoff=self.backoff, what=f"download {path}")

        if expected_sha256:
            actual = sha256_file(dest)
            if actual.lower() != expected_sha256.lower():
                raise ValueError(f"checksum mismatch for {path}: "
                                 f"expected {expected_sha256}, got {actual}")
        return dest
