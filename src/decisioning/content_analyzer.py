"""Analyze uploaded footage so trends can be found *for* the content.

Claude looks at a few keyframes across the clips and returns a `ContentBrief`
(subject, niche, keywords, and a ready-to-use search query). That query is then
handed to the web-search trend source, so the trends surfaced actually relate
to what was filmed — instead of forcing the footage to fit a random trend.
"""

from __future__ import annotations

import base64
import tempfile
from pathlib import Path

import anthropic

from ..config import Config
from ..media.keyframes import extract_keyframes, sample_times
from ..models import ContentBrief, SourceClip
from ..usage import Usage, record

SYSTEM = (
    "You identify what raw video footage is about so we can match it to current "
    "trends. Judge only from the frames you are shown."
)


class ClipAnalyzer:
    def __init__(self, cfg: Config) -> None:
        self.cfg = cfg
        self.client = anthropic.Anthropic()
        cc = cfg.claude
        self.model = cc.get("model", "claude-opus-4-8")
        self.frames_per_clip = int(cc.get("analysis_frames_per_clip", 2))
        self.max_frames = int(cc.get("analysis_max_frames", 8))
        self.pricing = cc.get("pricing")
        self.ffmpeg = cfg.media.get("ffmpeg", "ffmpeg")
        self.last_usage = Usage()

    def analyze(self, clips: list[SourceClip]) -> ContentBrief:
        if not clips:
            raise ValueError("No clips to analyze.")

        with tempfile.TemporaryDirectory(prefix="analyze_") as tmp:
            content: list[dict] = [
                {
                    "type": "text",
                    "text": "Here are sample frames from a user's raw footage. "
                    "Identify what it shows and give a search query to find "
                    "currently-trending short-form topics it could ride.",
                }
            ]
            budget = self.max_frames
            for c in clips:
                if budget <= 0:
                    break
                n = min(self.frames_per_clip, budget)
                frames = extract_keyframes(
                    c.path, sample_times(c.duration, n), Path(tmp), ffmpeg=self.ffmpeg
                )
                for _, img in frames:
                    b64 = base64.standard_b64encode(img.read_bytes()).decode()
                    content.append(
                        {
                            "type": "image",
                            "source": {"type": "base64", "media_type": "image/jpeg", "data": b64},
                        }
                    )
                    budget -= 1

            resp = self.client.messages.parse(
                model=self.model,
                max_tokens=1000,
                system=[{
                    "type": "text",
                    "text": SYSTEM,
                    "cache_control": {"type": "ephemeral"},
                }],
                messages=[{"role": "user", "content": content}],  # type: ignore[typeddict-item]
                output_format=ContentBrief,
            )

        self.last_usage = record("analyze", self.model, resp.usage, self.pricing)

        brief = resp.parsed_output
        if brief is None:
            raise RuntimeError("Could not analyze the footage.")
        return brief
