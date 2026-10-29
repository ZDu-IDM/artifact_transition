"""Execution context passed to every phase of every type module."""
from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path

from .artifactory import Artifactory
from .config import Config
from .github import GitHub
from .state import State


@dataclass
class Context:
    cfg: Config
    art: Artifactory
    gh: GitHub
    state: State
    dry_run: bool
    # Optional substrings to restrict Docker images (ephemeral, for bounded runs).
    image_filter: list[str] = field(default_factory=list)

    def inventory_path(self, type_name: str) -> Path:
        return self.cfg.manifests_dir / f"{type_name}-inventory.json"

    def write_inventory(self, type_name: str, data: dict) -> Path:
        path = self.inventory_path(type_name)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(data, indent=2, sort_keys=True), encoding="utf-8")
        return path

    def read_inventory(self, type_name: str) -> dict:
        path = self.inventory_path(type_name)
        if not path.exists():
            raise FileNotFoundError(
                f"no inventory for '{type_name}' — run `inventory --type {type_name}` first")
        return json.loads(path.read_text(encoding="utf-8"))

    def staging(self, *parts: str) -> Path:
        return self.cfg.staging_dir.joinpath(*parts)
