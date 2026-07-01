"""Data models shared across the pipeline.

The `EditDecisionList` (EDL) is the contract between the Claude decisioning
step and the FFmpeg renderer: Claude produces it, the renderer consumes it.
It is defined as a Pydantic model so `client.messages.parse()` can validate
Claude's output against it directly.
"""

from __future__ import annotations

from typing import Literal, Optional

from pydantic import BaseModel, Field


class Trend(BaseModel):
    """A trending topic surfaced by an ingestion source."""

    title: str
    source: str  # e.g. "google_trends", "reddit", "local"
    score: float = 0.0  # relative popularity, source-defined
    url: Optional[str] = None
    summary: Optional[str] = None


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
    caption: Optional[str] = Field(
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


class EditDecisionList(BaseModel):
    """Claude's edit plan for one short-form video."""

    title: str = Field(..., description="Short, catchy title for the output video.")
    hook: str = Field(..., description="Opening line/caption to grab attention.")
    aspect_ratio: Literal["9:16", "1:1", "16:9"] = "9:16"
    cuts: list[Cut] = Field(..., description="Ordered segments.")
    transition: Literal["none", "crossfade"] = Field(
        "crossfade",
        description="How consecutive cuts join: 'crossfade' for smooth flow, "
        "'none' for hard cuts (punchier, more energetic).",
    )
    hashtags: list[str] = Field(default_factory=list)
    rationale: Optional[str] = Field(
        None, description="Why this edit fits the trend (not rendered)."
    )


class RenderResult(BaseModel):
    """Outcome of rendering an EDL to a file."""

    output_path: str
    edl: EditDecisionList
    trend: Trend
