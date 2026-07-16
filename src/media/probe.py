"""Probe source clips with ffprobe to build SourceClip metadata."""

from __future__ import annotations

import json
import subprocess
from pathlib import Path

from ..log import get_logger
from ..models import SourceClip

log = get_logger(__name__)

VIDEO_EXTS = {".mp4", ".mov", ".mkv", ".webm", ".avi", ".m4v"}


def probe_clip(path: Path, ffprobe: str = "ffprobe") -> SourceClip | None:
    """Return metadata for a single clip, or None if it can't be probed."""
    cmd = [
        ffprobe,
        "-v",
        "error",
        "-print_format",
        "json",
        "-show_format",
        "-show_streams",
        str(path),
    ]
    try:
        out = subprocess.run(
            cmd, capture_output=True, text=True, check=True, timeout=30
        ).stdout
    except (subprocess.CalledProcessError, FileNotFoundError, subprocess.TimeoutExpired) as exc:
        log.warning("could not probe %s: %s", path.name, exc)
        return None

    data = json.loads(out)
    streams = data.get("streams", [])
    video = next((s for s in streams if s.get("codec_type") == "video"), None)
    has_audio = any(s.get("codec_type") == "audio" for s in streams)
    if video is None:
        return None

    duration = float(data.get("format", {}).get("duration") or video.get("duration") or 0)
    return SourceClip(
        path=str(path),
        duration=round(duration, 3),
        width=int(video.get("width", 0)),
        height=int(video.get("height", 0)),
        has_audio=has_audio,
    )


def probe_assets(assets_dir: Path, ffprobe: str = "ffprobe") -> list[SourceClip]:
    """Probe every video file in `assets_dir`."""
    if not assets_dir.exists():
        log.warning("assets dir not found: %s", assets_dir)
        return []
    clips: list[SourceClip] = []
    for f in sorted(assets_dir.iterdir()):
        if f.suffix.lower() in VIDEO_EXTS:
            clip = probe_clip(f, ffprobe=ffprobe)
            if clip:
                clips.append(clip)
    return clips
