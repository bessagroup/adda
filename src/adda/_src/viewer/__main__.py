"""CLI entry point for the read-only live run viewer.

Usage
-----
    python -m adda.viewer <study-dir>

Requires the ``viewer`` optional-dependency group
(``pip install adda[viewer]``).

Options
-------
--host HOST   Interface to bind (default: 127.0.0.1 — local-only).
--port PORT   Port to bind (default: 8765).

The viewer prints a ``/session?token=...`` URL at start. Reads are open; opening
that URL once lets the browser write (notes, answers). Without it the page is
read-only.
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="python -m adda.viewer",
        description=(
            "Serve a read-only live web viewer for adda agentic runs."
        ),
    )
    parser.add_argument(
        "study_dir",
        metavar="study-dir",
        type=Path,
        help="Path to the study directory whose runs/ this viewer serves.",
    )
    parser.add_argument(
        "--host", default="127.0.0.1", metavar="HOST",
        help="Interface to bind (default: 127.0.0.1).",
    )
    parser.add_argument(
        "--port", type=int, default=8765, metavar="PORT",
        help="Port to bind (default: 8765).",
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    parser = _build_parser()
    args = parser.parse_args(argv)

    try:
        from .app import run_viewer
    except ImportError as exc:
        print(
            "Error: the viewer requires the 'viewer' optional-dependency "
            "group — install with `pip install adda[viewer]`.\n"
            f"({exc})",
            file=sys.stderr,
        )
        return 1

    run_viewer(args.study_dir, host=args.host, port=args.port)
    return 0


if __name__ == "__main__":
    sys.exit(main())
