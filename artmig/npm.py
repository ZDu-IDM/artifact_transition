"""npm migration: idm-npm-production -> GitHub Packages npm (@<namespace>/*).

GitHub Packages requires the package scope to match the owning account, so each
tarball's package.json ``name`` is rewritten to ``@<namespace>/<pkg>`` before
publishing. The old->new name map is recorded for the README.
"""
from __future__ import annotations

import json
import subprocess
import tarfile
import tempfile
from pathlib import Path

from .context import Context
from .util import get_logger

log = get_logger()
TYPE = "npm"

# The 4 packages are known from the summary; we still fetch live metadata.
KNOWN = ["comps-ui-auth", "microcomps", "postette", "comps-ui"]


def inventory(ctx: Context) -> dict:
    repo = ctx.cfg.types[TYPE]["repo"]
    packages: dict[str, dict] = {}
    for pkg in KNOWN:
        try:
            meta = ctx.art.npm_metadata(repo, pkg)
        except Exception as exc:  # noqa: BLE001
            log.warning("npm: could not fetch %s: %s", pkg, exc)
            continue
        # Artifactory sometimes lists a version twice under both "0.2.0" and the
        # alias "v0.2.0", pointing at the same tarball. Dedup by tarball path so we
        # don't download twice or fail npm publish on a duplicate version.
        by_path: dict[str, dict] = {}
        for ver, vmeta in (meta.get("versions") or {}).items():
            dist = vmeta.get("dist", {})
            tarball = dist.get("tarball", "")
            path = tarball.split(repo, 1)[1].lstrip("/") if repo in tarball else tarball
            label = ver[1:] if ver[:1] == "v" and ver[1:2].isdigit() else ver
            if path not in by_path:
                by_path[path] = {"version": label, "path": path,
                                 "shasum": dist.get("shasum"), "tarball": tarball}
        versions = list(by_path.values())
        packages[pkg] = {
            "latest": (meta.get("dist-tags") or {}).get("latest"),
            "new_name": f"@{ctx.cfg.namespace}/{pkg}",
            "versions": versions,
        }
        log.info("npm: %-16s %d versions", pkg, len(versions))

    data = {"type": TYPE, "source_repo": repo,
            "package_count": len(packages), "packages": packages}
    expected = ctx.cfg.types[TYPE].get("expected_count")
    if expected and len(packages) != expected:
        log.warning("npm: found %d packages, expected %d", len(packages), expected)
    return data


def download(ctx: Context) -> None:
    inv = ctx.read_inventory(TYPE)
    repo = inv["source_repo"]
    for pkg, meta in inv["packages"].items():
        for v in meta["versions"]:
            key = f"npm:{pkg}:{v['version']}"
            dest = ctx.staging(TYPE, pkg, f"{pkg}-{v['version']}.tgz")
            if ctx.state.is_done(key, "downloaded") and dest.exists():
                continue
            if ctx.dry_run:
                log.info("[dry-run] download %s@%s", pkg, v["version"])
                continue
            ctx.art.download(repo, v["path"], dest)
            ctx.state.set(key, "downloaded", path=str(dest))
            log.info("downloaded %s@%s", pkg, v["version"])


def _rescope_tarball(src: Path, new_name: str, slug: str, namespace: str) -> Path:
    """Repack a tgz with package.json name rewritten to the scoped name."""
    out = src.with_name(src.stem + "-scoped.tgz")
    with tempfile.TemporaryDirectory() as td:
        tmp = Path(td)
        with tarfile.open(src, "r:gz") as tf:
            tf.extractall(tmp)  # noqa: S202 - trusted internal artifacts
        pkg_root = tmp / "package"
        pj = pkg_root / "package.json"
        data = json.loads(pj.read_text(encoding="utf-8"))
        data["name"] = new_name
        data.setdefault("publishConfig", {})["registry"] = "https://npm.pkg.github.com"
        data["repository"] = {"type": "git", "url": f"git+https://github.com/{slug}.git"}
        pj.write_text(json.dumps(data, indent=2), encoding="utf-8")
        with tarfile.open(out, "w:gz") as tf:
            tf.add(pkg_root, arcname="package")
    return out


def publish(ctx: Context) -> None:
    inv = ctx.read_inventory(TYPE)
    slug = ctx.cfg.github_slug
    for pkg, meta in inv["packages"].items():
        for v in meta["versions"]:
            key = f"npm:{pkg}:{v['version']}"
            src = ctx.staging(TYPE, pkg, f"{pkg}-{v['version']}.tgz")
            if ctx.dry_run:
                log.info("[dry-run] rescope %s and npm publish %s@%s -> npm.pkg.github.com",
                         src.name, meta["new_name"], v["version"])
                continue
            if not src.exists():
                log.warning("npm: missing tarball for %s@%s — download first", pkg, v["version"])
                continue
            scoped = _rescope_tarball(src, meta["new_name"], slug, ctx.cfg.namespace)
            subprocess.run(
                ["npm", "publish", str(scoped),
                 "--registry=https://npm.pkg.github.com"],
                check=True, text=True)
            ctx.state.set(key, "published")
            log.info("published %s@%s", meta["new_name"], v["version"])


def transform(ctx: Context) -> None:
    log.info("npm: transform happens inline during publish (tarball rescope)")


def verify(ctx: Context) -> None:
    inv = ctx.read_inventory(TYPE)
    for meta in inv["packages"].values():
        log.info("npm: verify -> npm view %s --registry=https://npm.pkg.github.com",
                 meta["new_name"])
        break
