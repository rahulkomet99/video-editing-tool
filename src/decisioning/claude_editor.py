"""Claude-driven edit decisioning — now with vision.

Given a trend and the available source clips, ask Claude to produce an
`EditDecisionList`. Instead of editing blind from metadata alone, we extract a
handful of keyframes per clip with FFmpeg and pass them as images, so Claude
can see what's in the footage and choose meaningful cut points and captions
that match what's on screen.

Uses `client.messages.parse()` with structured outputs so the response is
validated against the Pydantic schema — no manual JSON parsing.
"""

from __future__ import annotations

import base64
import json
import tempfile
from pathlib import Path

import anthropic

from ..config import Config
from ..media.keyframes import extract_keyframes, sample_times
from ..models import EditDecisionList, SourceClip, Trend

SYSTEM_PROMPT = """\
You are a short-form video editor. You turn a trending topic and a set of \
source clips into a tight, engaging vertical (9:16) edit for platforms like \
TikTok, Reels, and Shorts.

You are shown sample frames from each clip (with the timestamp each was taken \
at). USE THEM: pick cut points around the most visually interesting moments \
you can see, order cuts for momentum, and write captions that match what is \
actually on screen — not generic filler.

You also direct motion and pacing. For each cut you may set:
- `zoom`: 'in' to build toward a reveal or draw the eye, 'out' to open up a \
scene, 'none' to hold steady. Use zoom purposefully, not on every cut.
- `speed`: 0.5-2.0. Slow a beat down to savor it, or speed up dead time.
And for the whole video, `transition`: 'crossfade' for a smooth, polished flow \
or 'none' for hard cuts (punchier, higher energy). Pick what fits the trend.

Rules for the edit you produce:
- Only cut from the source clips provided. Use their exact `path` values.
- Never set a cut's `end` beyond that clip's `duration`, and keep `start` < `end`.
- Infer good cut points from the frames: the sampled timestamps tell you what \
the footage looks like around each moment.
- Keep the whole video punchy: aim for 15-40 seconds total across all cuts.
- Open with a strong hook. Write short, high-energy captions (a few words).
- Use zoom/speed/transition to make it feel edited, but keep it tasteful.
- Return concise, platform-appropriate hashtags (no leading '#').
"""


class ClaudeEditor:
    def __init__(self, cfg: Config) -> None:
        self.cfg = cfg
        # Zero-arg client resolves ANTHROPIC_API_KEY or an `ant auth login` profile.
        self.client = anthropic.Anthropic()
        claude_cfg = cfg.claude
        self.model = claude_cfg.get("model", "claude-opus-4-8")
        self.effort = claude_cfg.get("effort", "high")
        self.max_tokens = int(claude_cfg.get("max_tokens", 8000))
        self.keyframes_per_clip = int(claude_cfg.get("keyframes_per_clip", 4))
        self.ffmpeg = cfg.media.get("ffmpeg", "ffmpeg")

    def _build_content(
        self, trend: Trend, clips: list[SourceClip], frame_dir: Path
    ) -> list[dict]:
        """Assemble the user message: trend + per-clip metadata and keyframes."""
        clip_meta = [
            {
                "path": c.path,
                "duration": c.duration,
                "resolution": f"{c.width}x{c.height}",
                "has_audio": c.has_audio,
            }
            for c in clips
        ]
        content: list[dict] = [
            {
                "type": "text",
                "text": (
                    "Create one short-form video edit for this trend.\n\n"
                    f"TREND:\n{json.dumps(trend.model_dump(), indent=2)}\n\n"
                    f"AVAILABLE SOURCE CLIPS (metadata):\n"
                    f"{json.dumps(clip_meta, indent=2)}\n\n"
                    "Sample frames for each clip follow."
                ),
            }
        ]

        if self.keyframes_per_clip > 0:
            for c in clips:
                frames = extract_keyframes(
                    c.path,
                    sample_times(c.duration, self.keyframes_per_clip),
                    out_dir=frame_dir,
                    ffmpeg=self.ffmpeg,
                )
                ts = ", ".join(f"{t}s" for t, _ in frames) or "none"
                content.append(
                    {
                        "type": "text",
                        "text": f"=== CLIP: {c.path} | {c.duration}s | frames at: {ts} ===",
                    }
                )
                for _, img_path in frames:
                    b64 = base64.standard_b64encode(img_path.read_bytes()).decode()
                    content.append(
                        {
                            "type": "image",
                            "source": {
                                "type": "base64",
                                "media_type": "image/jpeg",
                                "data": b64,
                            },
                        }
                    )

        content.append(
            {
                "type": "text",
                "text": "Now produce the edit decision list that best fits the trend.",
            }
        )
        return content

    def decide(self, trend: Trend, clips: list[SourceClip]) -> EditDecisionList:
        """Return a validated EditDecisionList for one trend."""
        if not clips:
            raise ValueError("No source clips available to edit.")

        with tempfile.TemporaryDirectory(prefix="edit_frames_") as tmp:
            content = self._build_content(trend, clips, Path(tmp))
            response = self.client.messages.parse(
                model=self.model,
                max_tokens=self.max_tokens,
                thinking={"type": "adaptive"},
                output_config={"effort": self.effort},
                system=SYSTEM_PROMPT,
                messages=[{"role": "user", "content": content}],
                output_format=EditDecisionList,
            )

        edl = response.parsed_output
        if edl is None:
            raise RuntimeError(
                f"Claude did not return a valid edit (stop_reason={response.stop_reason})."
            )
        return self._clamp(edl, clips)

    @staticmethod
    def _clamp(edl: EditDecisionList, clips: list[SourceClip]) -> EditDecisionList:
        """Defensive guard: drop/repair cuts that exceed clip bounds."""
        by_path = {c.path: c for c in clips}
        valid = []
        for cut in edl.cuts:
            clip = by_path.get(cut.clip_path)
            if clip is None:
                continue
            start = max(0.0, min(cut.start, clip.duration))
            end = max(start + 0.1, min(cut.end, clip.duration))
            if end - start < 0.1:
                continue
            cut.start, cut.end = round(start, 3), round(end, 3)
            valid.append(cut)
        if not valid:
            raise RuntimeError("No valid cuts remained after clamping to clip bounds.")
        edl.cuts = valid
        return edl
