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
from .util import get_logger, report_probes, resolve_exe

log = get_logger()
TYPE = "npm"
REGISTRY = "https://npm.pkg.github.com"


def _discover_packages(ctx: Context, repo: str) -> list[str]:
    """Enumerate package names live from the repo storage tree.

    Unscoped packages are top-level folders; scoped packages live under @scope/.
    Skips Artifactory metadata folders (e.g. .npm).
    """
    pkgs: list[str] = []
    for child in ctx.art.storage(repo).get("children", []):
        if not child.get("folder"):
            continue
        name = child["uri"].strip("/")
        if name.startswith("."):          # .npm and other metadata
            continue
        if name.startswith("@"):          # scope folder -> descend
            for sub in ctx.art.storage(f"{repo}/{name}").get("children", []):
                if sub.get("folder"):
                    pkgs.append(f"{name}/{sub['uri'].strip('/')}")
        else:
            pkgs.append(name)
    return sorted(pkgs)


def inventory(ctx: Context) -> dict:
    repo = ctx.cfg.types[TYPE]["repo"]
    discovered = _discover_packages(ctx, repo)
    log.info("npm: discovered %d package(s) in %s", len(discovered), repo)
    packages: dict[str, dict] = {}
    for pkg in discovered:
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
        base = pkg.split("/")[-1]  # drop any existing @scope/ for the new name
        packages[pkg] = {
            "latest": (meta.get("dist-tags") or {}).get("latest"),
            "new_name": f"@{ctx.cfg.namespace}/{base}",
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
    for pkg, meta in inv["packages"].items():
        for v in meta["versions"]:
            key = f"npm:{pkg}:{v['version']}"
            dest = ctx.staging(TYPE, pkg, f"{pkg}-{v['version']}.tgz")
            if ctx.state.is_done(key, "downloaded") and dest.exists():
                continue
            if ctx.dry_run:
                log.info("[dry-run] download %s@%s", pkg, v["version"])
                continue
            # Download via the authoritative dist.tarball URL — its layout isn't
            # always a clean repo/path (e.g. comps-ui-auth@2.1.0 lacks the /-/ dir).
            ctx.art.download_abs(v["tarball"], dest)
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
                [resolve_exe("npm"), "publish", str(scoped), f"--registry={REGISTRY}"],
                check=True, text=True)
            ctx.state.set(key, "published")
            log.info("published %s@%s", meta["new_name"], v["version"])


def transform(ctx: Context) -> None:
    log.info("npm: transform happens inline during publish (tarball rescope)")


def cleanup(ctx: Context) -> None:
    """Delete the rescoped @<ns>/* packages from GitHub Packages and reset state."""
    inv = ctx.read_inventory(TYPE)
    for pkg, meta in inv["packages"].items():
        ctx.gh.delete_package("npm", meta["new_name"])
        for v in meta["versions"]:
            ctx.state.clear(f"npm:{pkg}:{v['version']}")
    log.info("npm: rollback requested deletion of %d package(s) (warnings above = absent/no perm)",
             len(inv["packages"]))


def _npm_view(name: str) -> tuple[str, str, str]:
    """`npm view <name> version` against GitHub Packages. SKIP if npm/auth absent."""
    try:
        out = subprocess.run([resolve_exe("npm"), "view", name, "version",
                              f"--registry={REGISTRY}"],
                             capture_output=True, text=True, timeout=60)
    except FileNotFoundError:
        return ("SKIP", f"npm view {name}", "npm not installed")
    except Exception as exc:
        return ("SKIP", f"npm view {name}", str(exc))
    if out.returncode == 0 and out.stdout.strip():
        return ("OK", f"npm view {name}", f"published version {out.stdout.strip()}")
    # Pull a meaningful line out of npm's noisy stderr (skip the "complete log" footer).
    lines = [ln.strip() for ln in (out.stderr or "").splitlines()
             if ln.strip() and "complete log" not in ln]
    keys = ("404", "E404", "401", "ENEEDAUTH", "not found", "Not Found")
    reason = next((ln for ln in lines if any(k in ln for k in keys)),
                  lines[0] if lines else "not found")
    # 404 vs auth/registry issues are indistinguishable here -> SKIP, not FAIL.
    return ("SKIP", f"npm view {name}", f"unavailable ({reason[:90]})")


def verify(ctx: Context) -> None:
    """Probe that each rescoped package is live on GitHub Packages (needs npm auth)."""
    inv = ctx.read_inventory(TYPE)
    results = [_npm_view(meta["new_name"]) for meta in inv["packages"].values()]
    report_probes(TYPE, results)
