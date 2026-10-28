#!/usr/bin/env python3
"""Artifactory -> GitHub migration CLI.

Usage examples
--------------
  python migrate.py inventory --type all          # read-only; reconcile counts first
  python migrate.py inventory --type pypi
  python migrate.py download  --type pypi --dry-run
  python migrate.py publish   --type pypi         # download -> transform -> publish
  python migrate.py run       --type all          # full pipeline (download->publish->verify)
  python migrate.py verify    --type all

Phases: inventory | download | transform | publish | verify | run
Types:  pypi | docker | npm | data | all
"""
from __future__ import annotations

import argparse
import datetime as _dt
import sys

from artmig import data, docker, npm, pypi
from artmig.artifactory import Artifactory
from artmig.config import load_config
from artmig.context import Context
from artmig.github import GitHub
from artmig.state import State
from artmig.util import add_file_log, get_logger

log = get_logger()

MODULES = {"pypi": pypi, "docker": docker, "npm": npm, "data": data}
PHASES = ["inventory", "download", "transform", "publish", "verify"]


def _types(arg: str, cfg) -> list[str]:
    if arg == "all":
        return [t for t in MODULES if cfg.type_enabled(t)]
    if arg not in MODULES:
        sys.exit(f"unknown type '{arg}' (choose: {', '.join(MODULES)}, all)")
    return [arg]


def _run_phase(ctx: Context, type_name: str, phase: str) -> None:
    mod = MODULES[type_name]
    log.info("=== %s : %s ===", type_name, phase)
    if phase == "inventory":
        data_ = mod.inventory(ctx)
        path = ctx.write_inventory(type_name, data_)
        log.info("wrote %s", path)
    else:
        getattr(mod, phase)(ctx)


def main() -> None:
    parser = argparse.ArgumentParser(description="Artifactory -> GitHub migration")
    parser.add_argument("phase", choices=[*PHASES, "run"])
    parser.add_argument("--type", default="all", help="pypi|docker|npm|data|all")
    parser.add_argument("--config", default="migration.yaml")
    parser.add_argument("--dry-run", action="store_true",
                        help="log mutating actions without executing them")
    args = parser.parse_args()

    cfg = load_config(args.config)
    ts = _dt.datetime.now().strftime("%Y%m%d-%H%M%S")
    add_file_log(cfg.manifests_dir / f"migration-{args.phase}-{ts}.log")

    # Credential sanity (inventory/download/publish all need Artifactory read).
    if not cfg.artifactory.has_credentials:
        log.warning("No ARTIFACTORY_TOKEN set — anonymous access may fail for downloads "
                    "and the Docker catalog.")

    art = Artifactory(cfg)
    gh = GitHub(cfg.github_slug, dry_run=args.dry_run)
    state = State(cfg.manifests_dir / "state.json")
    ctx = Context(cfg=cfg, art=art, gh=gh, state=state, dry_run=args.dry_run)

    types = _types(args.type, cfg)
    phases = PHASES if args.phase == "run" else [args.phase]
    # 'publish' implies download+transform first for a one-shot push.
    if args.phase == "publish":
        phases = ["download", "transform", "publish"]

    for type_name in types:
        for phase in phases:
            try:
                _run_phase(ctx, type_name, phase)
            except Exception as exc:  # noqa: BLE001
                log.error("%s:%s failed: %s", type_name, phase, exc)
                if args.phase != "run":
                    raise
    log.info("done.")


if __name__ == "__main__":
    main()
