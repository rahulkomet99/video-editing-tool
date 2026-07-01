from ..config import Config
from .base import Renderer
from .ffmpeg_renderer import FFmpegRenderer

_BACKENDS: dict[str, type[Renderer]] = {"ffmpeg": FFmpegRenderer}


def get_renderer(cfg: Config) -> Renderer:
    backend = cfg.render.get("backend", "ffmpeg")
    cls = _BACKENDS.get(backend)
    if cls is None:
        raise ValueError(f"Unknown render backend: {backend}")
    return cls(cfg)


__all__ = ["Renderer", "FFmpegRenderer", "get_renderer"]
