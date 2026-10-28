"""Live repository discovery + coverage gate.

Guarantees the migration covers every LOCAL (IDM-owned) repository that
Artifactory holds *right now* — not just what a point-in-time summary listed.
REMOTE repos (caches of public registries) and VIRTUAL repos (aggregate views)
are not IDM-owned data and are intentionally excluded.

`discover` lists the LOCAL repos and FAILS if any isn't handled by the migration
config, so a newly added repo (or an unsupported package type) can never be
silently dropped.
"""
from __future__ import annotations

from .context import Context
from .util import get_logger

log = get_logger()

# Artifactory packageType -> our handler/type name.
_HANDLER = {"Pypi": "pypi", "Docker": "docker", "Npm": "npm", "Generic": "data"}


def list_local_repos(ctx: Context) -> list[dict]:
    """All LOCAL repositories (key + packageType), sorted by key."""
    repos = ctx.art.get_json(f"{ctx.art.base}/api/repositories")
    return sorted((r for r in repos if r.get("type") == "LOCAL"),
                  key=lambda r: r.get("key", ""))


def _covered_repos(ctx: Context) -> dict[str, str]:
    """repo key -> handler type, as declared in migration.yaml."""
    out: dict[str, str] = {}
    types = ctx.cfg.types
    for name in ("pypi", "npm", "data"):
        repo = types.get(name, {}).get("repo")
        if repo:
            out[repo] = name
    for repo in types.get("docker", {}).get("repos", []):
        out[repo] = "docker"
    return out


def coverage(ctx: Context) -> dict:
    """Cross-reference live LOCAL repos against the config. Returns rows + gaps."""
    covered = _covered_repos(ctx)
    rows, uncovered = [], []
    for r in list_local_repos(ctx):
        key, ptype = r["key"], r.get("packageType", "?")
        handler = _HANDLER.get(ptype)
        if key in covered:
            status = "covered"
        elif handler is None:
            status = f"UNCOVERED — unsupported package type ({ptype})"
            uncovered.append(key)
        else:
            status = f"UNCOVERED — add to types.{handler} in migration.yaml"
            uncovered.append(key)
        rows.append({"repo": key, "type": ptype, "handler": handler, "status": status})
    return {"local": rows, "uncovered": uncovered, "covered_count": len(covered)}


def run(ctx: Context) -> None:
    """Print the coverage audit; raise if any LOCAL repo is uncovered."""
    cov = coverage(ctx)
    log.info("LOCAL (IDM-owned) repositories — coverage audit:")
    for row in cov["local"]:
        mark = "[MISS]" if row["status"].startswith("UNCOVERED") else "[ OK ]"
        log.info("  %s %-26s %-8s %s", mark, row["repo"], row["type"], row["status"])
    if cov["uncovered"]:
        raise RuntimeError(
            f"{len(cov['uncovered'])} LOCAL repo(s) not covered by the migration: "
            f"{', '.join(cov['uncovered'])}. Add them to migration.yaml.")
    log.info("All %d LOCAL repositories are covered.", len(cov["local"]))
