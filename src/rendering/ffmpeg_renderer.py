"""FFmpeg rendering backend with effects.

Per cut it can trim, change speed, Ken Burns zoom, letterbox to the vertical
canvas, and burn a styled caption. Cuts are joined by hard concat or crossfade
(video xfade + audio acrossfade). An optional music bed is mixed under the
whole thing, plus a gentle fade in/out.

All filter syntax here was validated against ffmpeg 8.x before wiring in.
Clips without audio get synthesized silence so the audio graph stays aligned.
"""

from __future__ import annotations

import shutil
import subprocess
from pathlib import Path

from ..config import Config
from ..media.probe import probe_clip
from ..models import Cut, EditDecisionList
from .base import Renderer

_AFMT = "aformat=sample_rates=44100:channel_layouts=stereo"


def _strip_emoji(text: str) -> str:
    """Drop emoji/pictographs — the caption font (Arial) has no glyphs for them,
    so they'd burn in as empty boxes. Keeps normal text and punctuation."""
    out = []
    for ch in text:
        cp = ord(ch)
        if cp > 0xFFFF:
            continue
        if 0x2600 <= cp <= 0x27BF or cp == 0xFE0F or 0x2B00 <= cp <= 0x2BFF:
            continue
        out.append(ch)
    return "".join(out).strip()


def _escape_drawtext(text: str) -> str:
    """Escape a string for use inside ffmpeg drawtext=text='...'."""
    text = text.replace("\\", "\\\\")
    text = text.replace(":", r"\:")
    text = text.replace("'", "’")
    text = text.replace("%", r"\%")
    return text


class FFmpegRenderer(Renderer):
    def __init__(self, cfg: Config) -> None:
        super().__init__(cfg)
        r = cfg.render
        self.ffmpeg = cfg.media.get("ffmpeg", "ffmpeg")
        self.ffprobe = cfg.media.get("ffprobe", "ffprobe")
        self.w = int(r.get("width", 1080))
        self.h = int(r.get("height", 1920))
        self.fps = int(r.get("fps", 30))
        self.vcodec = r.get("video_codec", "libx264")
        self.acodec = r.get("audio_codec", "aac")
        self.crf = str(r.get("crf", 20))
        self.preset = r.get("preset", "medium")
        self.captions = bool(r.get("captions", True))
        # Caption styling.
        self.caption_size = int(r.get("caption_size", 56))
        self.caption_box = bool(r.get("caption_box", True))
        self.caption_position = r.get("caption_position", "bottom")
        # Effects.
        self.transition_duration = float(r.get("transition_duration", 0.5))
        self.final_fade = float(r.get("final_fade", 0.3))
        self.zoom_rate = float(r.get("zoom_rate", 0.004))
        # Music.
        self.music = r.get("music") or None
        self.music_volume = float(r.get("music_volume", 0.25))
        # Caption font (drawtext needs an explicit fontfile on Windows). We run
        # ffmpeg with cwd = the font's directory and reference it by bare name,
        # sidestepping the un-escapable drive-letter colon.
        self.font = r.get("font") or self._default_font()
        if self.font:
            self.font_dir: str | None = str(Path(self.font).parent)
            self.font_name: str | None = Path(self.font).name
        else:
            self.font_dir = self.font_name = None

    @staticmethod
    def _default_font() -> str | None:
        for candidate in (
            r"C:\Windows\Fonts\arial.ttf",
            "/System/Library/Fonts/Supplemental/Arial.ttf",
            "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf",
        ):
            if Path(candidate).exists():
                return candidate
        return None

    # ------------------------------------------------------------------ #
    # Filter building blocks
    # ------------------------------------------------------------------ #
    def _caption_filter(self, caption: str) -> str:
        txt = _escape_drawtext(_strip_emoji(caption))
        y = {
            "bottom": "h*0.80",
            "center": "(h-text_h)/2",
            "top": "h*0.10",
        }.get(self.caption_position, "h*0.80")
        draw = (
            f"drawtext=text='{txt}':fontcolor=white:fontsize={self.caption_size}:"
            f"borderw=4:bordercolor=black:x=(w-text_w)/2:y={y}"
        )
        if self.caption_box:
            draw += ":box=1:boxcolor=black@0.5:boxborderw=16"
        if self.font_name:
            draw += f":fontfile={self.font_name}"
        return draw

    def _zoom_filter(self, mode: str, out_frames: int) -> str:
        """zoompan Ken Burns; 'on' is the output frame index."""
        if mode == "in":
            z = f"min(1+{self.zoom_rate}*on,1.35)"
        else:  # out
            z = f"max(1.35-{self.zoom_rate}*on,1.0)"
        return (
            f"zoompan=z='{z}':d=1:x='iw/2-(iw/zoom/2)':y='ih/2-(ih/zoom/2)':"
            f"s={self.w}x{self.h}:fps={self.fps}"
        )

    def _video_chain(self, i: int, cut: Cut, duration: float) -> str:
        speed = max(0.5, min(cut.speed, 2.0))
        vf = (
            f"[{i}:v]trim=start={cut.start}:end={cut.end},"
            f"setpts=(PTS-STARTPTS)/{speed},"
            f"scale={self.w}:{self.h}:force_original_aspect_ratio=decrease,"
            f"pad={self.w}:{self.h}:(ow-iw)/2:(oh-ih)/2:color=black,"
            f"setsar=1,fps={self.fps}"
        )
        if cut.zoom in ("in", "out"):
            vf += "," + self._zoom_filter(cut.zoom, int(duration * self.fps))
        if self.captions and cut.caption and _strip_emoji(cut.caption):
            vf += "," + self._caption_filter(cut.caption)
        return f"{vf}[v{i}]"

    def _audio_chain(self, i: int, cut: Cut, has_audio: bool, silence_idx: int, duration: float) -> str:
        speed = max(0.5, min(cut.speed, 2.0))
        if has_audio:
            chain = f"[{i}:a]atrim=start={cut.start}:end={cut.end},asetpts=PTS-STARTPTS"
            if abs(speed - 1.0) > 1e-3:
                chain += f",atempo={speed}"
            chain += f",{_AFMT}[a{i}]"
        else:
            chain = (
                f"[{silence_idx}:a]atrim=start=0:end={round(duration, 3)},"
                f"asetpts=PTS-STARTPTS,{_AFMT}[a{i}]"
            )
        return chain

    # ------------------------------------------------------------------ #
    # Render
    # ------------------------------------------------------------------ #
    def render(self, edl: EditDecisionList, output_path: Path) -> Path:
        if shutil.which(self.ffmpeg) is None and not Path(self.ffmpeg).exists():
            raise RuntimeError(
                f"ffmpeg not found ('{self.ffmpeg}'). Install it or set media.ffmpeg."
            )
        output_path.parent.mkdir(parents=True, exist_ok=True)

        cuts = edl.cuts
        n = len(cuts)
        # Per-segment output durations (after speed).
        durs = [(c.end - c.start) / max(0.5, min(c.speed, 2.0)) for c in cuts]

        # Audio presence per unique clip.
        has_audio: dict[str, bool] = {}
        for c in cuts:
            if c.clip_path not in has_audio:
                clip = probe_clip(Path(c.clip_path), ffprobe=self.ffprobe)
                has_audio[c.clip_path] = bool(clip and clip.has_audio)

        # ---- inputs ----
        cmd: list[str] = [self.ffmpeg, "-y"]
        for c in cuts:
            cmd += ["-i", str(Path(c.clip_path).resolve())]
        silence_idx = n
        cmd += ["-f", "lavfi", "-i", "anullsrc=channel_layout=stereo:sample_rate=44100"]
        music_idx = None
        if self.music and Path(self.music).exists():
            music_idx = n + 1
            cmd += ["-stream_loop", "-1", "-i", str(Path(self.music).resolve())]

        # ---- per-segment chains ----
        parts: list[str] = []
        for i, c in enumerate(cuts):
            parts.append(self._video_chain(i, c, durs[i]))
            parts.append(self._audio_chain(i, c, has_audio[c.clip_path], silence_idx, durs[i]))

        # ---- combine (crossfade or concat) ----
        use_xfade = edl.transition == "crossfade" and n > 1
        if use_xfade:
            t = min(self.transition_duration, min(durs) / 2 - 0.05)
            t = max(t, 0.1)
            combine, vlabel, alabel, total = self._crossfade(n, durs, t)
            parts += combine
        elif n == 1:
            vlabel, alabel = "[v0]", "[a0]"
            total = durs[0]
        else:
            vlab = "".join(f"[v{i}]" for i in range(n))
            alab = "".join(f"[a{i}]" for i in range(n))
            parts.append(f"{vlab}concat=n={n}:v=1:a=0[vcat]")
            parts.append(f"{alab}concat=n={n}:v=0:a=1[acat]")
            vlabel, alabel, total = "[vcat]", "[acat]", sum(durs)

        # ---- final video fade ----
        if self.final_fade > 0:
            f = self.final_fade
            parts.append(
                f"{vlabel}fade=t=in:st=0:d={f},"
                f"fade=t=out:st={max(0, total - f):.3f}:d={f}[vout]"
            )
            vlabel = "[vout]"

        # ---- music bed ----
        if music_idx is not None:
            fade_st = max(0, total - 1.0)
            parts.append(
                f"[{music_idx}:a]atrim=0:{total:.3f},asetpts=PTS-STARTPTS,{_AFMT},"
                f"volume={self.music_volume},afade=t=out:st={fade_st:.3f}:d=1[mus]"
            )
            parts.append(f"{alabel}[mus]amix=inputs=2:duration=first[aout]")
            alabel = "[aout]"

        filter_complex = ";".join(parts)

        cmd += [
            "-filter_complex",
            filter_complex,
            "-map",
            vlabel,
            "-map",
            alabel,
            "-c:v",
            self.vcodec,
            "-crf",
            self.crf,
            "-preset",
            self.preset,
            "-c:a",
            self.acodec,
            "-movflags",
            "+faststart",
            str(output_path.resolve()),
        ]

        cwd = self.font_dir if self.font_name else None
        proc = subprocess.run(cmd, capture_output=True, text=True, cwd=cwd)
        if proc.returncode != 0:
            raise RuntimeError("ffmpeg failed:\n" + proc.stderr[-4000:])
        return output_path

    def _crossfade(
        self, n: int, durs: list[float], t: float
    ) -> tuple[list[str], str, str, float]:
        """Chain xfade (video) + acrossfade (audio) across n segments."""
        parts: list[str] = []
        # Video: offset_k = sum(d_0..d_k) - (k+1)*t
        vprev = "[v0]"
        cumulative = 0.0
        for k in range(1, n):
            cumulative += durs[k - 1]
            offset = cumulative - k * t
            out = f"[vx{k}]" if k < n - 1 else "[vout_x]"
            parts.append(
                f"{vprev}[v{k}]xfade=transition=fade:duration={t}:"
                f"offset={offset:.3f}{out}"
            )
            vprev = out
        # Audio: acrossfade chains, no offset needed.
        aprev = "[a0]"
        for k in range(1, n):
            out = f"[ax{k}]" if k < n - 1 else "[aout_x]"
            parts.append(f"{aprev}[a{k}]acrossfade=d={t}{out}")
            aprev = out
        total = sum(durs) - (n - 1) * t
        return parts, "[vout_x]", "[aout_x]", total
