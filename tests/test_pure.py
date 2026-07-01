"""Unit tests for the pure logic — no network, no ffmpeg, no Claude.

Covers the bits that break silently: EDL clamping, slugs, token math, caption
styling/escaping, color looks, and the ASS caption builder.
"""

from __future__ import annotations

import pytest

from src.config import Config
from src.decisioning.claude_editor import ClaudeEditor
from src.models import Cut, EditDecisionList, SourceClip
from src.pipeline import _slugify
from src.rendering import captions_ass as ca
from src.rendering.ffmpeg_renderer import (
    LOOKS,
    FFmpegRenderer,
    _escape_drawtext,
    _escape_filter_path,
    _strip_emoji,
)
from src.usage import Usage


# --------------------------------------------------------------------------- #
# pipeline / slug
# --------------------------------------------------------------------------- #
def test_slugify_basic():
    assert _slugify("Hello, World!") == "hello-world"


def test_slugify_empty_falls_back():
    assert _slugify("!!!") == "video"


def test_slugify_truncates():
    assert len(_slugify("a" * 100, maxlen=10)) == 10


# --------------------------------------------------------------------------- #
# usage / token cost
# --------------------------------------------------------------------------- #
def test_usage_cost():
    u = Usage(input=1_000_000, output=1_000_000)
    rates = {"input": 15.0, "output": 75.0, "cache_write": 0, "cache_read": 0}
    assert u.cost(rates) == pytest.approx(90.0)


def test_usage_line_contains_tokens():
    u = Usage(input=1234, output=56)
    assert "1,234" in u.line() and "56" in u.line()


# --------------------------------------------------------------------------- #
# caption text helpers
# --------------------------------------------------------------------------- #
def test_strip_emoji_keeps_text():
    assert _strip_emoji("Hello 🚀 World 🎬") == "Hello  World"


def test_escape_drawtext_escapes_colon_and_percent():
    out = _escape_drawtext("a: 50%")
    assert r"\:" in out and r"\%" in out


def test_escape_filter_path_forward_slashes_and_colon():
    out = _escape_filter_path(r"C:\a\b.cube")
    assert "/" in out and r"\:" in out and "\\" not in out.replace(r"\:", "")


# --------------------------------------------------------------------------- #
# EDL clamping (static — no client needed)
# --------------------------------------------------------------------------- #
def _clip(path="a.mp4", duration=10.0):
    return SourceClip(path=path, duration=duration, width=1080, height=1920, has_audio=True)


def _edl(cuts, **kw):
    return EditDecisionList(title="t", hook="h", cuts=cuts, **kw)


def test_clamp_trims_end_to_duration():
    edl = _edl([Cut(clip_path="a.mp4", start=0, end=20)])
    out = ClaudeEditor._clamp(edl, [_clip(duration=10)])
    assert out.cuts[0].end == 10.0


def test_clamp_drops_unknown_clip():
    edl = _edl([
        Cut(clip_path="known.mp4", start=0, end=5),
        Cut(clip_path="ghost.mp4", start=0, end=5),
    ])
    out = ClaudeEditor._clamp(edl, [_clip(path="known.mp4")])
    assert len(out.cuts) == 1 and out.cuts[0].clip_path == "known.mp4"


def test_clamp_raises_when_no_valid_cuts():
    edl = _edl([Cut(clip_path="ghost.mp4", start=0, end=5)])
    with pytest.raises(RuntimeError):
        ClaudeEditor._clamp(edl, [_clip(path="known.mp4")])


def test_clamp_drops_overlay_past_end():
    from src.models import TextOverlay
    edl = _edl(
        [Cut(clip_path="a.mp4", start=0, end=4)],
        text_overlays=[TextOverlay(text="late", start=99, end=100)],
    )
    out = ClaudeEditor._clamp(edl, [_clip(duration=10)])
    assert out.text_overlays == []


# --------------------------------------------------------------------------- #
# color looks (renderer instance, offline)
# --------------------------------------------------------------------------- #
@pytest.fixture(scope="module")
def renderer():
    return FFmpegRenderer(Config.load("config.yaml"))


def test_all_looks_have_filters():
    # Every look except the explicit no-op should map to a filter chain.
    for name, chain in LOOKS.items():
        assert isinstance(chain, str)
        assert (chain == "") == (name == "none")


def test_look_filter_uses_per_cut_override(renderer):
    cut = Cut(clip_path="a.mp4", start=0, end=2, look="bw")
    assert "hue=s=0" in renderer._look_filter(cut, "vibrant")


def test_look_filter_fine_overrides(renderer):
    cut = Cut(clip_path="a.mp4", start=0, end=2, contrast=1.4, saturation=1.6)
    out = renderer._look_filter(cut, "clean")
    assert "contrast=1.4" in out and "saturation=1.6" in out


def test_fit_size_shrinks_long_text(renderer):
    short = renderer._fit_size("HI", 100)
    long = renderer._fit_size("A" * 60, 100)
    assert long < short <= 100 and long >= 16


def test_crossfade_total_duration(renderer):
    durs = [3.0, 3.0, 3.0]
    parts, vlabel, alabel, total = renderer._crossfade(3, durs, t=0.5)
    assert total == pytest.approx(sum(durs) - 2 * 0.5)
    assert vlabel == "[vout_x]" and alabel == "[aout_x]"


# --------------------------------------------------------------------------- #
# ASS caption builder
# --------------------------------------------------------------------------- #
def test_hex_to_ass_bgr_order():
    assert ca._hex_to_ass("#FFE000") == "&H0000E0FF"


def test_hex_to_ass_bad_input_defaults_white():
    assert ca._hex_to_ass("nope") == "&H00FFFFFF"


def test_ass_time_format():
    assert ca._ass_time(3661.5) == "1:01:01.50"


def test_group_words_splits_on_gap():
    W = ca.Word
    words = [W("a", 0.0, 0.3), W("b", 0.4, 0.7), W("c", 5.0, 5.3)]  # big gap before c
    chunks = ca.group_words(words, max_words=10, max_gap=0.6)
    assert len(chunks) == 2 and chunks[1][0].text == "c"


def test_build_ass_has_events_and_karaoke():
    W = ca.Word
    words = [W("hello", 0.0, 0.4), W("world", 0.5, 0.9)]
    out = ca.build_ass(words, 1080, 1920)
    assert "[Events]" in out and "Dialogue:" in out and r"\kf" in out
