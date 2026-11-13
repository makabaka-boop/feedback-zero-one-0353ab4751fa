"""Command line entry point.

Usage::

    python -m sampling.cli plan.json -o schedule.json
    python -m sampling.cli plan.json            # write to stdout
    cat plan.json | python -m sampling.cli - -o schedule.json

The output file is replaced atomically: the result is first written to a
temporary file in the same directory and then renamed over the destination,
so a failed run always leaves any pre-existing output file untouched.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import tempfile

from .errors import SamplingError
from .scheduler import build_schedule


def _read_input(path: str) -> bytes:
    if path == "-":
        return sys.stdin.buffer.read()
    with open(path, "rb") as handle:
        return handle.read()


def _atomic_write(path: str, payload: bytes) -> None:
    directory = os.path.dirname(os.path.abspath(path))
    os.makedirs(directory, exist_ok=True)
    fd, tmp_name = tempfile.mkstemp(
        prefix=f".{os.path.basename(path)}.", suffix=".tmp", dir=directory
    )
    try:
        with os.fdopen(fd, "wb") as handle:
            handle.write(payload)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(tmp_name, path)
    except BaseException:
        try:
            os.unlink(tmp_name)
        except FileNotFoundError:
            pass
        raise


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Expand a local-time sampling plan into UTC occurrences."
    )
    parser.add_argument("input", help="plan JSON file, or - for stdin")
    parser.add_argument(
        "-o",
        "--output",
        help="output JSON file (default: stdout); replaced atomically",
    )
    args = parser.parse_args(argv)

    try:
        document = _read_input(args.input)
        result = build_schedule(document)
    except SamplingError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1
    except FileNotFoundError as exc:
        print(f"error: cannot read input: {exc}", file=sys.stderr)
        return 1

    payload = (json.dumps(result, ensure_ascii=False, separators=(",", ":"))
               + "\n").encode("utf-8")

    if args.output is None:
        sys.stdout.buffer.write(payload)
        return 0

    try:
        _atomic_write(args.output, payload)
    except OSError as exc:
        print(f"error: cannot write output: {exc}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
