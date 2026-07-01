"""Live trend ingestion via Claude's server-side web search tool.

No RSS feeds or third-party keys — Claude searches the web and returns current
trending topics. Needs an Anthropic key (or an `ant auth login` profile).
"""

from __future__ import annotations

import json
import re

import anthropic

from ..models import Trend
from .base import TrendSource

# web_search_20260209 (dynamic filtering) needs Opus 4.8/4.7/4.6 or Sonnet 4.6.
# Older models should use "web_search_20250305"; override via claude.web_search_tool.
DEFAULT_TOOL = "web_search_20260209"

SYSTEM = (
    "You surface current, real trends that would make engaging short-form "
    "vertical videos (TikTok/Reels/Shorts). Prefer topics with visual or "
    "narrative hooks over dry headlines."
)


def _extract_json_array(text: str) -> list:
    """Pull the first JSON array out of a text blob, tolerantly."""
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        pass
    match = re.search(r"\[.*\]", text, re.DOTALL)
    if match:
        try:
            return json.loads(match.group(0))
        except json.JSONDecodeError:
            return []
    return []


class WebSearchSource(TrendSource):
    name = "web_search"

    def available(self) -> bool:
        # Attempt even without an explicit key (an `ant` profile may exist);
        # gather_trends() catches and skips on auth failure.
        return True

    def fetch(self, context: str | None = None) -> list[Trend]:
        claude_cfg = self.cfg.claude
        model = claude_cfg.get("model", "claude-opus-4-8")
        tool_type = claude_cfg.get("web_search_tool", DEFAULT_TOOL)
        n = int(self.cfg.ingestion.get("max_trends", 8))

        client = anthropic.Anthropic()
        if context:
            user = (
                f"The user has footage about: {context}\n\n"
                f"Search the web for what's trending RIGHT NOW that this footage "
                f"could ride, and pick the {n} best angles. Prefer trends that "
                "genuinely fit the footage over generic viral topics. Respond "
                'with ONLY a JSON array of objects, each {"title": "...", '
                '"summary": "one line on how this footage fits the trend"}.'
            )
        else:
            user = (
                f"Search the web for what's trending right now and pick the {n} best "
                "for short-form videos. Respond with ONLY a JSON array of objects, "
                'each {"title": "...", "summary": "one line on the angle"}.'
            )

        messages: list[dict] = [{"role": "user", "content": user}]
        tools = [{"type": tool_type, "name": "web_search"}]

        # Server-side tools run a sampling loop; on pause_turn we resend.
        for _ in range(6):
            resp = client.messages.create(
                model=model,
                max_tokens=2000,
                system=SYSTEM,
                tools=tools,
                messages=messages,
            )
            if resp.stop_reason == "pause_turn":
                messages = [
                    {"role": "user", "content": user},
                    {"role": "assistant", "content": resp.content},
                ]
                continue
            break

        text = "".join(b.text for b in resp.content if b.type == "text")
        items = _extract_json_array(text)

        trends: list[Trend] = []
        for i, item in enumerate(items):
            if isinstance(item, str):
                title, summary = item, None
            else:
                title, summary = item.get("title", ""), item.get("summary")
            if title:
                trends.append(
                    Trend(
                        title=title.strip(),
                        source=self.name,
                        score=round(1.0 - i / max(len(items), 1), 3),
                        summary=summary,
                    )
                )
        return trends
