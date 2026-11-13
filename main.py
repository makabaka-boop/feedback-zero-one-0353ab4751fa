"""Repository entry point.

Works even under ``python -I`` (isolated mode, which omits the current
working directory and the script directory from ``sys.path``) by adding its
own directory before importing the package::

    python3 -I main.py plan.json -o schedule.json
"""

import os
import sys

ROOT = os.path.dirname(os.path.abspath(__file__))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

from sampling.cli import main  # noqa: E402

if __name__ == "__main__":
    raise SystemExit(main())
