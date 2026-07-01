"""Base interface for trend sources."""

from __future__ import annotations

from abc import ABC, abstractmethod

from ..config import Config
from ..models import Trend


class TrendSource(ABC):
    """A source of trending topics.

    Subclasses implement `available()` (cheap check for required creds/deps)
    and `fetch()` (the actual network call).
    """

    name: str = "base"

    def __init__(self, cfg: Config) -> None:
        self.cfg = cfg

    def available(self) -> bool:  # noqa: D401 - simple predicate
        """Whether this source can run (creds present, deps installed)."""
        return True

    @abstractmethod
    def fetch(self, context: str | None = None) -> list[Trend]:
        """Return a list of trending topics.

        `context` is an optional hint (e.g. a description of the uploaded
        footage) so sources that support it can surface *related* trends.
        Sources that don't use it simply ignore the argument.
        """
        raise NotImplementedError
