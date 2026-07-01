"""Data models shared across the pipeline.

The `EditDecisionList` (EDL) is the contract between the Claude decisioning
step and the FFmpeg renderer: Claude produces it, the renderer consumes it.
It is defined as a Pydantic model so `client.messages.parse()` can validate
Claude's output against it directly.
"""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, Field

# Named color looks the renderer knows how to build (see ffmpeg_renderer.LOOKS).
# "none" = leave the footage untouched.
Look = Literal[
    "none", "clean", "vibrant", "cinematic", "warm", "cool", "moody",
    "vintage", "bw",
]


class Trend(BaseModel):
    """A trending topic surfaced by an ingestion source."""

    title: str
    source: str  # e.g. "google_trends", "reddit", "local"
    score: float = 0.0  # relative popularity, source-defined
    url: str | None = None
    summary: str | None = None


class ContentBrief(BaseModel):
    """What the uploaded footage is actually about — derived by Claude from
    sample frames, used to find trends *related* to the content."""

    subject: str = Field(..., description="One line: what the footage shows.")
    niche: str = Field(..., description="Category/niche, e.g. 'DIY robotics'.")
    keywords: list[str] = Field(default_factory=list)
    search_query: str = Field(
        ..., description="A query to find currently-trending short-form topics "
        "this footage could ride."
    )


class SourceClip(BaseModel):
    """A source media file available to the editor, with probed metadata."""

    path: str
    duration: float  # seconds
    width: int
    height: int
    has_audio: bool = False


class Cut(BaseModel):
    """A single trimmed segment taken from one source clip."""

    clip_path: str = Field(..., description="Path to the source clip to cut from.")
    start: float = Field(..., ge=0, description="In-point in the source, seconds.")
    end: float = Field(..., gt=0, description="Out-point in the source, seconds.")
    caption: str | None = Field(
        None, description="Optional on-screen caption burned over this cut."
    )
    zoom: Literal["none", "in", "out"] = Field(
        "none",
        description="Ken Burns motion: slow zoom 'in' (build tension/reveal), "
        "'out' (open up a scene), or 'none' for a static shot.",
    )
    speed: float = Field(
        1.0,
        ge=0.5,
        le=2.0,
        description="Playback speed multiplier (0.5=slow-mo, 2.0=fast). Use "
        "sparingly to punch up slow moments or savor a beat.",
    )
    look: Look | None = Field(
        None,
        description="Color grade for THIS cut; overrides the video-level look. "
        "Leave null to inherit the video look.",
    )
    contrast: float | None = Field(
        None, ge=0.5, le=2.0, description="Fine contrast tweak on top of the "
        "look (1.0 = unchanged). Optional.",
    )
    saturation: float | None = Field(
        None, ge=0.0, le=2.0, description="Fine saturation tweak (1.0 = "
        "unchanged, 0 = grayscale). Optional.",
    )
    brightness: float | None = Field(
        None, ge=-0.3, le=0.3, description="Fine brightness tweak (0 = "
        "unchanged). Optional.",
    )


class TextOverlay(BaseModel):
    """A free-floating text element placed on the output timeline — a title,
    lower-third, or callout that can span multiple cuts. Independent of the
    per-cut `caption` (which is tied to its own cut)."""

    text: str
    start: float = Field(..., ge=0, description="Timeline in-point, seconds.")
    end: float = Field(..., gt=0, description="Timeline out-point, seconds.")
    x: float = Field(0.5, ge=0, le=1, description="Center X, 0=left..1=right.")
    y: float = Field(0.18, ge=0, le=1, description="Center Y, 0=top..1=bottom.")
    size: int = Field(
        84, description="Font size in px (frame is 1080 wide). Hero titles "
        "80-120, callouts 56-72.",
    )
    color: str = Field("white", description="Font color name or #hex.")
    box: bool = Field(
        False,
        description="Solid box behind text. Leave off for the clean outlined "
        "look; turn on only for a deliberate title-card block.",
    )
    animate: Literal["none", "fade"] = "fade"


class ImageOverlay(BaseModel):
    """A logo / watermark / sticker composited on the timeline. Paths are set
    by the app (uploads/config), not authored by the model."""

    path: str
    start: float = 0.0
    end: float = Field(1e9, description="Timeline out-point; default = whole video.")
    x: float = Field(0.85, ge=0, le=1)
    y: float = Field(0.08, ge=0, le=1)
    scale: float = Field(0.18, gt=0, le=1, description="Width as fraction of frame.")
    opacity: float = Field(1.0, ge=0, le=1)


class EditDecisionList(BaseModel):
    """Claude's edit plan for one short-form video."""

    title: str = Field(..., description="Short, catchy title for the output video.")
    hook: str = Field(..., description="Opening line/caption to grab attention.")
    aspect_ratio: Literal["9:16", "1:1", "16:9"] = "9:16"
    look: Look = Field(
        "clean",
        description="Default color grade for the whole video. Pick one that "
        "matches the mood/trend; per-cut `look` can override individual shots.",
    )
    cuts: list[Cut] = Field(..., description="Ordered segments (the main track).")
    transition: Literal["none", "crossfade"] = Field(
        "crossfade",
        description="How consecutive cuts join: 'crossfade' for smooth flow, "
        "'none' for hard cuts (punchier, more energetic).",
    )
    text_overlays: list[TextOverlay] = Field(
        default_factory=list,
        description="Timeline text (title card, lower-thirds, callouts) layered "
        "over the video. Use for a bold opening title and 1-2 key callouts; keep "
        "per-cut captions for moment-by-moment text.",
    )
    image_overlays: list[ImageOverlay] = Field(
        default_factory=list,
        description="Logos/stickers — set by the app, leave empty.",
    )
    hashtags: list[str] = Field(default_factory=list)
    rationale: str | None = Field(
        None, description="Why this edit fits the trend (not rendered)."
    )


class RenderResult(BaseModel):
    """Outcome of rendering an EDL to a file."""

    output_path: str
    edl: EditDecisionList
    trend: Trend
