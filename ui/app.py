"""Streamlit UI for the automated video editing pipeline.

Run from the project root:
    streamlit run ui/app.py

Two ways to drive it:
  • Auto-Pilot — one click runs the whole pipeline (trend → edit → render)
    with live status.
  • Manual — step through clips, trend, edit, and render for full control.
Both show a storyboard of the edit and play the result inline.
"""

from __future__ import annotations

import shutil
import sys
from pathlib import Path
from typing import get_args

# Make `import src...` work regardless of where streamlit is launched from.
_ROOT = Path(__file__).resolve().parent.parent
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

import streamlit as st

from src.config import Config
from src.decisioning import ClaudeEditor, ClipAnalyzer
from src.ingestion import gather_trends
from src.log import configure as configure_logging
from src.media.keyframes import extract_frame
from src.media.probe import probe_assets, probe_clip
from src.media.uploads import (
    UploadLimits,
    count_error,
    duration_error,
    size_error,
)
from src.models import Cut, EditDecisionList, Look, Trend
from src.pipeline import _slugify
from src.rendering import get_renderer

configure_logging()
LOOK_OPTIONS = list(get_args(Look))

st.set_page_config(page_title="Auto Video Editor", page_icon="🎬", layout="wide")

# Light touch of styling on top of the dark theme in .streamlit/config.toml.
st.markdown(
    """
    <style>
      .block-container {padding-top: 2.2rem; max-width: 1200px;}
      h1 {letter-spacing: -0.5px;}
      .hero-tag {color: #9aa0b4; font-size: 1.02rem; margin-top: -0.6rem;}
      div[data-testid="stMetricValue"] {font-size: 1.5rem;}
      .stButton>button {border-radius: 10px; font-weight: 600;}
    </style>
    """,
    unsafe_allow_html=True,
)

UPLOAD_TYPES = ["mp4", "mov", "mkv", "webm", "avi", "m4v"]
MUSIC_TYPES = ["mp3", "m4a", "aac", "wav", "ogg"]


# --------------------------------------------------------------------------- #
# Helpers
# --------------------------------------------------------------------------- #
@st.cache_resource(show_spinner=False)
def load_config(path: str) -> Config:
    return Config.load(path)


def ffmpeg_ready(cfg: Config) -> bool:
    exe = cfg.media.get("ffmpeg", "ffmpeg")
    return shutil.which(exe) is not None or Path(exe).exists()


def ss_init() -> None:
    for k, v in {
        "clips": None,
        "trends": [],
        "edl": None,
        "edl_version": 0,
        "brief": None,
        "render_path": None,
        "uploaded_sigs": set(),
    }.items():
        st.session_state.setdefault(k, v)


def set_edl(edl) -> None:
    """Store a freshly-generated EDL and bump the version so the timeline
    editor rebinds to the new data (keyed widgets ignore new data otherwise)."""
    st.session_state.edl = edl
    st.session_state.edl_version += 1
    st.session_state.render_path = None


def scan_clips(cfg: Config):
    st.session_state.clips = probe_assets(
        cfg.path(cfg.media.get("assets_dir", "assets")),
        ffprobe=cfg.media.get("ffprobe", "ffprobe"),
    )
    return st.session_state.clips


def save_uploads(cfg: Config, uploads) -> list[str]:
    """Persist uploaded clips into the assets folder (within size/count/duration
    limits); return new filenames. Rejected files are surfaced via st.error."""
    limits = UploadLimits.from_config(cfg)
    assets_dir = cfg.path(cfg.media.get("assets_dir", "assets"))
    assets_dir.mkdir(parents=True, exist_ok=True)
    existing = len(st.session_state.clips or [])
    ffprobe = cfg.media.get("ffprobe", "ffprobe")
    saved: list[str] = []
    for uf in uploads or []:
        sig = (uf.name, uf.size)
        if sig in st.session_state.uploaded_sigs:
            continue
        err = (size_error(uf.name, uf.size, limits.max_upload_mb)
               or count_error(existing + len(saved), 1, limits.max_clips))
        if err:
            st.error(f"Skipped {err}")
            continue
        dest = assets_dir / Path(uf.name).name
        dest.write_bytes(uf.getbuffer())
        # Duration guard needs the file on disk; reject + remove if too long.
        derr = duration_error(probe_clip(dest, ffprobe=ffprobe), limits.max_duration_s)
        if derr:
            dest.unlink(missing_ok=True)
            st.error(f"Skipped {derr}")
            continue
        st.session_state.uploaded_sigs.add(sig)
        saved.append(Path(uf.name).name)
    if saved:
        scan_clips(cfg)
    return saved


def stash_upload(cfg: Config, file, subdir: str) -> str | None:
    """Persist a per-run upload (music/logo) under output/<subdir>, within the
    size limit. Returns None (with an st.error) if the file is too large."""
    if file is None:
        return None
    limits = UploadLimits.from_config(cfg)
    err = size_error(file.name, file.size, limits.max_upload_mb)
    if err:
        st.error(f"Skipped {err}")
        return None
    d = cfg.path(cfg.render.get("output_dir", "output")) / subdir
    d.mkdir(parents=True, exist_ok=True)
    p = d / Path(file.name).name
    p.write_bytes(file.getbuffer())
    return str(p)


def thumb(cfg: Config, clip_path: str, t: float) -> Path | None:
    """Cached storyboard thumbnail for a clip at time t."""
    if not ffmpeg_ready(cfg):
        return None
    cache = cfg.path(cfg.render.get("output_dir", "output")) / ".thumbs"
    dest = cache / f"{Path(clip_path).stem}_{int(t * 1000)}.jpg"
    if dest.exists():
        return dest
    return extract_frame(clip_path, t, dest, ffmpeg=cfg.media.get("ffmpeg", "ffmpeg"))


def show_edl(cfg: Config, edl) -> None:
    """Metrics + storyboard for an edit decision list."""
    total = round(sum(c.end - c.start for c in edl.cuts), 1)
    c1, c2, c3, c4 = st.columns(4)
    c1.metric("Cuts", len(edl.cuts))
    c2.metric("Length", f"~{total}s")
    c3.metric("Transition", edl.transition)
    c4.metric("Look", getattr(edl, "look", "clean"))

    st.markdown(f"#### {edl.title}")
    st.caption(f"Hook — {edl.hook}")
    if edl.hashtags:
        st.markdown(" ".join(f"`#{h}`" for h in edl.hashtags))
    if edl.text_overlays:
        st.caption("🅣 Text overlays: " + " · ".join(
            f"“{o.text}” ({o.start:.0f}–{o.end:.0f}s)" for o in edl.text_overlays))

    # Token usage for this video (analyze + edit), if we ran the pipeline.
    rates = cfg.claude.get("pricing")
    parts_u = []
    au = st.session_state.get("analyze_usage")
    eu = st.session_state.get("edit_usage")
    if au and (au.input or au.output):
        parts_u.append(f"analyze {au.input + au.output:,} tok")
    if eu and (eu.input or eu.output):
        parts_u.append(f"edit {eu.input + eu.output:,} tok")
    if parts_u:
        combined_cost = sum(u.cost(rates) for u in (au, eu) if u)
        st.caption(f"🧮 Claude usage — {' · '.join(parts_u)} · ~${combined_cost:.3f} (est.)")

    st.markdown("**Storyboard**")
    cuts = edl.cuts
    per_row = 4
    for row_start in range(0, len(cuts), per_row):
        row = cuts[row_start : row_start + per_row]
        cols = st.columns(per_row)
        for col, (idx, cut) in zip(cols, enumerate(row, start=row_start), strict=False):
            with col, st.container(border=True):
                img = thumb(cfg, cut.clip_path, (cut.start + cut.end) / 2)
                if img:
                    st.image(str(img), width="stretch")
                st.markdown(f"**{idx + 1}. {cut.caption or '—'}**")
                badges = f"{cut.start:.1f}–{cut.end:.1f}s"
                if cut.zoom != "none":
                    badges += f" · 🔍 {cut.zoom}"
                if abs(cut.speed - 1.0) > 1e-3:
                    badges += f" · ⏩ {cut.speed}×"
                if cut.look:
                    badges += f" · 🎨 {cut.look}"
                st.caption(badges)
    if edl.rationale:
        with st.expander("Why this edit"):
            st.write(edl.rationale)


def show_result(path: str) -> None:
    if path and Path(path).exists():
        st.markdown("#### Result")
        st.video(path)
        with open(path, "rb") as fh:
            st.download_button(
                "⬇ Download .mp4", fh, file_name=Path(path).name, mime="video/mp4"
            )


def _opt_float(value):
    """Parse an optional numeric cell; blank/invalid → None (inherit default)."""
    if value in (None, ""):
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def rebuild_edl(rows, title, transition, hashtags, look, clips, base) -> EditDecisionList:
    """Turn edited timeline rows back into a validated EditDecisionList."""
    name2clip = {Path(c.path).name: c for c in clips}
    name2path = {Path(c.path).name: c.path for c in clips}
    for cut in base.cuts:  # keep paths that aren't in the current clip set
        name2path.setdefault(Path(cut.clip_path).name, cut.clip_path)

    cuts: list[Cut] = []
    for r in rows:
        name = r.get("clip")
        path = name2path.get(name)
        if not path:
            continue
        try:
            start = max(0.0, float(r.get("start") or 0.0))
            end = float(r.get("end") or 0.0)
        except (TypeError, ValueError):
            continue
        if end <= start:  # reversed/zero-length — drop, don't coerce
            continue
        clip = name2clip.get(name)
        if clip:  # clamp to real bounds when we know the duration
            end = min(end, clip.duration)
            start = min(start, end)
        if end - start < 0.1:
            continue
        speed = min(2.0, max(0.5, float(r.get("speed") or 1.0)))
        zoom = r.get("zoom") or "none"
        caption = (r.get("caption") or "").strip() or None
        cut_look = r.get("look") or None
        if cut_look in ("", "inherit"):
            cut_look = None
        cuts.append(Cut(
            clip_path=path, start=round(start, 3), end=round(end, 3),
            caption=caption, zoom=zoom, speed=speed, look=cut_look,
            contrast=_opt_float(r.get("contrast")),
            saturation=_opt_float(r.get("saturation")),
            brightness=_opt_float(r.get("brightness")),
        ))

    if not cuts:  # never let an empty edit wipe the plan
        cuts = base.cuts
    tags = [h.strip().lstrip("#") for h in (hashtags or "").split(",") if h.strip()]
    return EditDecisionList(
        title=title or base.title, hook=base.hook, transition=transition,
        look=look or getattr(base, "look", "clean"), cuts=cuts, hashtags=tags,
        text_overlays=base.text_overlays, rationale=base.rationale,
    )


def timeline_editor(cfg, clips) -> None:
    """Editable storyboard: tweak cuts/title/transition, add/delete rows."""
    edl = st.session_state.edl
    if not edl:
        return
    ver = st.session_state.edl_version
    st.markdown("**✏️ Edit the timeline** — changes apply immediately.")
    g1, g2, g3 = st.columns([2, 1, 1])
    title = g1.text_input("Title", value=edl.title, key=f"title_{ver}")
    transition = g2.selectbox(
        "Transition", ["crossfade", "none"],
        index=0 if edl.transition == "crossfade" else 1, key=f"trans_{ver}",
    )
    cur_look = getattr(edl, "look", "clean")
    look = g3.selectbox(
        "Look (whole video)", LOOK_OPTIONS,
        index=LOOK_OPTIONS.index(cur_look) if cur_look in LOOK_OPTIONS else 1,
        key=f"look_{ver}",
    )
    hashtags = st.text_input(
        "Hashtags (comma-separated)", value=", ".join(edl.hashtags), key=f"tags_{ver}"
    )

    names = sorted({Path(c.path).name for c in clips}
                   | {Path(c.clip_path).name for c in edl.cuts})
    rows = [
        {"clip": Path(c.clip_path).name, "start": c.start, "end": c.end,
         "caption": c.caption or "", "zoom": c.zoom, "speed": c.speed,
         "look": c.look or "inherit", "contrast": c.contrast,
         "saturation": c.saturation, "brightness": c.brightness}
        for c in edl.cuts
    ]
    edited = st.data_editor(
        rows, key=f"cuts_{ver}", num_rows="dynamic", width="stretch",
        column_config={
            "clip": st.column_config.SelectboxColumn("Clip", options=names, required=True),
            "start": st.column_config.NumberColumn("Start (s)", min_value=0.0, step=0.5),
            "end": st.column_config.NumberColumn("End (s)", min_value=0.0, step=0.5),
            "caption": st.column_config.TextColumn("Caption"),
            "zoom": st.column_config.SelectboxColumn("Zoom", options=["none", "in", "out"]),
            "speed": st.column_config.NumberColumn("Speed", min_value=0.5, max_value=2.0, step=0.05),
            "look": st.column_config.SelectboxColumn(
                "Look", options=["inherit"] + LOOK_OPTIONS,
                help="Per-shot color grade; 'inherit' uses the video look."),
            "contrast": st.column_config.NumberColumn(
                "Contrast", min_value=0.5, max_value=2.0, step=0.05,
                help="Optional fine tweak; blank = untouched."),
            "saturation": st.column_config.NumberColumn(
                "Saturation", min_value=0.0, max_value=2.0, step=0.05,
                help="Optional fine tweak; blank = untouched."),
            "brightness": st.column_config.NumberColumn(
                "Brightness", min_value=-0.3, max_value=0.3, step=0.02,
                help="Optional fine tweak; blank = untouched."),
        },
    )
    rows_out = edited.to_dict("records") if hasattr(edited, "to_dict") else list(edited)
    st.session_state.edl = rebuild_edl(rows_out, title, transition, hashtags, look, clips, edl)
    show_edl(cfg, st.session_state.edl)


def run_pipeline(cfg, clips, trend, music_path, logo_path, status) -> tuple:
    """Shared trend→edit→render flow with live status writes."""
    status.write(f"🔎 Trend: **{trend.title}**")
    status.write("🎬 Watching the footage and cutting (Claude)…")
    editor = ClaudeEditor(cfg)
    edl = editor.decide(trend, clips)
    st.session_state.edit_usage = editor.last_usage
    status.write(f"→ {len(edl.cuts)} cuts · {len(edl.text_overlays)} text overlays · "
                 f"{edl.transition}")
    status.write(f"🧮 Edit tokens — {editor.last_usage.line(cfg.claude.get('pricing'))}")
    status.write("🎞️ Rendering — zoom, crossfades, captions, overlays"
                 + (", music" if music_path else "") + "…")
    out_dir = cfg.path(cfg.render.get("output_dir", "output"))
    out_path = out_dir / f"{_slugify(edl.title)}.mp4"
    prev = (cfg.render.get("music"), cfg.render.get("logo"))
    try:
        if music_path:
            cfg.render["music"] = music_path
        if logo_path:
            cfg.render["logo"] = logo_path
        get_renderer(cfg).render(edl, out_path)
    finally:
        cfg.render["music"], cfg.render["logo"] = prev
    return edl, str(out_path)


ss_init()

# --------------------------------------------------------------------------- #
# Sidebar — environment
# --------------------------------------------------------------------------- #
st.sidebar.title("🎬 Auto Video Editor")
config_path = st.sidebar.text_input("Config file", value="config.yaml")
try:
    cfg = load_config(config_path)
except Exception as exc:  # noqa: BLE001
    st.sidebar.error(f"Could not load config: {exc}")
    st.stop()

api_ok = bool(cfg.anthropic_api_key)
ff_ok = ffmpeg_ready(cfg)
st.sidebar.subheader("Status")
st.sidebar.write(f"{'🟢' if api_ok else '🟡'} Anthropic key"
                 + ("" if api_ok else " (or `ant` profile)"))
st.sidebar.write(f"{'🟢' if ff_ok else '🔴'} FFmpeg"
                 + ("" if ff_ok else " — not found"))
st.sidebar.caption(
    f"Model `{cfg.claude.get('model', 'claude-opus-4-8')}` · "
    f"{cfg.render.get('width', 1080)}×{cfg.render.get('height', 1920)} · "
    f"sources: {', '.join(cfg.ingestion.get('sources', []))}"
)
if not ff_ok:
    st.sidebar.warning("Rendering is disabled until FFmpeg is on PATH.")

st.sidebar.subheader("Captions")
_cap_modes = ["manual", "auto", "off"]
_cur_cap = cfg.render.get("captions_mode", "manual")
cap_mode = st.sidebar.selectbox(
    "Style", _cap_modes,
    index=_cap_modes.index(_cur_cap) if _cur_cap in _cap_modes else 0,
    format_func=lambda m: {
        "manual": "Manual (Claude's captions)",
        "auto": "Auto — word-by-word (Whisper, slower)",
        "off": "Off",
    }[m],
)
cfg.render["captions_mode"] = cap_mode
if cap_mode == "auto":
    st.sidebar.caption("🎤 Transcribes speech & animates captions. First run "
                       "downloads a small model; adds a second render pass.")

if st.session_state.clips is None:
    scan_clips(cfg)
clips = st.session_state.clips or []
st.sidebar.metric("Source clips", len(clips))

# --------------------------------------------------------------------------- #
# Hero
# --------------------------------------------------------------------------- #
st.title("Turn footage into trend-ready shorts")
st.markdown(
    "<div class='hero-tag'>Claude watches your clips and cuts them to a live "
    "trend — zoom, pacing, captions, music. Rendered vertical, ready to post.</div>",
    unsafe_allow_html=True,
)
st.write("")

tab_auto, tab_manual = st.tabs(["🚀 Auto-Pilot", "🎛️ Manual"])

# --------------------------------------------------------------------------- #
# Auto-Pilot — one click, full pipeline
# --------------------------------------------------------------------------- #
with tab_auto:
    st.subheader("One click → finished short")
    if not clips:
        st.info("No source clips yet. Add some in the **Manual** tab (upload), then come back.")
    a1, a2 = st.columns([2, 1])
    with a1:
        topic = st.text_input(
            "Topic", placeholder="Leave blank to auto-pick a live trend",
            key="auto_topic",
        )
    with a2:
        auto_music = st.file_uploader("Music (optional)", type=MUSIC_TYPES, key="auto_music")
        auto_logo = st.file_uploader("Logo (optional)", type=["png", "jpg", "jpeg"], key="auto_logo")

    go = st.button("🚀 Generate video", type="primary",
                   disabled=not clips or not ff_ok, width="stretch")
    if go:
        try:
            with st.status("Generating your short…", expanded=True) as status:
                if topic.strip():
                    trend = Trend(title=topic.strip(), source="manual", score=1.0)
                else:
                    status.write("🧠 Figuring out what your video is about…")
                    analyzer = ClipAnalyzer(cfg)
                    brief = analyzer.analyze(clips)
                    st.session_state.analyze_usage = analyzer.last_usage
                    status.write(f"→ **{brief.subject}**  ·  niche: {brief.niche}")
                    status.write("🔎 Finding trends related to your footage…")
                    trends = gather_trends(cfg, context=brief.search_query)
                    trend = trends[0] if trends else Trend(title=brief.subject, source="fallback")
                edl, out_path = run_pipeline(
                    cfg, clips, trend,
                    stash_upload(cfg, auto_music, ".music"),
                    stash_upload(cfg, auto_logo, ".logo"),
                    status,
                )
                set_edl(edl)
                st.session_state.render_path = out_path
                status.update(label="Done ✅", state="complete", expanded=False)
        except Exception as exc:  # noqa: BLE001
            st.error(f"Generation failed: {exc}")

    if st.session_state.edl and st.session_state.render_path:
        st.divider()
        show_result(st.session_state.render_path)
        show_edl(cfg, st.session_state.edl)

# --------------------------------------------------------------------------- #
# Manual — step by step
# --------------------------------------------------------------------------- #
with tab_manual:
    # Step 1 — clips
    st.subheader("1 · Source clips")
    uploads = st.file_uploader(
        "Upload video clips", type=UPLOAD_TYPES, accept_multiple_files=True,
        key="manual_upload",
    )
    saved = save_uploads(cfg, uploads)
    if saved:
        st.success(f"Uploaded: {', '.join(saved)}")
    if st.button("Rescan folder"):
        scan_clips(cfg)
    clips = st.session_state.clips or []
    if clips:
        st.dataframe(
            [
                {"file": Path(c.path).name, "duration (s)": c.duration,
                 "resolution": f"{c.width}×{c.height}", "audio": "yes" if c.has_audio else "no"}
                for c in clips
            ],
            width="stretch", hide_index=True,
        )
    else:
        st.info("Drop video files above to get started.")

    # Step 2 — trend
    st.subheader("2 · Pick a trend")
    t1, t2 = st.columns([1, 2])
    with t1:
        related = st.checkbox(
            "🎯 Related to my clips", value=bool(clips), disabled=not clips,
            help="Analyze your footage first, then find trends that actually fit it.",
        )
        if st.button("🔎 Gather live trends"):
            try:
                with st.spinner("Analyzing footage…" if related else "Searching the web…"):
                    ctx = None
                    if related and clips:
                        brief = ClipAnalyzer(cfg).analyze(clips)
                        st.session_state.brief = brief
                        ctx = brief.search_query
                    st.session_state.trends = gather_trends(cfg, context=ctx)
            except Exception as exc:  # noqa: BLE001
                st.error(f"Trend gathering failed: {exc}")
        if related and st.session_state.get("brief"):
            b = st.session_state.brief
            st.caption(f"Detected: {b.subject} · {b.niche}")
        custom_topic = st.text_input("…or type a custom topic", key="manual_topic")
    trend_options: list[Trend] = list(st.session_state.trends)
    if custom_topic.strip():
        trend_options = [Trend(title=custom_topic.strip(), source="manual", score=1.0)] + trend_options
    with t2:
        if trend_options:
            labels = [f"[{t.source}] {t.title}" for t in trend_options]
            idx = st.radio("Available trends", options=range(len(trend_options)),
                           format_func=lambda i: labels[i])
            selected_trend = trend_options[idx]
            if selected_trend.summary:
                st.caption(selected_trend.summary)
        else:
            selected_trend = None
            st.info("Gather trends or enter a topic.")

    # Step 3 — edit
    st.subheader("3 · Generate the edit")
    can_decide = bool(clips) and selected_trend is not None
    if st.button("🎬 Ask Claude for an edit", disabled=not can_decide):
        try:
            with st.status("Editing…", expanded=True) as status:
                status.write("🖼️ Sampling keyframes so Claude can see the footage…")
                status.write(f"🎬 Cutting for **{selected_trend.title}**…")
                set_edl(ClaudeEditor(cfg).decide(selected_trend, clips))
                status.update(label="Edit ready ✅", state="complete", expanded=False)
        except Exception as exc:  # noqa: BLE001
            st.error(f"Edit decisioning failed: {exc}")
    if not can_decide:
        st.caption("Need at least one clip and a selected trend.")
    if st.session_state.edl:
        timeline_editor(cfg, clips)

    # Step 4 — render
    st.subheader("4 · Render")
    rc1, rc2 = st.columns(2)
    manual_music = rc1.file_uploader("Background music (optional)", type=MUSIC_TYPES, key="manual_music")
    manual_logo = rc2.file_uploader("Logo / watermark (optional)", type=["png", "jpg", "jpeg"], key="manual_logo")
    if st.button("🎞️ Render video", disabled=st.session_state.edl is None or not ff_ok):
        try:
            with st.status("Rendering…", expanded=True) as status:
                status.write("Applying zoom, crossfades, captions, overlays"
                             + (", music" if manual_music else "") + "…")
                prev = (cfg.render.get("music"), cfg.render.get("logo"))
                try:
                    mp = stash_upload(cfg, manual_music, ".music")
                    lp = stash_upload(cfg, manual_logo, ".logo")
                    if mp:
                        cfg.render["music"] = mp
                    if lp:
                        cfg.render["logo"] = lp
                    out_dir = cfg.path(cfg.render.get("output_dir", "output"))
                    out_path = out_dir / f"{_slugify(st.session_state.edl.title)}.mp4"
                    get_renderer(cfg).render(st.session_state.edl, out_path)
                finally:
                    cfg.render["music"], cfg.render["logo"] = prev
                st.session_state.render_path = str(out_path)
                status.update(label="Rendered ✅", state="complete", expanded=False)
        except Exception as exc:  # noqa: BLE001
            st.error(f"Render failed: {exc}")
    if not ff_ok:
        st.caption("FFmpeg not found — install it to enable rendering.")
    show_result(st.session_state.render_path)
