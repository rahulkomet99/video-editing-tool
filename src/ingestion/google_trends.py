"""Google Trends ingestion via the public Daily Trends RSS feed.

Free, no API key. Feed URL:
  https://trends.google.com/trends/trendingsearches/daily/rss?geo=US
"""

from __future__ import annotations

import feedparser

from ..models import Trend
from .base import TrendSource

RSS_URL = "https://trends.google.com/trends/trendingsearches/daily/rss?geo={geo}"


class GoogleTrendsSource(TrendSource):
    name = "google_trends"

    def fetch(self) -> list[Trend]:
        geo = self.cfg.ingestion.get("google_trends", {}).get("geo", "US")
        feed = feedparser.parse(RSS_URL.format(geo=geo))

        trends: list[Trend] = []
        total = len(feed.entries) or 1
        for i, entry in enumerate(feed.entries):
            # Google lists entries roughly by rank; derive a descending score.
            score = round(1.0 - i / total, 3)
            summary = getattr(entry, "summary", None) or getattr(
                entry, "ht_news_item_title", None
            )
            trends.append(
                Trend(
                    title=entry.title,
                    source=self.name,
                    score=score,
                    url=getattr(entry, "link", None),
                    summary=summary,
                )
            )
        return trends
