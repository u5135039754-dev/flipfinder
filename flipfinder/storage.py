"""Remembers which items were already checked so you don't get the same alert twice."""

from __future__ import annotations

import json
import time
from pathlib import Path

KEEP_DAYS = 14


class SeenStore:
    def __init__(self, path: Path):
        self.path = path
        self.data: dict[str, float] = {}
        if path.exists():
            try:
                self.data = json.loads(path.read_text(encoding="utf-8"))
            except (json.JSONDecodeError, OSError):
                self.data = {}
        # Keys are "platform:id" so IDs from different platforms can't clash.
        # Older files only had Vinted, stored as bare IDs.
        self.data = {(k if ":" in k else f"vinted:{k}"): v for k, v in self.data.items()}

    @property
    def is_empty(self) -> bool:
        return not self.data

    def has_platform(self, source: str) -> bool:
        return any(k.startswith(source + ":") for k in self.data)

    def __contains__(self, key: str) -> bool:
        return key in self.data

    def add(self, key: str):
        self.data[key] = time.time()

    def save(self):
        cutoff = time.time() - KEEP_DAYS * 86400
        self.data = {k: v for k, v in self.data.items() if v >= cutoff}
        self.path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.path.with_suffix(".tmp")
        tmp.write_text(json.dumps(self.data), encoding="utf-8")
        tmp.replace(self.path)
