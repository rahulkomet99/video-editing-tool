"""Track and log Claude token usage across a run.

Every Claude call funnels its `response.usage` through `record()`, which prints
a one-line breakdown and folds the numbers into a process-wide running total.
Cost is an *estimate* from configurable per-million rates (see
`config.claude.pricing`); token counts themselves are exact.
"""

from __future__ import annotations

from dataclasses import dataclass

from .log import get_logger

log = get_logger(__name__)

# Fallback rates (USD per 1M tokens) if config doesn't override. Opus tier.
DEFAULT_RATES = {"input": 15.0, "output": 75.0, "cache_write": 18.75, "cache_read": 1.5}


@dataclass
class Usage:
    calls: int = 0
    input: int = 0
    output: int = 0
    cache_read: int = 0
    cache_write: int = 0

    def cost(self, rates: dict | None = None) -> float:
        r = rates or DEFAULT_RATES
        return (
            self.input / 1e6 * r.get("input", 0)
            + self.output / 1e6 * r.get("output", 0)
            + self.cache_write / 1e6 * r.get("cache_write", 0)
            + self.cache_read / 1e6 * r.get("cache_read", 0)
        )

    def line(self, rates: dict | None = None) -> str:
        return (
            f"in {self.input:,} · out {self.output:,}"
            + (f" · cache {self.cache_read:,}" if self.cache_read else "")
            + f" · ~${self.cost(rates):.3f}"
        )


# Process-wide running total (persists for the life of the Streamlit server).
TOTAL = Usage()


def record(label: str, model: str, api_usage, rates: dict | None = None) -> Usage:
    """Fold one API call's usage into the running total, print a log line, and
    return the per-call Usage."""
    one = Usage(
        calls=1,
        input=getattr(api_usage, "input_tokens", 0) or 0,
        output=getattr(api_usage, "output_tokens", 0) or 0,
        cache_read=getattr(api_usage, "cache_read_input_tokens", 0) or 0,
        cache_write=getattr(api_usage, "cache_creation_input_tokens", 0) or 0,
    )
    TOTAL.calls += 1
    TOTAL.input += one.input
    TOTAL.output += one.output
    TOTAL.cache_read += one.cache_read
    TOTAL.cache_write += one.cache_write
    # ASCII-only so a cp1252 Windows console can't raise UnicodeEncodeError.
    log.info(
        "tokens %s (%s): in=%d out=%d cache=%d ~$%.3f | run total (%d calls): "
        "in=%d out=%d ~$%.3f",
        label, model, one.input, one.output, one.cache_read, one.cost(rates),
        TOTAL.calls, TOTAL.input, TOTAL.output, TOTAL.cost(rates),
    )
    return one
