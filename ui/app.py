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

# Make `import src...` work regardless of where streamlit is launched from.
_ROOT = Path(__file__).resolve().parent.parent
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

import streamlit as st

from src.config import Config
from src.decisioning import ClaudeEditor, ClipAnalyzer
from src.ingestion import gather_trends
from src.media.keyframes import extract_frame
from src.media.probe import probe_assets
from src.models import Trend
from src.pipeline import _slugify
from src.rendering import get_renderer

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
        "render_path": None,
        "uploaded_sigs": set(),
    }.items():
        st.session_state.setdefault(k, v)


def scan_clips(cfg: Config):
    st.session_state.clips = probe_assets(
        cfg.path(cfg.media.get("assets_dir", "assets")),
        ffprobe=cfg.media.get("ffprobe", "ffprobe"),
    )
    return st.session_state.clips


def save_uploads(cfg: Config, uploads) -> list[str]:
    """Persist uploaded clips into the assets folder; return new filenames."""
    assets_dir = cfg.path(cfg.media.get("assets_dir", "assets"))
    assets_dir.mkdir(parents=True, exist_ok=True)
    saved: list[str] = []
    for uf in uploads or []:
        sig = (uf.name, uf.size)
        if sig in st.session_state.uploaded_sigs:
            continue
        (assets_dir / Path(uf.name).name).write_bytes(uf.getbuffer())
        st.session_state.uploaded_sigs.add(sig)
        saved.append(Path(uf.name).name)
    if saved:
        scan_clips(cfg)
    return saved


def stash_music(cfg: Config, music_file) -> str | None:
    if music_file is None:
        return None
    out_dir = cfg.path(cfg.render.get("output_dir", "output"))
    mdir = out_dir / ".music"
    mdir.mkdir(parents=True, exist_ok=True)
    p = mdir / Path(music_file.name).name
    p.write_bytes(music_file.getbuffer())
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
    c4.metric("Hashtags", len(edl.hashtags))

    st.markdown(f"#### {edl.title}")
    st.caption(f"Hook — {edl.hook}")
    if edl.hashtags:
        st.markdown(" ".join(f"`#{h}`" for h in edl.hashtags))

    st.markdown("**Storyboard**")
    cuts = edl.cuts
    per_row = 4
    for row_start in range(0, len(cuts), per_row):
        row = cuts[row_start : row_start + per_row]
        cols = st.columns(per_row)
        for col, (idx, cut) in zip(cols, enumerate(row, start=row_start)):
            with col, st.container(border=True):
                img = thumb(cfg, cut.clip_path, (cut.start + cut.end) / 2)
                if img:
                    st.image(str(img), use_container_width=True)
                st.markdown(f"**{idx + 1}. {cut.caption or '—'}**")
                badges = f"{cut.start:.1f}–{cut.end:.1f}s"
                if cut.zoom != "none":
                    badges += f" · 🔍 {cut.zoom}"
                if abs(cut.speed - 1.0) > 1e-3:
                    badges += f" · ⏩ {cut.speed}×"
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


def run_pipeline(cfg, clips, trend, music_path, status) -> tuple:
    """Shared trend→edit→render flow with live status writes."""
    status.write(f"🔎 Trend: **{trend.title}**")
    status.write("🎬 Watching the footage and cutting (Claude)…")
    edl = ClaudeEditor(cfg).decide(trend, clips)
    status.write(f"→ {len(edl.cuts)} cuts · {edl.transition} · "
                 f"~{round(sum(c.end - c.start for c in edl.cuts), 1)}s")
    status.write("🎞️ Rendering — zoom, crossfades, captions"
                 + (", music" if music_path else "") + "…")
    out_dir = cfg.path(cfg.render.get("output_dir", "output"))
    out_path = out_dir / f"{_slugify(edl.title)}.mp4"
    prev_music = cfg.render.get("music")
    try:
        if music_path:
            cfg.render["music"] = music_path
        get_renderer(cfg).render(edl, out_path)
    finally:
        cfg.render["music"] = prev_music
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

    go = st.button("🚀 Generate video", type="primary",
                   disabled=not clips or not ff_ok, use_container_width=True)
    if go:
        try:
            with st.status("Generating your short…", expanded=True) as status:
                if topic.strip():
                    trend = Trend(title=topic.strip(), source="manual", score=1.0)
                else:
                    status.write("🧠 Figuring out what your video is about…")
                    brief = ClipAnalyzer(cfg).analyze(clips)
                    status.write(f"→ **{brief.subject}**  ·  niche: {brief.niche}")
                    status.write("🔎 Finding trends related to your footage…")
                    trends = gather_trends(cfg, context=brief.search_query)
                    trend = trends[0] if trends else Trend(title=brief.subject, source="fallback")
                edl, out_path = run_pipeline(
                    cfg, clips, trend, stash_music(cfg, auto_music), status
                )
                st.session_state.edl = edl
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
                st.session_state.edl = ClaudeEditor(cfg).decide(selected_trend, clips)
                st.session_state.render_path = None
                status.update(label="Edit ready ✅", state="complete", expanded=False)
        except Exception as exc:  # noqa: BLE001
            st.error(f"Edit decisioning failed: {exc}")
    if not can_decide:
        st.caption("Need at least one clip and a selected trend.")
    if st.session_state.edl:
        show_edl(cfg, st.session_state.edl)

    # Step 4 — render
    st.subheader("4 · Render")
    manual_music = st.file_uploader("Background music (optional)", type=MUSIC_TYPES, key="manual_music")
    if st.button("🎞️ Render video", disabled=st.session_state.edl is None or not ff_ok):
        try:
            with st.status("Rendering…", expanded=True) as status:
                status.write("Applying zoom, crossfades, captions"
                             + (", music" if manual_music else "") + "…")
                prev_music = cfg.render.get("music")
                try:
                    mp = stash_music(cfg, manual_music)
                    if mp:
                        cfg.render["music"] = mp
                    out_dir = cfg.path(cfg.render.get("output_dir", "output"))
                    out_path = out_dir / f"{_slugify(st.session_state.edl.title)}.mp4"
                    get_renderer(cfg).render(st.session_state.edl, out_path)
                finally:
                    cfg.render["music"] = prev_music
                st.session_state.render_path = str(out_path)
                status.update(label="Rendered ✅", state="complete", expanded=False)
        except Exception as exc:  # noqa: BLE001
            st.error(f"Render failed: {exc}")
    if not ff_ok:
        st.caption("FFmpeg not found — install it to enable rendering.")
    show_result(st.session_state.render_path)
