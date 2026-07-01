"""Reddit ingestion via the free OAuth API (script app credentials).

Uses the client-credentials grant, which needs only a client id + secret from
a "script" type app at https://www.reddit.com/prefs/apps.
"""

from __future__ import annotations

import httpx

from ..models import Trend
from .base import TrendSource

TOKEN_URL = "https://www.reddit.com/api/v1/access_token"
API_BASE = "https://oauth.reddit.com"


class RedditSource(TrendSource):
    name = "reddit"

    def available(self) -> bool:
        return bool(self.cfg.reddit_client_id and self.cfg.reddit_client_secret)

    def _token(self) -> str:
        ua = self.cfg.reddit_user_agent or "video-editing-tool/0.1"
        resp = httpx.post(
            TOKEN_URL,
            auth=(self.cfg.reddit_client_id, self.cfg.reddit_client_secret),
            data={"grant_type": "client_credentials"},
            headers={"User-Agent": ua},
            timeout=15,
        )
        resp.raise_for_status()
        return resp.json()["access_token"]

    def fetch(self) -> list[Trend]:
        conf = self.cfg.ingestion.get("reddit", {})
        subreddits = conf.get("subreddits", ["popular"])
        limit = int(conf.get("limit", 10))
        ua = self.cfg.reddit_user_agent or "video-editing-tool/0.1"

        token = self._token()
        headers = {"Authorization": f"Bearer {token}", "User-Agent": ua}

        trends: list[Trend] = []
        with httpx.Client(headers=headers, timeout=15) as client:
            for sub in subreddits:
                resp = client.get(
                    f"{API_BASE}/r/{sub}/hot", params={"limit": limit}
                )
                resp.raise_for_status()
                for child in resp.json().get("data", {}).get("children", []):
                    post = child.get("data", {})
                    trends.append(
                        Trend(
                            title=post.get("title", "").strip(),
                            source=f"{self.name}:{sub}",
                            score=float(post.get("score", 0)),
                            url="https://reddit.com" + post.get("permalink", ""),
                            summary=(post.get("selftext") or "")[:500] or None,
                        )
                    )
        return trends
