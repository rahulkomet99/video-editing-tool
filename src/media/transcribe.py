"""Speech-to-text with word-level timestamps via faster-whisper.

Used to auto-generate word-by-word animated captions. faster-whisper is
imported lazily and cached per model size, so the rest of the pipeline runs
fine without it installed — callers just get an empty list (and a warning) if
it's unavailable or the footage has no speech.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from ..log import get_logger

log = get_logger(__name__)

_MODELS: dict[tuple, object] = {}


@dataclass
class Word:
    text: str
    start: float
    end: float


def _get_model(size: str, compute_type: str = "int8"):
    key = (size, compute_type)
    if key not in _MODELS:
        # Quiet the harmless "symlinks not supported" warning on Windows.
        import os

        os.environ.setdefault("HF_HUB_DISABLE_SYMLINKS_WARNING", "1")
        from faster_whisper import WhisperModel  # lazy: optional dependency

        log.info("Loading whisper model %r (%s) …", size, compute_type)
        _MODELS[key] = WhisperModel(size, device="cpu", compute_type=compute_type)
    return _MODELS[key]


def transcribe_words(
    media_path: str | Path, model_size: str = "base", language: str | None = None
) -> list[Word]:
    """Return word-level timings for the speech in `media_path` (in that file's
    own timeline). Empty list if faster-whisper is missing, errors, or there's
    no detectable speech."""
    try:
        model = _get_model(model_size)
    except Exception as exc:  # noqa: BLE001 — optional dep / load failure
        log.warning("faster-whisper unavailable (%s) — skipping auto captions.", exc)
        return []
    try:
        segments, _info = model.transcribe(
            str(media_path), word_timestamps=True, language=language, vad_filter=True
        )
        words: list[Word] = []
        for seg in segments:
            for w in getattr(seg, "words", None) or []:
                text = (w.word or "").strip()
                if text:
                    words.append(Word(text=text, start=float(w.start), end=float(w.end)))
        log.info("Transcribed %d word(s) from %s", len(words), Path(media_path).name)
        return words
    except Exception as exc:  # noqa: BLE001
        log.warning("Transcription failed (%s) — skipping auto captions.", exc)
        return []
