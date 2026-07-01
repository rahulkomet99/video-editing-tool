# Automated Video Editing Pipeline

Short-form video pipeline that uses the **Claude API** for edit decisioning and
**FFmpeg** for rendering, driven by **trend-based content ingestion**.

```
trends  ──▶  Claude (edit decisioning)  ──▶  FFmpeg (render)  ──▶  vertical .mp4
(ingest)     produces an Edit Decision List     cuts/scales/captions
```

## How it works

1. **Ingest trends** from pluggable sources (see below).
2. **Probe source clips** in `assets/` with `ffprobe` (duration, resolution, audio).
3. **Decide the edit (with vision)** — FFmpeg samples keyframes from each clip
   and passes them to Claude as images, so it edits from what's *actually on
   screen* rather than blind timestamps. Claude returns a validated
   `EditDecisionList` (clips to cut, in/out points, captions, ordering, hook,
   hashtags) via structured outputs.
4. **Render (with effects)** — FFmpeg trims each cut, applies the motion Claude
   chose (Ken Burns **zoom**, **speed** changes), letterboxes to 9:16, burns
   **styled captions**, joins cuts with **crossfades** or hard cuts, mixes an
   optional **music bed**, and adds a gentle fade in/out.

## Trend sources (all free)

Configured in `config.yaml` under `ingestion.sources`, tried in order:

| Source          | Cost                | Needs                                    |
| --------------- | ------------------- | ---------------------------------------- |
| `web_search`    | Anthropic tokens    | Anthropic key — Claude searches live. **Best.** |
| `reddit`        | Free OAuth tier     | `REDDIT_CLIENT_ID` / `REDDIT_CLIENT_SECRET` |
| `local`         | Free                | `data/topics.json` (bundled sample)      |
| `google_trends` | Free, no key        | nothing — **but** Google largely retired the Daily Trends RSS, so it usually returns nothing now. |

Default is `[web_search, local]`. Sources missing credentials are skipped
automatically, and `local` always backfills.

## Setup

**1. Install FFmpeg** (must be on PATH, or set `media.ffmpeg` / `media.ffprobe` in config)

```
Windows:  winget install Gyan.FFmpeg
macOS:    brew install ffmpeg
```

**2. Python deps** — Windows (PowerShell):

```powershell
python -m venv .venv
.\.venv\Scripts\Activate.ps1        # if blocked: Set-ExecutionPolicy -Scope CurrentUser RemoteSigned
pip install -r requirements.txt
```

macOS / Linux (bash):

```bash
python -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
```

**3. Credentials** — create a `.env` file in the project root (it's gitignored)
with your Anthropic key:

```
ANTHROPIC_API_KEY=sk-ant-...
# Optional, for the Reddit trend source:
REDDIT_CLIENT_ID=
REDDIT_CLIENT_SECRET=
```

Or, instead of a key, run `ant auth login` — the SDK picks up that profile.

**4. Add source clips** — drop video files into `assets/`.

Auth note: if `ANTHROPIC_API_KEY` is unset but you've run `ant auth login`, the
Anthropic SDK picks up that profile automatically — no key needed in `.env`.

## Usage

### Web UI (Streamlit)

```bash
streamlit run ui/app.py
```

Walks through the pipeline one stage at a time — scan clips → pick a trend (or
type a custom topic) → generate the edit with Claude → render and preview the
video inline, with a download button. The sidebar shows live status for your
Anthropic key and FFmpeg.

### CLI

```bash
# Preview the trends that would be ingested
python -m src.main trends

# Full pipeline: ingest -> decide -> render (makes 1 video by default)
python -m src.main run --limit 1
```

Rendered videos land in `output/`.

## Configuration

Everything is in [`config.yaml`](config.yaml): the Claude model + effort, how
many keyframes to show Claude per clip, which ingestion sources to use, the
source/output folders, and the render settings.

**Render / effects** (`render:` in config): vertical 1080×1920 @ 30fps H.264 by
default. Caption styling (`caption_size`, `caption_box`, `caption_position`),
`transition_duration` (crossfades), `final_fade`, `zoom_rate` (Ken Burns), and a
background `music` file + `music_volume`. Per-cut **zoom** and **speed** and the
**transition** style are chosen by Claude in the edit decision list, not config.
Music can also be uploaded per-run in the UI.

## Project layout

```
ui/app.py               Streamlit web UI
src/
  main.py               CLI entrypoint (run / trends)
  pipeline.py           orchestrator
  config.py             config.yaml + .env loading
  models.py             Trend, SourceClip, EditDecisionList (the EDL contract)
  ingestion/            TrendSource interface + google_trends / reddit / local
  decisioning/          ClaudeEditor: trend + clips -> validated EDL
  rendering/            Renderer interface + FFmpeg backend
  media/probe.py        ffprobe wrapper
```

## Extending

- **New trend source:** subclass `TrendSource`, register it in
  `src/ingestion/__init__.py`, add its name to `ingestion.sources`.
- **Cloud rendering (Shotstack):** implement the `Renderer` interface in
  `src/rendering/` and register it in `get_renderer()`; the `EditDecisionList`
  maps cleanly onto Shotstack's JSON edit spec.
