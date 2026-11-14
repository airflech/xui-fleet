"""Local secrets the panels only show once (API tokens) plus SSH host keys.

Everything else is read back from the master panel on every run, so losing this
file is recoverable: tokens are simply re-minted over SSH.
"""
from __future__ import annotations

import json
import os
import threading
from pathlib import Path
from typing import Any, Dict, Optional


class State:
    def __init__(self, path: Path):
        self.path = path
        self._lock = threading.Lock()
        self.data: Dict[str, Any] = {"servers": {}}
        if path.exists():
            self.data = json.loads(path.read_text(encoding="utf-8"))
            self.data.setdefault("servers", {})

    def server(self, name: str) -> Dict[str, Any]:
        with self._lock:
            return dict(self.data["servers"].get(name, {}))

    def update(self, name: str, **fields: Any) -> None:
        with self._lock:
            entry = self.data["servers"].setdefault(name, {})
            entry.update(fields)
            self._save()

    def forget(self, name: str) -> None:
        with self._lock:
            self.data["servers"].pop(name, None)
            self._save()

    def drop(self, name: str, key: str) -> None:
        with self._lock:
            self.data["servers"].get(name, {}).pop(key, None)
            self._save()

    def get(self, name: str, key: str) -> Optional[Any]:
        return self.server(name).get(key)

    def _save(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.path.with_suffix(".tmp")
        fd = os.open(str(tmp), os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            json.dump(self.data, fh, indent=2, ensure_ascii=False)
        os.replace(str(tmp), str(self.path))
