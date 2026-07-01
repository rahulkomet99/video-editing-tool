"""Pipeline orchestrator: ties ingestion, decisioning, and rendering together."""

from __future__ import annotations

import re
from pathlib import Path

from .config import Config
from .decisioning import ClaudeEditor
from .ingestion import gather_trends
from .media.probe import probe_assets
from .models import RenderResult, Trend
from .rendering import get_renderer


def _slugify(text: str, maxlen: int = 40) -> str:
    slug = re.sub(r"[^a-zA-Z0-9]+", "-", text.strip().lower()).strip("-")
    return (slug or "video")[:maxlen]


class Pipeline:
    def __init__(self, cfg: Config) -> None:
        self.cfg = cfg
        self.editor = ClaudeEditor(cfg)
        self.renderer = get_renderer(cfg)

    def run(self, limit: int = 1) -> list[RenderResult]:
        """Ingest trends, edit for the top `limit`, render each. Returns results."""
        assets_dir = self.cfg.path(self.cfg.media.get("assets_dir", "assets"))
        ffprobe = self.cfg.media.get("ffprobe", "ffprobe")

        print("[1/4] Probing source clips...")
        clips = probe_assets(assets_dir, ffprobe=ffprobe)
        if not clips:
            raise SystemExit(
                f"No source clips found in {assets_dir}. Add video files and retry."
            )
        print(f"      found {len(clips)} clip(s).")

        print("[2/4] Gathering trends...")
        trends = gather_trends(self.cfg)
        if not trends:
            raise SystemExit("No trends gathered. Check ingestion config / creds.")
        print(f"      got {len(trends)} trend(s).")

        out_dir = self.cfg.path(self.cfg.render.get("output_dir", "output"))
        results: list[RenderResult] = []
        for trend in trends[:limit]:
            print(f"[3/4] Deciding edit for: {trend.title!r}")
            edl = self.editor.decide(trend, clips)
            out_path = out_dir / f"{_slugify(edl.title)}.mp4"

            print(f"[4/4] Rendering -> {out_path}")
            self.renderer.render(edl, out_path)
            results.append(RenderResult(output_path=str(out_path), edl=edl, trend=trend))
            print(f"      done: {out_path}")

        return results
