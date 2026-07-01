"""Build an ASS subtitle file for word-by-word animated captions.

Words are grouped into short phrases; within each phrase, karaoke tags (\\kf)
sweep-highlight each word as it's spoken — the classic auto-caption look.
FFmpeg's libass burns the result in a fast second pass.
"""

from __future__ import annotations

from ..media.transcribe import Word


def _ass_time(t: float) -> str:
    t = max(0.0, t)
    h = int(t // 3600)
    m = int((t % 3600) // 60)
    s = t % 60
    return f"{h:d}:{m:02d}:{s:05.2f}"


def _hex_to_ass(color: str) -> str:
    """'#RRGGBB' → ASS '&H00BBGGRR' (opaque). Falls back to white on bad input."""
    c = color.lstrip("#")
    if len(c) != 6:
        return "&H00FFFFFF"
    rr, gg, bb = c[0:2], c[2:4], c[4:6]
    return f"&H00{bb}{gg}{rr}".upper()


def group_words(
    words: list[Word], max_words: int = 4, max_gap: float = 0.6, max_dur: float = 2.5
) -> list[list[Word]]:
    """Chunk a flat word list into readable phrases, breaking on length, a long
    pause, or total duration."""
    chunks: list[list[Word]] = []
    cur: list[Word] = []
    for w in words:
        if cur:
            gap = w.start - cur[-1].end
            dur = w.end - cur[0].start
            if len(cur) >= max_words or gap > max_gap or dur > max_dur:
                chunks.append(cur)
                cur = []
        cur.append(w)
    if cur:
        chunks.append(cur)
    return chunks


def build_ass(
    words: list[Word],
    width: int,
    height: int,
    font: str = "Arial",
    highlight: str = "#FFE000",
    upcoming: str = "#FFFFFF",
    position: str = "bottom",
) -> str:
    """Return the full text of an .ass file rendering `words` as word-by-word
    animated captions sized for a `width`×`height` canvas."""
    fontsize = round(height / 20)  # ~96px on a 1920-tall canvas
    outline = max(3, round(fontsize / 12))
    align = {"bottom": 2, "center": 5, "top": 8}.get(position, 2)
    margin_v = round(height * (0.16 if position == "bottom" else 0.06))
    primary = _hex_to_ass(highlight)   # colour after a word is "sung"
    secondary = _hex_to_ass(upcoming)  # colour before it's sung
    outline_c = "&H00000000"
    back_c = "&HA0000000"

    header = f"""[Script Info]
ScriptType: v4.00+
PlayResX: {width}
PlayResY: {height}
WrapStyle: 2
ScaledBorderAndShadow: yes

[V4+ Styles]
Format: Name, Fontname, Fontsize, PrimaryColour, SecondaryColour, OutlineColour, BackColour, Bold, Italic, Underline, StrikeOut, ScaleX, ScaleY, Spacing, Angle, BorderStyle, Outline, Shadow, Alignment, MarginL, MarginR, MarginV, Encoding
Style: Words,{font},{fontsize},{primary},{secondary},{outline_c},{back_c},1,0,0,0,100,100,0,0,1,{outline},2,{align},60,60,{margin_v},1

[Events]
Format: Layer, Start, End, Style, Name, MarginL, MarginR, MarginV, Effect, Text
"""

    events: list[str] = []
    for chunk in group_words(words):
        start = chunk[0].start
        end = chunk[-1].end + 0.15  # brief hold on the last word
        prev_end = start
        parts: list[str] = []
        for w in chunk:
            gap_cs = round((w.start - prev_end) * 100)
            if gap_cs > 0:
                parts.append(f"{{\\k{gap_cs}}}")
            dur_cs = max(1, round((w.end - w.start) * 100))
            text = w.text.replace("{", "(").replace("}", ")")
            parts.append(f"{{\\kf{dur_cs}}}{text} ")
            prev_end = w.end
        line = "".join(parts).strip()
        events.append(
            f"Dialogue: 0,{_ass_time(start)},{_ass_time(end)},Words,,0,0,0,,{line}"
        )

    return header + "\n".join(events) + "\n"
