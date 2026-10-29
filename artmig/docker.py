"""Docker migration: Artifactory Docker repos -> GitHub Container Registry (ghcr.io).

For each image:tag we pull from packages.idmod.org, retag to
``ghcr.io/<namespace>/<source-repo>/<image>:<tag>`` (lowercased; the source-repo
prefix preserves provenance and prevents collisions between the two source repos),
then push. No local staging dir — images live in the local Docker daemon cache.
"""
from __future__ import annotations

import subprocess

from .context import Context
from .util import get_logger, report_probes, resolve_exe

log = get_logger()
TYPE = "docker"


def _select_tags(tags: list[str], selection: str) -> list[str]:
    """Apply the configured tag_selection to an image's tag list.

    - "all"    : every tag (default).
    - "one"    : a single representative tag — 'latest' if present, else the first;
                 keeps every image (unlike "latest", which drops images lacking it).
    - "latest" : only the 'latest' tag (images without it migrate 0 tags).
    """
    if selection == "all" or not tags:
        return tags
    if selection == "latest":
        return [t for t in tags if t == "latest"]
    if selection == "one":
        return ["latest"] if "latest" in tags else tags[:1]
    raise ValueError(f"unknown docker tag_selection '{selection}' (all|one|latest)")


def inventory(ctx: Context) -> dict:
    repos = ctx.cfg.types[TYPE]["repos"]
    selection = ctx.cfg.types[TYPE].get("tag_selection", "all")
    only = ctx.image_filter or []
    images: dict[str, dict] = {}
    total_images = 0
    for repo in repos:
        try:
            names = ctx.art.docker_catalog(repo)
        except Exception as exc:  # noqa: BLE001
            log.warning("docker: catalog for %s failed (%s) — "
                        "falling back to storage listing", repo, exc)
            names = _catalog_from_storage(ctx, repo)
        for img in names:
            key = f"{repo}/{img}"
            if only and not any(s in key for s in only):
                continue
            try:
                tags = ctx.art.docker_tags(repo, img)
            except Exception as exc:  # noqa: BLE001
                log.warning("docker: tags for %s/%s failed: %s", repo, img, exc)
                tags = []
            tags = _select_tags(tags, selection)
            images[f"{repo}/{img}"] = {"source_repo": repo, "image": img, "tags": tags}
            total_images += 1
            log.info("docker: %-45s %d tags", f"{repo}/{img}", len(tags))

    if selection != "all":
        log.info("docker: tag_selection=%s -> %d tags across %d images",
                 selection, sum(len(m["tags"]) for m in images.values()), total_images)

    data = {"type": TYPE, "source_repos": repos,
            "image_count": total_images, "images": images}
    expected = ctx.cfg.types[TYPE].get("expected_count")
    if expected and total_images != expected:
        log.warning("docker: found %d images, summary expected ~%d", total_images, expected)
    return data


def _catalog_from_storage(ctx: Context, repo: str) -> list[str]:
    """Reconstruct image names from the storage tree (top-level folders).

    Skips Artifactory metadata folders (e.g. ``.jfrog``) that aren't real images.
    """
    node = ctx.art.storage(repo)
    return [c["uri"].strip("/") for c in node.get("children", [])
            if c.get("folder") and not c["uri"].strip("/").startswith(".")]


def _dest_ref(ctx: Context, repo: str, image: str, tag: str) -> str:
    name = f"ghcr.io/{ctx.cfg.namespace}/{repo}/{image}:{tag}".lower()
    return name


def _src_registry(ctx: Context, repo: str) -> str:
    """Subdomain method: each repo is its own registry host <repo>.<registry>.
    (The plain <registry>/v2/ root is misconfigured and returns 400.)"""
    return f"{repo}.{ctx.cfg.artifactory.docker_registry}"


def _src_ref(ctx: Context, repo: str, image: str, tag: str) -> str:
    return f"{_src_registry(ctx, repo)}/{image}:{tag}"


_LOGGED_IN: set[str] = set()


def _ensure_login(ctx: Context, repo: str) -> None:
    """`docker login` to a repo's subdomain registry once (needs Artifactory creds)."""
    reg = _src_registry(ctx, repo)
    if ctx.dry_run or reg in _LOGGED_IN:
        return
    auth = ctx.cfg.artifactory
    if not auth.token:
        raise RuntimeError(f"docker: no ARTIFACTORY_TOKEN — cannot log in to {reg}")
    # This registry validates the username against the token (it rejected a
    # placeholder with "Wrong username was used"), so ARTIFACTORY_USER is required.
    if not auth.user:
        raise RuntimeError(
            f"docker: ARTIFACTORY_USER is required to log in to {reg} "
            "(the registry rejects a placeholder username). Set the ARTIFACTORY_USER secret.")
    subprocess.run([resolve_exe("docker"), "login", reg, "-u", auth.user,
                    "--password-stdin"], input=auth.token, check=True, text=True)
    _LOGGED_IN.add(reg)
    log.info("docker: logged in to %s", reg)


def download(ctx: Context) -> None:
    """No-op for Docker: pulls happen per-image during publish and are removed
    right after push, so images never all land on disk at once."""
    log.info("docker: download is a no-op — publish pulls+pushes+removes per image")


def _dest_exists(ctx: Context, dst: str) -> bool:
    """True if the destination tag already exists on ghcr (cross-run resume)."""
    if ctx.dry_run:
        return False
    r = subprocess.run([resolve_exe("docker"), "manifest", "inspect", dst],
                       capture_output=True, text=True)
    return r.returncode == 0


def _rmi(refs: list[str]) -> None:
    """Remove local images to free disk (best-effort)."""
    if not refs:
        return
    subprocess.run([resolve_exe("docker"), "rmi", "-f", *sorted(set(refs))],
                   capture_output=True, text=True)


def publish(ctx: Context) -> None:
    inv = ctx.read_inventory(TYPE)
    for _, meta in inv["images"].items():
        repo, image = meta["source_repo"], meta["image"]
        local_refs: list[str] = []
        for tag in meta["tags"]:
            src = _src_ref(ctx, repo, image, tag)
            dst = _dest_ref(ctx, repo, image, tag)
            key = f"docker:{src}"
            if ctx.state.is_done(key, "published"):
                continue
            if ctx.dry_run:
                log.info("[dry-run] docker pull %s && tag -> %s && push", src, dst)
                continue
            # (b) resume across fresh runners: skip tags already on ghcr.
            if _dest_exists(ctx, dst):
                log.info("docker: %s already on ghcr — skipping", dst)
                ctx.state.set(key, "published", dest=dst)
                continue
            _ensure_login(ctx, repo)
            subprocess.run([resolve_exe("docker"), "pull", src], check=True, text=True)
            subprocess.run([resolve_exe("docker"), "tag", src, dst], check=True, text=True)
            subprocess.run([resolve_exe("docker"), "push", dst], check=True, text=True)
            ctx.state.set(key, "published", dest=dst)
            local_refs += [src, dst]
            log.info("pushed %s", dst)
        # (a) free disk before moving to the next image.
        _rmi(local_refs)


def transform(ctx: Context) -> None:
    log.info("docker: transform == retag (handled inline during publish)")


def cleanup(ctx: Context) -> None:
    """Delete the migrated container packages from ghcr.io and reset state."""
    inv = ctx.read_inventory(TYPE)
    for meta in inv["images"].values():
        # ghcr package name is the image path under the namespace: <repo>/<image>
        ctx.gh.delete_package("container", f"{meta['source_repo']}/{meta['image']}")
        for tag in meta["tags"]:
            ctx.state.clear(f"docker:{_src_ref(ctx, meta['source_repo'], meta['image'], tag)}")
    log.info("docker: rollback requested deletion of %d container package(s) "
             "(warnings above = absent/no perm)", len(inv["images"]))


def _manifest_inspect(ref: str) -> tuple[str, str, str]:
    """`docker manifest inspect <ref>` — proves the image exists without pulling it."""
    try:
        out = subprocess.run([resolve_exe("docker"), "manifest", "inspect", ref],
                             capture_output=True, text=True, timeout=120)
    except FileNotFoundError:
        return ("SKIP", f"manifest {ref}", "docker not installed")
    except Exception as exc:
        return ("SKIP", f"manifest {ref}", str(exc))
    if out.returncode == 0:
        return ("OK", f"manifest {ref}", "exists")
    reason = (out.stderr or "").strip().splitlines()
    return ("SKIP", f"manifest {ref}", f"unavailable ({reason[-1][:90] if reason else '?'})")


def verify(ctx: Context) -> None:
    """Probe Docker access without pulling layers: confirm we can read a SOURCE
    image (login + subdomain registry) and whether its ghcr DESTINATION exists."""
    inv = ctx.read_inventory(TYPE)
    sample = None
    for meta in inv["images"].values():
        if meta["tags"]:
            sample = (meta["source_repo"], meta["image"], meta["tags"][0])
            break
    if sample is None:
        report_probes(TYPE, [("SKIP", "docker image",
                              "no tags in inventory — run an authed inventory first")])
        return

    repo, image, tag = sample
    results = []
    src = _src_ref(ctx, repo, image, tag)
    try:
        _ensure_login(ctx, repo)
        results.append(_manifest_inspect(src))      # source readable? (validates subdomain + auth)
    except Exception as exc:  # noqa: BLE001
        results.append(("SKIP", f"source login {repo}", str(exc).splitlines()[-1][:90]))
    results.append(_manifest_inspect(_dest_ref(ctx, repo, image, tag)))  # ghcr destination
    report_probes(TYPE, results)
