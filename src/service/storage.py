"""Output storage abstraction.

`LocalStorage` keeps rendered files on disk (single node). For horizontal scale,
implement the same `Storage` protocol over S3/MinIO (upload on save, presigned
URL on `url_for`) and swap it in `api.py` — nothing else changes.
"""

from __future__ import annotations

import shutil
from pathlib import Path
from typing import Protocol


class Storage(Protocol):
    def save(self, local_path: str | Path, key: str) -> str:
        """Persist a rendered file under `key`; return a locator (path or URL)."""
        ...

    def path_for(self, key: str) -> Path | None:
        """Local filesystem path for `key`, or None if it isn't downloadable
        locally (e.g. a remote object — serve via redirect instead)."""
        ...


class LocalStorage:
    def __init__(self, root: str | Path) -> None:
        self.root = Path(root)
        self.root.mkdir(parents=True, exist_ok=True)

    def save(self, local_path: str | Path, key: str) -> str:
        dest = self.root / key
        dest.parent.mkdir(parents=True, exist_ok=True)
        src = Path(local_path)
        if src.resolve() != dest.resolve():
            shutil.copy2(src, dest)
        return str(dest)

    def path_for(self, key: str) -> Path | None:
        p = self.root / key
        return p if p.exists() else None
