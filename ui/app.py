"""Streamlit UI for the automated video editing pipeline.

Run from the project root:
    streamlit run ui/app.py

It drives the same modules the CLI uses, one stage at a time:
    1. scan source clips (assets/)
    2. gather trends (or type a custom topic)
    3. ask Claude for an edit decision list
    4. render with FFmpeg and preview the result inline
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
from src.decisioning import ClaudeEditor
from src.ingestion import gather_trends
from src.media.probe import probe_assets
from src.models import Trend
from src.pipeline import _slugify
from src.rendering import get_renderer

st.set_page_config(page_title="Auto Video Editor", page_icon="🎬", layout="wide")


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
    st.session_state.setdefault("clips", None)
    st.session_state.setdefault("trends", [])
    st.session_state.setdefault("edl", None)
    st.session_state.setdefault("render_path", None)
    st.session_state.setdefault("uploaded_sigs", set())


# File extensions accepted by the uploader (no leading dot, per Streamlit).
UPLOAD_TYPES = ["mp4", "mov", "mkv", "webm", "avi", "m4v"]


ss_init()

# --------------------------------------------------------------------------- #
# Sidebar: config + environment status
# --------------------------------------------------------------------------- #
st.sidebar.title("🎬 Auto Video Editor")
config_path = st.sidebar.text_input("Config file", value="config.yaml")

try:
    cfg = load_config(config_path)
except Exception as exc:  # noqa: BLE001
    st.sidebar.error(f"Could not load config: {exc}")
    st.stop()

st.sidebar.subheader("Environment")
api_ok = bool(cfg.anthropic_api_key)
ff_ok = ffmpeg_ready(cfg)
st.sidebar.markdown(
    f"- Anthropic key: {'✅' if api_ok else '⚠️ not set (may use `ant` profile)'}\n"
    f"- FFmpeg: {'✅' if ff_ok else '❌ not found'}\n"
    f"- Model: `{cfg.claude.get('model', 'claude-opus-4-8')}`\n"
    f"- Sources: `{', '.join(cfg.ingestion.get('sources', []))}`"
)
if not ff_ok:
    st.sidebar.warning("Rendering will fail until FFmpeg is on PATH.")

r = cfg.render
st.sidebar.caption(
    f"Output: {r.get('width', 1080)}×{r.get('height', 1920)} @ {r.get('fps', 30)}fps"
)

# --------------------------------------------------------------------------- #
# Step 1 — source clips
# --------------------------------------------------------------------------- #
st.header("1 · Source clips")
assets_dir = cfg.path(cfg.media.get("assets_dir", "assets"))
ffprobe = cfg.media.get("ffprobe", "ffprobe")
st.caption(f"Source folder: `{assets_dir}`")

uploads = st.file_uploader(
    "Upload video clips",
    type=UPLOAD_TYPES,
    accept_multiple_files=True,
    help="Saved into the source folder and probed automatically.",
)
if uploads:
    assets_dir.mkdir(parents=True, exist_ok=True)
    saved: list[str] = []
    for uf in uploads:
        sig = (uf.name, uf.size)
        if sig in st.session_state.uploaded_sigs:
            continue  # already saved this session — don't rewrite every rerun
        dest = assets_dir / Path(uf.name).name  # strip any path components
        dest.write_bytes(uf.getbuffer())
        st.session_state.uploaded_sigs.add(sig)
        saved.append(dest.name)
    if saved:
        st.session_state.clips = probe_assets(assets_dir, ffprobe=ffprobe)
        st.success(f"Uploaded: {', '.join(saved)}")

if st.button("Rescan folder") or st.session_state.clips is None:
    st.session_state.clips = probe_assets(assets_dir, ffprobe=ffprobe)

clips = st.session_state.clips or []
if clips:
    st.dataframe(
        [
            {
                "file": Path(c.path).name,
                "duration (s)": c.duration,
                "resolution": f"{c.width}×{c.height}",
                "audio": "yes" if c.has_audio else "no",
            }
            for c in clips
        ],
        width='stretch',
        hide_index=True,
    )
else:
    st.info(f"No clips found. Drop video files into `{assets_dir}` and click **Scan clips**.")

# --------------------------------------------------------------------------- #
# Step 2 — trends
# --------------------------------------------------------------------------- #
st.header("2 · Pick a trend")
col_a, col_b = st.columns([1, 2])

with col_a:
    if st.button("Gather trends"):
        with st.spinner("Fetching trends..."):
            st.session_state.trends = gather_trends(cfg)
    custom_topic = st.text_input("…or type a custom topic")

trend_options: list[Trend] = list(st.session_state.trends)
if custom_topic.strip():
    trend_options = [Trend(title=custom_topic.strip(), source="manual", score=1.0)] + trend_options

with col_b:
    if trend_options:
        labels = [f"[{t.source}] {t.title}" for t in trend_options]
        idx = st.radio(
            "Available trends",
            options=range(len(trend_options)),
            format_func=lambda i: labels[i],
        )
        selected_trend = trend_options[idx]
        if selected_trend.summary:
            st.caption(selected_trend.summary)
    else:
        selected_trend = None
        st.info("Click **Gather trends** or enter a custom topic.")

# --------------------------------------------------------------------------- #
# Step 3 — edit decisioning (Claude)
# --------------------------------------------------------------------------- #
st.header("3 · Generate the edit")
can_decide = bool(clips) and selected_trend is not None
if st.button("Ask Claude for an edit", disabled=not can_decide):
    try:
        with st.spinner(f"Claude is editing for “{selected_trend.title}”…"):
            editor = ClaudeEditor(cfg)
            st.session_state.edl = editor.decide(selected_trend, clips)
            st.session_state.render_path = None  # invalidate stale render
    except Exception as exc:  # noqa: BLE001
        st.error(f"Edit decisioning failed: {exc}")

if not can_decide:
    st.caption("Need at least one source clip and a selected trend.")

edl = st.session_state.edl
if edl:
    st.subheader(edl.title)
    st.markdown(f"**Hook:** {edl.hook}")
    if edl.hashtags:
        st.markdown(" ".join(f"`#{h}`" for h in edl.hashtags))
    st.dataframe(
        [
            {
                "#": i + 1,
                "clip": Path(c.clip_path).name,
                "start": c.start,
                "end": c.end,
                "caption": c.caption or "",
            }
            for i, c in enumerate(edl.cuts)
        ],
        width='stretch',
        hide_index=True,
    )
    total = round(sum(c.end - c.start for c in edl.cuts), 1)
    st.caption(f"{len(edl.cuts)} cuts · ~{total}s total")
    if edl.rationale:
        with st.expander("Claude's rationale"):
            st.write(edl.rationale)

# --------------------------------------------------------------------------- #
# Step 4 — render
# --------------------------------------------------------------------------- #
st.header("4 · Render")

music_file = st.file_uploader(
    "Background music (optional)",
    type=["mp3", "m4a", "aac", "wav", "ogg"],
    help="Mixed under the video, looped and faded to fit. Great for silent clips.",
)

if st.button("Render video", disabled=edl is None or not ff_ok):
    # cfg is cached across reruns, so override music just for this render and
    # restore afterward to avoid leaking it into later renders.
    prev_music = cfg.render.get("music")
    try:
        out_dir = cfg.path(cfg.render.get("output_dir", "output"))
        out_path = out_dir / f"{_slugify(edl.title)}.mp4"

        if music_file is not None:
            music_dir = out_dir / ".music"
            music_dir.mkdir(parents=True, exist_ok=True)
            music_path = music_dir / Path(music_file.name).name
            music_path.write_bytes(music_file.getbuffer())
            cfg.render["music"] = str(music_path)

        with st.spinner("Rendering with FFmpeg…"):
            get_renderer(cfg).render(edl, out_path)
        st.session_state.render_path = str(out_path)
    except Exception as exc:  # noqa: BLE001
        st.error(f"Render failed: {exc}")
    finally:
        cfg.render["music"] = prev_music

if not ff_ok:
    st.caption("FFmpeg not found — install it to enable rendering.")

if st.session_state.render_path and Path(st.session_state.render_path).exists():
    path = st.session_state.render_path
    st.success(f"Rendered: {path}")
    st.video(path)
    with open(path, "rb") as fh:
        st.download_button("Download .mp4", fh, file_name=Path(path).name, mime="video/mp4")
