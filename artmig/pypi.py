"""PyPI migration: idm-pypi-production -> GitHub Releases + PEP 503 static index.

Strategy
--------
* one GitHub Release per package, tag ``pypi/<normalized-name>``; every version's
  wheels/sdists are uploaded as assets.
* a static PEP 503 "simple" index is generated under ``docs/simple/`` whose links
  point at the release-asset download URLs (so ``pip --index-url`` keeps working).
"""
from __future__ import annotations

from html.parser import HTMLParser
from pathlib import Path

from .context import Context
from .util import get_logger, normalize_pypi_name, sanitize_asset_name

log = get_logger()
TYPE = "pypi"


class _LinkParser(HTMLParser):
    """Collect (href, text, data-dist-info-metadata/sha) from simple-index HTML."""

    def __init__(self) -> None:
        super().__init__()
        self.links: list[dict] = []
        self._cur: dict | None = None

    def handle_starttag(self, tag, attrs):
        if tag == "a":
            d = dict(attrs)
            self._cur = {"href": d.get("href", ""), "text": ""}

    def handle_data(self, data):
        if self._cur is not None:
            self._cur["text"] += data.strip()

    def handle_endtag(self, tag):
        if tag == "a" and self._cur is not None:
            self.links.append(self._cur)
            self._cur = None


def _parse_links(html: str) -> list[dict]:
    p = _LinkParser()
    p.feed(html)
    return p.links


# ---- inventory --------------------------------------------------------------
def inventory(ctx: Context) -> dict:
    repo = ctx.cfg.types[TYPE]["repo"]
    index_html = ctx.art.pypi_simple_index(repo)
    raw_names = [lk["text"] or lk["href"].strip("/") for lk in _parse_links(index_html)]

    # Collapse PEP 503 duplicates (idmtools_cli vs idmtools-cli).
    norm_to_display: dict[str, str] = {}
    for name in raw_names:
        norm = normalize_pypi_name(name)
        norm_to_display.setdefault(norm, name)

    packages: dict[str, dict] = {}
    for norm, display in sorted(norm_to_display.items()):
        page = ctx.art.pypi_package_page(repo, norm)
        files = []
        for lk in _parse_links(page):
            href = lk["href"]
            filename = lk["text"] or href.split("/")[-1].split("#")[0]
            sha256 = None
            if "#sha256=" in href:
                sha256 = href.split("#sha256=", 1)[1]
            # repo-relative path for download: strip everything up to the repo name
            path = _relpath(href, repo, filename)
            files.append({"filename": filename, "sha256": sha256, "path": path})
        packages[norm] = {"display_name": display, "files": files}
        log.info("pypi: %-40s %d files", norm, len(files))

    total_files = sum(len(p["files"]) for p in packages.values())
    data = {
        "type": TYPE,
        "source_repo": repo,
        "package_count": len(packages),
        "file_count": total_files,
        "packages": packages,
    }
    _reconcile(ctx, len(packages))
    return data


def _relpath(href: str, repo: str, filename: str) -> str:
    """Repo-relative download path from a simple-index href.

    Artifactory emits relative hrefs like ``../../emodpy/1.1.0/emodpy-1.1.0.whl``;
    stripping the leading ``.``/``..`` segments yields the repo-relative path.
    """
    clean = href.split("#", 1)[0]
    if repo in clean:
        return clean.split(repo, 1)[1].lstrip("/")
    parts = [p for p in clean.split("/") if p and p not in (".", "..")]
    return "/".join(parts) if parts else filename


def _reconcile(ctx: Context, found: int) -> None:
    expected = ctx.cfg.types[TYPE].get("expected_count")
    if expected and found != expected:
        log.warning("pypi: found %d packages, summary expected ~%d (drift is OK, just noting)",
                    found, expected)
    else:
        log.info("pypi: %d packages", found)


# ---- download ---------------------------------------------------------------
def download(ctx: Context) -> None:
    inv = ctx.read_inventory(TYPE)
    repo = inv["source_repo"]
    for norm, pkg in inv["packages"].items():
        for f in pkg["files"]:
            key = f"pypi:{norm}:{f['filename']}"
            dest = ctx.staging(TYPE, norm, f["filename"])
            if ctx.state.is_done(key, "downloaded") and dest.exists():
                continue
            if ctx.dry_run:
                log.info("[dry-run] download %s", f["path"])
                continue
            ctx.art.download(repo, f["path"], dest, expected_sha256=f.get("sha256"))
            ctx.state.set(key, "downloaded", path=str(dest))
            log.info("downloaded %s", f["filename"])


# ---- publish (release assets) -----------------------------------------------
# The PEP 503 index is produced by transform(); the CLI runs it as its own phase
# (download -> transform -> publish / run), so publish() does not regenerate it.
def publish(ctx: Context) -> None:
    inv = ctx.read_inventory(TYPE)
    for norm, pkg in inv["packages"].items():
        tag = f"pypi/{norm}"
        ctx.gh.ensure_release(tag, title=f"PyPI: {pkg['display_name']}",
                              notes=f"Migrated from Artifactory {inv['source_repo']}.")
        if ctx.dry_run:
            log.info("[dry-run] upload %d assets to release %s", len(pkg["files"]), tag)
            continue
        files = [ctx.staging(TYPE, norm, f["filename"]) for f in pkg["files"]]
        files = [f for f in files if f.exists()]
        if not files:
            log.warning("pypi: no downloaded files for %s — run download first", norm)
            continue
        ctx.gh.upload_assets(tag, files)
        for f in pkg["files"]:
            ctx.state.set(f"pypi:{norm}:{f['filename']}", "published")


def transform(ctx: Context) -> None:
    """Generate docs/simple/ (PEP 503) linking to release-asset URLs."""
    inv = ctx.read_inventory(TYPE)
    simple = ctx.cfg.docs_dir / "simple"
    simple.mkdir(parents=True, exist_ok=True)

    root_links = []
    for norm, pkg in sorted(inv["packages"].items()):
        tag = f"pypi/{norm}"
        pkg_dir = simple / norm
        pkg_dir.mkdir(parents=True, exist_ok=True)
        file_links = []
        for f in pkg["files"]:
            url = ctx.gh.asset_download_url(tag, sanitize_asset_name(f["filename"]))
            frag = f"#sha256={f['sha256']}" if f.get("sha256") else ""
            file_links.append(f'    <a href="{url}{frag}">{f["filename"]}</a><br/>')
        (pkg_dir / "index.html").write_text(_page(pkg["display_name"], file_links),
                                            encoding="utf-8")
        root_links.append(f'    <a href="{norm}/">{pkg["display_name"]}</a><br/>')

    (simple / "index.html").write_text(_page("Simple index", root_links), encoding="utf-8")
    log.info("pypi: wrote PEP 503 index -> %s (%d packages)", simple, len(inv["packages"]))


def _page(title: str, links: list[str]) -> str:
    body = "\n".join(links)
    return ("<!DOCTYPE html>\n<html><head><meta charset=\"utf-8\">"
            f"<title>{title}</title></head>\n<body>\n{body}\n</body></html>\n")


# ---- verify -----------------------------------------------------------------
def verify(ctx: Context) -> None:
    inv = ctx.read_inventory(TYPE)
    sample = next(iter(inv["packages"]), None)
    url = f"{ctx.cfg.pages_url}/simple/"
    log.info("pypi: verify with -> pip install --index-url %s %s", url, sample or "<pkg>")
