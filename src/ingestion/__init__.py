"""Trend ingestion: pluggable sources that surface trending topics."""

from __future__ import annotations

from ..config import Config
from ..models import Trend
from .base import TrendSource
from .google_trends import GoogleTrendsSource
from .local import LocalTopicsSource
from .reddit import RedditSource
from .web_search import WebSearchSource

_REGISTRY: dict[str, type[TrendSource]] = {
    "web_search": WebSearchSource,
    "google_trends": GoogleTrendsSource,
    "reddit": RedditSource,
    "local": LocalTopicsSource,
}


def gather_trends(cfg: Config) -> list[Trend]:
    """Run every configured source in order and return a merged, capped list."""
    ing = cfg.ingestion
    names = ing.get("sources", ["local"])
    max_trends = int(ing.get("max_trends", 8))

    trends: list[Trend] = []
    seen: set[str] = set()
    for name in names:
        source_cls = _REGISTRY.get(name)
        if source_cls is None:
            print(f"[ingest] unknown source '{name}', skipping")
            continue
        source = source_cls(cfg)
        if not source.available():
            print(f"[ingest] source '{name}' unavailable (missing creds?), skipping")
            continue
        try:
            for trend in source.fetch():
                key = trend.title.strip().lower()
                if key and key not in seen:
                    seen.add(key)
                    trends.append(trend)
        except Exception as exc:  # noqa: BLE001 - one bad source shouldn't kill the run
            print(f"[ingest] source '{name}' failed: {exc}")

    return trends[:max_trends]


__all__ = ["TrendSource", "gather_trends"]
