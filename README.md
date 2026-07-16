# Automated Video Editing Pipeline

Short-form video pipeline that uses the **Claude API** for edit decisioning and
**FFmpeg** for rendering, driven by **trend-based content ingestion**. It turns
raw footage into a finished vertical (9:16) short — cut, graded, captioned, and
ready to post.

```
footage ─▶ Claude sees the frames ─▶ Edit Decision List ─▶ FFmpeg render ─▶ 9:16 .mp4
trends  ─┘  (+ related live trends)     cuts · looks · captions · overlays · music
```

## What it does

1. **Understand the footage (content-aware).** Claude looks at sample frames and
   figures out what the clips are about, then finds live trends *related to your
   content* instead of forcing it onto a random one. (UI Auto-Pilot.)
2. **Ingest trends** from pluggable free sources (see below).
3. **Probe clips** in `assets/` with `ffprobe` (duration, resolution, audio).
4. **Decide the edit — with vision.** FFmpeg samples keyframes per clip and sends
   them to Claude as images, so it cuts from what's *actually on screen*. Claude
   returns a validated `EditDecisionList` via structured outputs.
5. **Render — with real effects.** FFmpeg trims each cut and applies:
   - **Motion:** Ken Burns **zoom**, **speed** ramps, **crossfade** or hard cuts.
   - **Color looks:** a named grade per video (and per shot) — `clean`, `vibrant`,
     `cinematic`, `warm`, `cool`, `moody`, `vintage`, `bw` — plus optional fine
     contrast/saturation/brightness and `.cube` **LUTs**.
   - **Text:** bold, outlined per-cut **captions** and timeline **text overlays**
     (hero title + callouts). Or **auto word-by-word captions** (see below).
   - **Branding & audio:** a **logo/watermark** overlay and a **music** bed.

## Word-by-word auto captions

Set `captions_mode: auto` (config or the UI sidebar) and the pipeline transcribes
the speech with **faster-whisper** and burns karaoke-highlighted word-by-word
captions (via libass) — the modern TikTok/Reels look. Best for talking footage;
if there's no speech it falls back to Claude's written captions. `whisper_model`
and `caption_highlight` are configurable. `manual` (default) burns Claude's
per-cut captions; `off` burns none.

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

**1. Install FFmpeg** (must be on PATH, or set `media.ffmpeg` / `media.ffprobe` in
config). Needs `libass` (for captions) and `libfreetype` — the standard builds
below include them.

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

`faster-whisper` is included for auto captions; the first `auto` render downloads
a small speech model. The rest of the pipeline runs fine even if it's missing.

**3. Credentials** — create a `.env` file in the project root (it's gitignored)
with your Anthropic key:

```
ANTHROPIC_API_KEY=sk-ant-...
# Optional, for the Reddit trend source:
REDDIT_CLIENT_ID=
REDDIT_CLIENT_SECRET=
```

Or, instead of a key, run `ant auth login` — the SDK picks up that profile.

**4. Add source clips** — drop video files into `assets/` (or upload them in the UI).

## Usage

### Web UI (Streamlit)

```bash
streamlit run ui/app.py
```

- **🚀 Auto-Pilot** — one click runs the whole pipeline (analyze footage → find
  related trend → edit → render) with live status and a token/cost readout.
- **🎛️ Manual** — step through it, then fine-tune the edit in an editable
  **timeline** (cut in/out, caption, zoom, speed, per-shot look, fine grade) and
  re-render. Upload music/logo per run; choose the caption mode in the sidebar.

Both show a storyboard of the edit and play the result inline with a download
button. The sidebar shows live status for your Anthropic key and FFmpeg.

### CLI

```bash
# Preview the trends that would be ingested
python -m src.main trends

# Full pipeline: ingest -> decide -> render (makes 1 video by default)
python -m src.main run --limit 1
```

Rendered videos land in `output/`.

### HTTP API (async job service)

An async render API so renders don't block callers and can be metered per user.

```bash
uvicorn src.service.api:create_app --factory --port 8000
```

Interactive docs at `/docs`. Endpoints (send `X-API-Key` when keys are set):

```bash
# Submit a render job (returns immediately with a job id)
curl -XPOST localhost:8000/jobs -H 'X-API-Key: KEY' \
     -H 'content-type: application/json' -d '{"topic": "diy robots"}'

curl localhost:8000/jobs/<id>            -H 'X-API-Key: KEY'   # status
curl localhost:8000/jobs/<id>/download   -H 'X-API-Key: KEY' -o out.mp4
curl localhost:8000/usage                -H 'X-API-Key: KEY'   # your token/cost totals
```

Jobs run on a background worker pool (`service.max_workers`), state persists in
SQLite (`service.db_path`), outputs go through a pluggable store
(`service.storage_dir`), and each job records its own token usage/cost. Set
`service.api_keys` (`["key:caller"]`) or `SERVICE_API_KEYS` to require auth;
empty = open dev mode.

### Docker

```bash
docker compose up --build      # UI on :8501, API on :8000
```

Both services build from the one `Dockerfile` (ffmpeg + fonts included) and read
`ANTHROPIC_API_KEY` / `SERVICE_API_KEYS` from your local `.env`. `assets/` and
`output/` are mounted as volumes.

## Configuration

Everything is in [`config.yaml`](config.yaml). Highlights:

- **`claude:`** model + effort, keyframes shown per clip, and `pricing` (per-1M
  token rates used only for the logged cost estimate — token counts are exact).
- **`ingestion:`** which sources to use and their options.
- **`render:`** vertical 1080×1920 @ 30fps H.264 by default, plus:
  - Captions — `captions_mode` (manual/auto/off), `whisper_model`,
    `caption_highlight`, and manual-caption styling (`caption_size`,
    `caption_box`, `caption_position`).
  - Color — `look` (default grade), `lut` + `luts_dir` (optional `.cube`).
  - Effects — `transition_duration`, `final_fade`, `zoom_rate`.
  - Branding/audio — `logo` (+ position/scale/opacity), `music` + `music_volume`.

Per-cut **zoom/speed/look** and the video-level **look/transition** are chosen by
Claude in the EDL, not config. Music and logo can also be uploaded per run in the UI.

## Development

```bash
pip install -r requirements-dev.txt
pytest            # unit tests for the pure logic
ruff check .      # lint
mypy src          # type-check
```

## Project layout

```
ui/app.py                 Streamlit UI (Auto-Pilot + Manual tabs)
src/
  main.py                 CLI entrypoint (run / trends)
  pipeline.py             orchestrator
  config.py               config.yaml + .env loading
  log.py                  central logging setup
  usage.py                Claude token usage + cost logging
  models.py               Trend, SourceClip, EditDecisionList (the EDL contract), looks
  ingestion/              TrendSource interface + web_search / reddit / google_trends / local
  decisioning/
    claude_editor.py      trend + clips -> validated EDL (vision + prompt caching)
    content_analyzer.py   footage -> ContentBrief (for content-aware trends)
  rendering/
    ffmpeg_renderer.py    FFmpeg backend (cuts, looks, captions, overlays, music)
    captions_ass.py       word-by-word ASS caption builder
  media/
    probe.py              ffprobe wrapper
    keyframes.py          frame sampling / extraction
    transcribe.py         faster-whisper word timings (optional)
    uploads.py            upload size/count/duration guards
    run.py                timeout-bounded subprocess runner
  service/                async HTTP job API (FastAPI)
    api.py                endpoints (jobs / download / usage / healthz)
    jobs.py               SQLite job store
    worker.py             thread-pool runner (Celery-swappable)
    storage.py            output storage (LocalStorage; S3-swappable)
    auth.py               API-key auth
tests/                    pytest unit tests (pipeline + service)
```

## Extending

- **New trend source:** subclass `TrendSource`, register it in
  `src/ingestion/__init__.py`, add its name to `ingestion.sources`.
- **Cloud rendering (Shotstack):** implement the `Renderer` interface in
  `src/rendering/` and register it in `get_renderer()`; the `EditDecisionList`
  maps cleanly onto Shotstack's JSON edit spec.
- **New color look:** add an entry to `LOOKS` in `src/rendering/ffmpeg_renderer.py`
  and the `Look` type in `src/models.py`.
- **Scale out:** the job API is single-node (thread pool + SQLite + local files).
  For multiple workers, swap `JobRunner` for a Celery/RQ task calling the same
  `default_run`, `LocalStorage` for an S3/MinIO `Storage`, and SQLite for Postgres.
```
