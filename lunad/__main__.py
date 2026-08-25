"""``python -m lunad`` — the daemon entry point."""

from __future__ import annotations

import argparse
import logging
import sys

from . import __version__, config, log as luna_log
from .memory import FTS5Unavailable, assert_fts5
from .server import AlreadyRunning, serve


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="lunad", description="Luna daemon")
    parser.add_argument("--agent", default=None,
                        help="override the agent from omarchy defaults")
    parser.add_argument("--debug", action="store_true")
    parser.add_argument("--no-stderr", action="store_true",
                        help="log only to the rotating file")
    parser.add_argument("--version", action="version", version=__version__)
    args = parser.parse_args(argv)

    logger = luna_log.setup(
        level=logging.DEBUG if args.debug else logging.INFO,
        stderr=not args.no_stderr,
    )

    # Checked here, at start, not on first search: tier-2 recall has no
    # graceful degradation worth having, so a missing FTS5 is a start failure.
    try:
        assert_fts5()
    except FTS5Unavailable as exc:
        logger.critical("%s", exc)
        print(f"lunad: {exc}", file=sys.stderr)
        return 78  # EX_CONFIG

    try:
        return serve(args.agent)
    except AlreadyRunning as exc:
        logger.error("%s", exc)
        print(f"lunad: {exc}", file=sys.stderr)
        return 75  # EX_TEMPFAIL
    except FileNotFoundError as exc:
        logger.critical("%s", exc)
        print(f"lunad: {exc}", file=sys.stderr)
        return 78


if __name__ == "__main__":
    raise SystemExit(main())
