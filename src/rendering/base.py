"""Renderer interface. FFmpeg is the default backend; a Shotstack (cloud)
backend could implement the same interface later."""

from __future__ import annotations

from abc import ABC, abstractmethod
from pathlib import Path

from ..config import Config
from ..models import EditDecisionList


class Renderer(ABC):
    def __init__(self, cfg: Config) -> None:
        self.cfg = cfg

    @abstractmethod
    def render(self, edl: EditDecisionList, output_path: Path) -> Path:
        """Render an EDL to `output_path` and return the written path."""
        raise NotImplementedError
