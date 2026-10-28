"""Load migration.yaml and resolve credentials from the environment."""
from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml


@dataclass
class ArtifactoryAuth:
    base_url: str
    docker_registry: str
    user: str | None
    token: str | None

    @property
    def has_credentials(self) -> bool:
        return bool(self.token)


@dataclass
class Config:
    raw: dict[str, Any]
    root: Path                     # repo root (where migration.yaml lives)
    github_owner: str
    github_repo: str
    namespace: str
    pages_url: str
    artifactory: ArtifactoryAuth
    types: dict[str, Any] = field(default_factory=dict)
    options: dict[str, Any] = field(default_factory=dict)

    # ---- convenience paths ----
    @property
    def staging_dir(self) -> Path:
        return self.root / self.options.get("staging_dir", "staging")

    @property
    def manifests_dir(self) -> Path:
        return self.root / self.options.get("manifests_dir", "manifests")

    @property
    def docs_dir(self) -> Path:
        return self.root / self.options.get("docs_dir", "docs")

    @property
    def github_slug(self) -> str:
        return f"{self.github_owner}/{self.github_repo}"

    def type_enabled(self, name: str) -> bool:
        return bool(self.types.get(name, {}).get("enabled"))


def load_config(path: str | Path = "migration.yaml") -> Config:
    path = Path(path).resolve()
    with path.open("r", encoding="utf-8") as fh:
        raw = yaml.safe_load(fh)

    gh = raw.get("github", {})
    art = raw.get("artifactory", {})

    artifactory = ArtifactoryAuth(
        base_url=os.environ.get("ARTIFACTORY_URL", art.get("base_url", "")).rstrip("/"),
        docker_registry=art.get("docker_registry", "packages.idmod.org"),
        user=os.environ.get("ARTIFACTORY_USER"),
        token=os.environ.get("ARTIFACTORY_TOKEN"),
    )

    return Config(
        raw=raw,
        root=path.parent,
        github_owner=gh.get("owner", ""),
        github_repo=gh.get("repo", ""),
        namespace=gh.get("namespace", gh.get("owner", "")).lower(),
        pages_url=gh.get("pages_url", "").rstrip("/"),
        artifactory=artifactory,
        types=raw.get("types", {}),
        options=raw.get("options", {}),
    )
