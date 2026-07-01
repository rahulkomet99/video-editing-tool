"""Local topics ingestion: reads data/topics.json.

Always-available fallback so the pipeline runs with no external services.
Expected format:
  [
    {"title": "...", "summary": "...", "score": 1.0},
    "a bare string is also fine"
  ]
"""

from __future__ import annotations

import json
from pathlib import Path

from ..models import Trend
from .base import TrendSource


class LocalTopicsSource(TrendSource):
    name = "local"

    def _file(self) -> Path:
        return self.cfg.path("data/topics.json")

    def available(self) -> bool:
        return self._file().exists()

    def fetch(self) -> list[Trend]:
        raw = json.loads(self._file().read_text(encoding="utf-8"))
        trends: list[Trend] = []
        for item in raw:
            if isinstance(item, str):
                trends.append(Trend(title=item, source=self.name, score=1.0))
            else:
                trends.append(
                    Trend(
                        title=item["title"],
                        source=self.name,
                        score=float(item.get("score", 1.0)),
                        url=item.get("url"),
                        summary=item.get("summary"),
                    )
                )
        return trends
