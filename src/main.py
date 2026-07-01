"""CLI entrypoint.

Usage:
  python -m src.main run [--config config.yaml] [--limit 1]
  python -m src.main trends [--config config.yaml]   # just print gathered trends
"""

from __future__ import annotations

import argparse
import json
import sys

# Claude often includes emoji in titles/captions/hashtags; the Windows console
# defaults to cp1252 and raises UnicodeEncodeError on print(). Force UTF-8.
for _stream in (sys.stdout, sys.stderr):
    try:
        _stream.reconfigure(encoding="utf-8")  # type: ignore[union-attr]
    except (AttributeError, ValueError):
        pass

from .config import Config
from .ingestion import gather_trends
from .log import configure
from .pipeline import Pipeline


def _cmd_run(args: argparse.Namespace) -> int:
    cfg = Config.load(args.config)
    results = Pipeline(cfg).run(limit=args.limit)
    print("\n=== Rendered ===")
    for r in results:
        print(f"- {r.edl.title}")
        print(f"  file:     {r.output_path}")
        print(f"  trend:    {r.trend.title} ({r.trend.source})")
        print(f"  hashtags: {' '.join('#' + h for h in r.edl.hashtags)}")
    return 0


def _cmd_trends(args: argparse.Namespace) -> int:
    cfg = Config.load(args.config)
    trends = gather_trends(cfg)
    print(json.dumps([t.model_dump() for t in trends], indent=2))
    return 0


def main(argv: list[str] | None = None) -> int:
    # Shared options available both before and after the subcommand.
    common = argparse.ArgumentParser(add_help=False)
    common.add_argument("--config", default="config.yaml", help="Path to config.yaml")

    parser = argparse.ArgumentParser(
        description="Automated video editing pipeline.", parents=[common]
    )
    sub = parser.add_subparsers(dest="command", required=True)

    p_run = sub.add_parser("run", parents=[common], help="Ingest -> decide -> render.")
    p_run.add_argument("--limit", type=int, default=1, help="How many videos to make.")
    p_run.set_defaults(func=_cmd_run)

    p_trends = sub.add_parser(
        "trends", parents=[common], help="Print gathered trends and exit."
    )
    p_trends.set_defaults(func=_cmd_trends)

    args = parser.parse_args(argv)
    configure()
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())
