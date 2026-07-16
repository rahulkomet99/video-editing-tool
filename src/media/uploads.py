"""Guards for user-supplied uploads.

FFmpeg and Whisper on a huge or long file are a trivial denial-of-service, so
uploads are bounded by size, count, and (for clips) duration. Limits come from
`config.yaml` under `uploads:`. These helpers are pure (return an error string
or None) so they're easy to unit-test and reuse from the UI.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from ..models import SourceClip


@dataclass
class UploadLimits:
    max_upload_mb: float = 300.0
    max_clips: int = 25
    max_duration_s: float = 900.0

    @classmethod
    def from_config(cls, cfg) -> UploadLimits:
        u = getattr(cfg, "uploads", None) or {}
        return cls(
            max_upload_mb=float(u.get("max_upload_mb", 300.0)),
            max_clips=int(u.get("max_clips", 25)),
            max_duration_s=float(u.get("max_duration_s", 900.0)),
        )


def size_error(name: str, size_bytes: int, max_mb: float) -> str | None:
    """Reject a file larger than `max_mb`."""
    if size_bytes > max_mb * 1024 * 1024:
        got = size_bytes / (1024 * 1024)
        return f"{name}: {got:.0f} MB exceeds the {max_mb:.0f} MB limit."
    return None


def count_error(existing: int, adding: int, max_clips: int) -> str | None:
    """Reject uploads that would push the clip library past `max_clips`."""
    if existing + adding > max_clips:
        return (
            f"Too many clips: {existing}+{adding} would exceed the "
            f"{max_clips}-clip limit. Remove some first."
        )
    return None


def duration_error(clip: SourceClip | None, max_duration_s: float) -> str | None:
    """Reject a clip longer than `max_duration_s` (None = couldn't probe)."""
    if clip is None:
        return "could not read the video (unsupported or corrupt file)."
    if clip.duration > max_duration_s:
        return (
            f"{Path(clip.path).name}: {clip.duration:.0f}s exceeds the "
            f"{max_duration_s:.0f}s limit."
        )
    return None
