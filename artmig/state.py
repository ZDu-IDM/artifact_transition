"""Resume state: a small JSON of per-item status so reruns skip finished work."""
from __future__ import annotations

import json
from pathlib import Path
from typing import Literal

Status = Literal["pending", "downloaded", "published", "verified", "failed", "skipped"]


class State:
    """Keyed by a stable item id (e.g. 'pypi:emodpy:emodpy-2.1.16-py3-none-any.whl')."""

    def __init__(self, path: Path):
        self.path = path
        self._data: dict[str, dict] = {}
        if path.exists():
            self._data = json.loads(path.read_text(encoding="utf-8"))

    def get(self, key: str) -> Status | None:
        rec = self._data.get(key)
        return rec["status"] if rec else None

    def is_done(self, key: str, at_least: Status = "published") -> bool:
        order = ["pending", "downloaded", "published", "verified"]
        cur = self.get(key)
        if cur is None or cur in ("failed", "skipped"):
            return cur == "skipped"
        return order.index(cur) >= order.index(at_least)

    def set(self, key: str, status: Status, **extra) -> None:
        self._data[key] = {"status": status, **extra}
        self.save()

    def clear(self, key: str) -> None:
        """Forget an item so a later phase re-runs it (used by cleanup/rollback)."""
        if key in self._data:
            del self._data[key]
            self.save()

    def save(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.path.write_text(json.dumps(self._data, indent=2, sort_keys=True),
                             encoding="utf-8")
