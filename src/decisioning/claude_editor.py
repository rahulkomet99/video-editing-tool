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
from ..usage import Usage, record

SYSTEM_PROMPT = """\
You are a senior short-form video editor (TikTok / Reels / Shorts). You cut \
raw footage into a fast, scroll-stopping vertical (9:16) edit. Your edits feel \
DIRECTED — constant motion, tight pacing, punchy text — never a passive clip \
with a caption slapped on.

You are shown sample frames from each clip with the timestamp each was taken \
at. USE THEM: cut around the most visually interesting moments, order cuts for \
momentum, and make every caption match what's actually on screen.

PACING — this is what separates a real edit from a raw clip:
- Cut often. Aim for roughly 6-12 cuts, most of them 1.5-3s long. Short clips \
should be jump-cut into several beats, not left as one long shot.
- Front-load the payoff: the strongest 1-2 seconds go FIRST as the hook.
- Total runtime 15-35s.

MOTION — keep almost every shot alive:
- Set `zoom` on most cuts. Alternate 'in' (build/reveal, push on a subject) and \
'out' (open a scene). Only leave 'none' when a shot already has strong movement.
- Use `speed` (0.5-2.0) deliberately: slow-mo (0.5-0.7) on a reveal or reaction, \
speed-up (1.5-2.0) to blow through dead time or setup.
- `transition`: 'none' for hard, high-energy cuts (default for punchy content); \
'crossfade' only for a smoother, moodier flow.

COLOR / LOOK — grade to match the vibe:
- Set the video-level `look` to one of: 'clean' (safe, slightly punched), \
'vibrant' (bright, saturated — great for products/food/energy), 'cinematic' \
(contrast + teal-orange + vignette), 'warm', 'cool', 'moody' (dark, moody), \
'vintage' (retro), 'bw' (black & white), or 'none'. Choose what fits the trend.
- Optionally override a single shot with a per-cut `look` for contrast (e.g. one \
'bw' flashback in a 'vibrant' edit).
- Fine-tune sparingly with per-cut `contrast`/`saturation`/`brightness` only when \
a specific shot needs it — otherwise leave them null and trust the look.

TEXT — layer it, don't just caption:
- `text_overlays` (timeline text, separate from per-cut captions): open with ONE \
bold hero TITLE at ~0s (size 90-120), then add 2-4 short callouts at key beats \
(size 56-72). Each needs `start`/`end` in TIMELINE seconds (finished length ≈ \
sum of cut lengths ÷ speed) and x/y (0..1; y≈0.15 top, 0.5 center, 0.85 bottom). \
Keep `box` false for the clean outlined look. Keep every line 2-5 words — long \
lines run off a 1080-wide frame.
- Per-cut `caption`: short, punchy, ALL CAPS energy, 2-5 words. Add one to most \
cuts to carry the story; leave it null on cuts that already have a big overlay.
- Leave `image_overlays` empty — the app adds logos.

HARD RULES:
- Only cut from the source clips provided; use their exact `path` values.
- Never set a cut's `end` beyond that clip's `duration`; keep `start` < `end`.
- It's fine (and good) to reuse the same clip for multiple cuts at different \
in/out points to build rhythm.
- Return concise, platform-appropriate hashtags (no leading '#').
- Fill `rationale` with a one-line note on the edit's structure and energy.
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
        self.pricing = claude_cfg.get("pricing")
        self.ffmpeg = cfg.media.get("ffmpeg", "ffmpeg")
        self.last_usage = Usage()  # tokens from the most recent decide()

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

        # Mark the end of the (large) frame prefix as a cache breakpoint. On a
        # repeat call with identical footage — a revise loop or a batch of
        # variants from the same clips — this prefix is a cache read (~0.1x),
        # instead of re-sending every frame at full price.
        if len(content) > 1:
            content[-1]["cache_control"] = {"type": "ephemeral"}

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
                system=[{
                    "type": "text",
                    "text": SYSTEM_PROMPT,
                    "cache_control": {"type": "ephemeral"},
                }],
                messages=[{"role": "user", "content": content}],  # type: ignore[typeddict-item]
                output_format=EditDecisionList,
            )

        self.last_usage = record("edit", self.model, response.usage, self.pricing)

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

        # Keep timeline text overlays inside the (approx) video length; drop
        # any that start past the end. image_overlays are app-managed.
        total = sum((c.end - c.start) / max(0.5, min(c.speed, 2.0)) for c in valid)
        overlays = []
        for ov in edl.text_overlays:
            if ov.start >= total:
                continue
            ov.end = min(ov.end, total)
            if ov.end - ov.start >= 0.3:
                overlays.append(ov)
        edl.text_overlays = overlays
        edl.image_overlays = []
        return edl
