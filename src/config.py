"""Configuration loading: merges config.yaml with environment variables."""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml
from dotenv import load_dotenv

# Load .env once at import time so os.environ is populated everywhere.
# override=True makes the project's .env authoritative, so a stale
# ANTHROPIC_API_KEY left in the OS environment can't silently shadow it.
load_dotenv(override=True)


@dataclass
class Config:
    """Parsed pipeline configuration.

    Wraps the raw dict from config.yaml with typed accessors for the sections
    the pipeline reads most, while still exposing the raw dict for the rest.
    """

    raw: dict[str, Any]
    root: Path

    # --- secrets from environment ---
    anthropic_api_key: str | None = field(default=None)
    reddit_client_id: str | None = field(default=None)
    reddit_client_secret: str | None = field(default=None)
    reddit_user_agent: str | None = field(default=None)

    @classmethod
    def load(cls, path: str | Path = "config.yaml") -> Config:
        cfg_path = Path(path)
        root = cfg_path.resolve().parent
        data = yaml.safe_load(cfg_path.read_text(encoding="utf-8")) or {}
        return cls(
            raw=data,
            root=root,
            anthropic_api_key=os.getenv("ANTHROPIC_API_KEY"),
            reddit_client_id=os.getenv("REDDIT_CLIENT_ID"),
            reddit_client_secret=os.getenv("REDDIT_CLIENT_SECRET"),
            reddit_user_agent=os.getenv("REDDIT_USER_AGENT"),
        )

    # Convenience section accessors.
    @property
    def claude(self) -> dict[str, Any]:
        return self.raw.get("claude", {})

    @property
    def ingestion(self) -> dict[str, Any]:
        return self.raw.get("ingestion", {})

    @property
    def media(self) -> dict[str, Any]:
        return self.raw.get("media", {})

    @property
    def render(self) -> dict[str, Any]:
        return self.raw.get("render", {})

    def path(self, value: str) -> Path:
        """Resolve a config path relative to the project root."""
        p = Path(value)
        return p if p.is_absolute() else self.root / p
