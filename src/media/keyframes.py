"""Extract keyframes from clips so Claude can *see* the footage.

FFmpeg samples a handful of frames per clip; they're downscaled to keep the
vision token cost low and returned as (timestamp, jpeg_path) pairs. The
decisioning step base64-encodes them into the prompt as image blocks.
"""

from __future__ import annotations

import subprocess
from pathlib import Path


def sample_times(duration: float, n: int) -> list[float]:
    """Evenly spaced sample points across a clip, avoiding the exact ends."""
    n = max(1, n)
    return [round((i + 0.5) / n * duration, 3) for i in range(n)]


def extract_frame(
    clip_path: str, t: float, dest: Path, ffmpeg: str = "ffmpeg", width: int = 360
) -> Path | None:
    """Extract a single frame at time `t` to `dest`. Returns dest or None."""
    dest.parent.mkdir(parents=True, exist_ok=True)
    cmd = [
        ffmpeg, "-y", "-ss", str(t), "-i", str(Path(clip_path).resolve()),
        "-frames:v", "1", "-vf", f"scale={width}:-2", "-q:v", "3", str(dest),
    ]
    try:
        proc = subprocess.run(cmd, capture_output=True, text=True, timeout=60)
    except subprocess.TimeoutExpired:
        return None
    return dest if proc.returncode == 0 and dest.exists() else None


def extract_keyframes(
    clip_path: str,
    times: list[float],
    out_dir: Path,
    ffmpeg: str = "ffmpeg",
    width: int = 512,
) -> list[tuple[float, Path]]:
    """Extract one JPEG per timestamp; return [(t, path), ...] for successes."""
    out_dir.mkdir(parents=True, exist_ok=True)
    stem = Path(clip_path).stem
    frames: list[tuple[float, Path]] = []
    src = str(Path(clip_path).resolve())
    for i, t in enumerate(times):
        dest = out_dir / f"{stem}_{i}.jpg"
        cmd = [
            ffmpeg,
            "-y",
            "-ss",
            str(t),  # fast seek before -i
            "-i",
            src,
            "-frames:v",
            "1",
            "-vf",
            f"scale={width}:-2",
            "-q:v",
            "3",
            str(dest),
        ]
        try:
            proc = subprocess.run(cmd, capture_output=True, text=True, timeout=60)
        except subprocess.TimeoutExpired:
            continue
        if proc.returncode == 0 and dest.exists():
            frames.append((t, dest))
    return frames
