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
import tempfile
from functools import lru_cache
from pathlib import Path

from ..config import Config
from ..log import get_logger
from ..media.probe import probe_clip
from ..media.run import run as run_ffmpeg
from ..models import Cut, EditDecisionList, ImageOverlay
from .base import Renderer

log = get_logger(__name__)

try:  # optional: exact text measurement so titles/captions never run off-frame
    from PIL import ImageFont
except Exception:  # noqa: BLE001
    ImageFont = None  # type: ignore

_AFMT = "aformat=sample_rates=44100:channel_layouts=stereo"


@lru_cache(maxsize=128)
def _load_font(path: str, size: int):
    return ImageFont.truetype(path, size)


# Named color grades → FFmpeg filter chains. Applied per cut (after zoom,
# before captions) so the footage is graded but burned-in text stays clean.
# All filters are FFmpeg built-ins — no extra dependencies.
LOOKS: dict[str, str] = {
    "none": "",
    "clean": "eq=contrast=1.05:saturation=1.06",
    "vibrant": "eq=contrast=1.12:saturation=1.35,vibrance=intensity=0.30",
    "cinematic": "curves=preset=increase_contrast,"
                 "colorbalance=rs=-0.06:bs=0.06:rh=0.05:bh=-0.05,vignette",
    "warm": "colortemperature=temperature=5200,eq=saturation=1.10:contrast=1.05",
    "cool": "colortemperature=temperature=8200,eq=saturation=1.05:contrast=1.05",
    "moody": "eq=contrast=1.15:brightness=-0.03:saturation=0.90,vignette",
    "vintage": "curves=preset=vintage,eq=saturation=0.85,vignette",
    "bw": "hue=s=0,eq=contrast=1.12",
}


def _escape_filter_path(p: str) -> str:
    """Make an absolute path safe inside an FFmpeg filter arg (e.g. lut3d=file=).
    Forward slashes + escaped drive-letter colon, sidestepping Windows issues."""
    return str(Path(p).resolve()).replace("\\", "/").replace(":", r"\:")


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
        # Hard ceiling on a single ffmpeg pass so a bad input can't hang a worker.
        self.render_timeout = float(r.get("render_timeout", 600))
        self.captions = bool(r.get("captions", True))
        # Caption mode: "manual" burns the per-cut EDL captions; "auto" runs
        # speech-to-text (faster-whisper) and burns word-by-word animated
        # captions in a second pass; "off" burns none. Legacy `captions: false`
        # maps to "off" when captions_mode is unset.
        self.captions_mode = r.get("captions_mode") or ("manual" if self.captions else "off")
        self.whisper_model = r.get("whisper_model", "base")
        self.caption_highlight = r.get("caption_highlight", "#FFE000")
        # Color grading. `look` is the video-level default (an EDL can override
        # it, and cuts can override per-shot). `lut` is an optional .cube file
        # applied on top of every cut — set a name/path in config, drop the
        # file in luts_dir. LUT paths are escaped for the filter graph.
        self.look = r.get("look", "clean")
        self.lut_path: str | None = self._resolve_lut(cfg, r.get("lut"))
        # Caption styling — heavy outline + drop shadow (the clean "social"
        # look), no translucent box by default.
        self.caption_size = int(r.get("caption_size", 64))
        self.caption_box = bool(r.get("caption_box", False))
        self.caption_position = r.get("caption_position", "bottom")
        # Effects.
        self.transition_duration = float(r.get("transition_duration", 0.5))
        self.final_fade = float(r.get("final_fade", 0.3))
        self.zoom_rate = float(r.get("zoom_rate", 0.004))
        # Music.
        self.music = r.get("music") or None
        self.music_volume = float(r.get("music_volume", 0.25))
        # Logo / watermark (composited as an image overlay for the whole video).
        self.logo = r.get("logo") or None
        self.logo_x = float(r.get("logo_x", 0.85))
        self.logo_y = float(r.get("logo_y", 0.07))
        self.logo_scale = float(r.get("logo_scale", 0.16))
        self.logo_opacity = float(r.get("logo_opacity", 0.9))
        # Caption font (drawtext needs an explicit fontfile on Windows). We run
        # ffmpeg with cwd = the font's directory and reference it by bare name,
        # sidestepping the un-escapable drive-letter colon.
        self.font = r.get("font") or self._default_font()
        if self.font:
            self.font_dir: str | None = str(Path(self.font).parent)
            self.font_name: str | None = Path(self.font).name
        else:
            self.font_dir = self.font_name = None

    def _resolve_lut(self, cfg: Config, lut) -> str | None:
        """Find a .cube LUT by explicit path or by name inside luts_dir, and
        return it escaped for the filter graph (or None if not found)."""
        if not lut:
            return None
        cand = Path(lut)
        if not cand.exists():
            luts_dir = cfg.path(cfg.render.get("luts_dir", "assets/luts"))
            name = lut if lut.endswith(".cube") else f"{lut}.cube"
            cand = luts_dir / name
        if cand.exists():
            return _escape_filter_path(str(cand))
        log.warning("LUT %r not found — skipping.", lut)
        return None

    def _look_filter(self, cut: Cut, video_look: str) -> str:
        """Filter chain for a cut's color grade: named preset + optional fine
        contrast/saturation/brightness tweaks + optional LUT."""
        name = cut.look or video_look or "clean"
        parts: list[str] = []
        base = LOOKS.get(name, "")
        if base:
            parts.append(base)
        eq = []
        if cut.contrast is not None:
            eq.append(f"contrast={cut.contrast}")
        if cut.saturation is not None:
            eq.append(f"saturation={cut.saturation}")
        if cut.brightness is not None:
            eq.append(f"brightness={cut.brightness}")
        if eq:
            parts.append("eq=" + ":".join(eq))
        if self.lut_path:
            # Value must be single-quoted AND have its drive-letter colon
            # escaped, or the filtergraph parser splits it on ':' (Windows).
            parts.append(f"lut3d=file='{self.lut_path}'")
        return ",".join(parts)

    @staticmethod
    def _default_font() -> str | None:
        # Prefer a heavy/bold face — thin captions read as amateur. Fall back
        # through bold → black → regular on each platform.
        for candidate in (
            r"C:\Windows\Fonts\arialbd.ttf",   # Arial Bold
            r"C:\Windows\Fonts\ariblk.ttf",    # Arial Black (heaviest)
            r"C:\Windows\Fonts\seguisb.ttf",   # Segoe UI Semibold
            r"C:\Windows\Fonts\arial.ttf",
            "/System/Library/Fonts/Supplemental/Arial Bold.ttf",
            "/Library/Fonts/Arial Bold.ttf",
            "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf",
            "/System/Library/Fonts/Supplemental/Arial.ttf",
            "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf",
        ):
            if Path(candidate).exists():
                return candidate
        return None

    # ------------------------------------------------------------------ #
    # Filter building blocks
    # ------------------------------------------------------------------ #
    def _fit_size(self, text: str, size: int, max_frac: float = 0.9) -> int:
        """Shrink the font size until the (single-line) text fits within
        `max_frac` of the frame width. drawtext can't wrap, so an oversized
        title would run off the edge and look broken."""
        text = text.strip()
        if not text:
            return size
        max_w = self.w * max_frac
        if ImageFont and self.font:
            try:
                for s in range(size, 16, -2):
                    if _load_font(self.font, s).getlength(text) <= max_w:
                        return s
                return 16
            except Exception:  # noqa: BLE001
                pass
        # Fallback estimate: ~0.62em average advance for bold caps.
        return max(16, min(size, int(max_w / (0.62 * len(text)))))

    def _draw(
        self,
        txt: str,
        size: int,
        color: str,
        x: str,
        y: str,
        *,
        box: bool = False,
        enable: str | None = None,
        alpha: str | None = None,
    ) -> str:
        """One styled drawtext element. Heavy outline scaled to the font size
        plus a soft drop shadow — reads clean over any footage. `x`/`y` are
        ffmpeg position expressions; `enable`/`alpha` are optional time exprs."""
        border = max(5, round(size / 9))
        d = (
            f"drawtext=text='{txt}':fontcolor={color}:fontsize={size}:"
            f"borderw={border}:bordercolor=black:"
            f"shadowcolor=black@0.55:shadowx=3:shadowy=4:"
            f"line_spacing={round(size * 0.18)}:"
            f"x={x}:y={y}"
        )
        if box:
            d += ":box=1:boxcolor=black@0.5:boxborderw=28"
        if enable is not None:
            d += f":enable='{enable}'"
        if alpha is not None:
            d += f":alpha='{alpha}'"
        if self.font_name:
            d += f":fontfile={self.font_name}"
        return d

    def _caption_filter(self, caption: str) -> str:
        plain = _strip_emoji(caption)
        size = self._fit_size(plain, self.caption_size)
        txt = _escape_drawtext(plain)
        y = {
            "bottom": "h*0.82-text_h/2",
            "center": "(h-text_h)/2",
            "top": "h*0.13-text_h/2",
        }.get(self.caption_position, "h*0.82-text_h/2")
        # Captions live on the per-cut segment (t starts at 0), so pop them in
        # over the first 0.2s for a bit of life.
        return self._draw(
            txt, size, "white", "(w-text_w)/2", y,
            box=self.caption_box, alpha="if(lt(t,0.2),t/0.2,1)",
        )

    def _text_overlay_filter(self, ov, total: float) -> str:
        """A timeline text element (title/callout) positioned + timed on the
        output, with an optional fade. Independent of per-cut captions."""
        plain = _strip_emoji(ov.text)
        size = self._fit_size(plain, ov.size)
        txt = _escape_drawtext(plain)
        s = max(0.0, ov.start)
        e = min(ov.end, total)
        alpha = None
        if ov.animate == "fade" and e - s > 0.6:
            alpha = (
                f"if(lt(t,{s + 0.3:.3f}),(t-{s:.3f})/0.3,"
                f"if(gt(t,{e - 0.3:.3f}),({e:.3f}-t)/0.3,1))"
            )
        return self._draw(
            txt, size, ov.color, f"w*{ov.x}-text_w/2", f"h*{ov.y}-text_h/2",
            box=ov.box, enable=f"between(t,{s:.3f},{e:.3f})", alpha=alpha,
        )

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

    def _video_chain(
        self, i: int, cut: Cut, duration: float, video_look: str, burn_captions: bool
    ) -> str:
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
        look = self._look_filter(cut, video_look)
        if look:  # grade the footage before burning text over it
            vf += "," + look
        if burn_captions and cut.caption and _strip_emoji(cut.caption):
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
        """Dispatch on caption mode. 'auto' renders the video first, then burns
        word-by-word captions in a second pass; otherwise a single pass."""
        if self.captions_mode == "auto":
            return self._render_auto_captions(edl, output_path)
        return self._render_core(
            edl, output_path, burn_captions=(self.captions_mode == "manual")
        )

    def _render_auto_captions(self, edl: EditDecisionList, output_path: Path) -> Path:
        from ..media.transcribe import transcribe_words
        from .captions_ass import build_ass

        output_path.parent.mkdir(parents=True, exist_ok=True)
        # Pass 1 + the .ass live in a private temp dir: a unique path per render
        # (no collision if two renders of the same title run at once) that is
        # auto-cleaned. cwd = this dir lets the subtitles filter reference the
        # .ass by bare name, sidestepping the drive-letter colon it can't parse.
        with tempfile.TemporaryDirectory(prefix="autocap_") as td:
            pass1 = Path(td) / "pass1.mp4"
            # Full edit (looks, overlays, music) but no per-cut captions — the
            # word captions replace them.
            self._render_core(edl, pass1, burn_captions=False)

            words = transcribe_words(pass1, model_size=self.whisper_model)
            if not words:
                # No speech (e.g. b-roll). Auto captions can't help, so give the
                # user Claude's manual captions instead of a caption-less video.
                log.warning("No speech detected — falling back to manual captions.")
                return self._render_core(edl, output_path, burn_captions=True)

            ass = build_ass(
                words, self.w, self.h, highlight=self.caption_highlight,
                position=self.caption_position,
            )
            (Path(td) / "words.ass").write_text(ass, encoding="utf-8")
            cmd = [
                self.ffmpeg, "-y", "-i", str(pass1.resolve()),
                "-vf", "subtitles=words.ass",
                "-c:v", self.vcodec, "-crf", self.crf, "-preset", self.preset,
                "-c:a", "copy", "-movflags", "+faststart",
                str(output_path.resolve()),
            ]
            proc = run_ffmpeg(cmd, timeout=self.render_timeout, cwd=td)
            if proc.returncode != 0:
                raise RuntimeError("ffmpeg (subtitles) failed:\n" + proc.stderr[-4000:])
        return output_path

    def _render_core(
        self, edl: EditDecisionList, output_path: Path, burn_captions: bool = True
    ) -> Path:
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
        # Image overlays: config/UI logo first, then any on the EDL. Missing
        # files are skipped (handles unset/hallucinated paths).
        overlays_to_add = list(edl.image_overlays)
        if self.logo and Path(self.logo).exists():
            overlays_to_add = [
                ImageOverlay(path=self.logo, start=0.0, end=1e9, x=self.logo_x,
                             y=self.logo_y, scale=self.logo_scale, opacity=self.logo_opacity)
            ] + overlays_to_add
        next_idx = (music_idx + 1) if music_idx is not None else (silence_idx + 1)
        img_overlays: list[tuple] = []
        for ov in overlays_to_add:
            if Path(ov.path).exists():
                cmd += ["-i", str(Path(ov.path).resolve())]
                img_overlays.append((next_idx, ov))
                next_idx += 1

        # ---- per-segment chains ----
        video_look = getattr(edl, "look", None) or self.look
        parts: list[str] = []
        for i, c in enumerate(cuts):
            parts.append(self._video_chain(i, c, durs[i], video_look, burn_captions))
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

        # ---- text overlays (timeline titles / callouts) ----
        for k, tov in enumerate(edl.text_overlays):
            parts.append(f"{vlabel}{self._text_overlay_filter(tov, total)}[vt{k}]")
            vlabel = f"[vt{k}]"

        # ---- image overlays (logo / stickers) ----
        for j, (idx, iov) in enumerate(img_overlays):
            end = min(iov.end, total)
            parts.append(
                f"[{idx}:v]format=rgba,colorchannelmixer=aa={iov.opacity},"
                f"scale={self.w}*{iov.scale}:-1[ovi{j}]"
            )
            parts.append(
                f"{vlabel}[ovi{j}]overlay=x='W*{iov.x}-w/2':y='H*{iov.y}-h/2':"
                f"enable='between(t,{iov.start},{end})'[vo{j}]"
            )
            vlabel = f"[vo{j}]"

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
        proc = run_ffmpeg(cmd, timeout=self.render_timeout, cwd=cwd)
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
