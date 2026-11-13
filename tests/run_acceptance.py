"""Standalone acceptance-test runner.

Executed in isolated mode (no site packages, no PYTHONPATH leakage)::

    python -I tests/run_acceptance.py

It adds only the repository root to ``sys.path`` and runs every ``test_*``
module in this directory with the standard-library ``unittest`` framework.
"""

import os
import sys
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
sys.path.insert(0, os.path.join(ROOT, "tests"))


def main() -> int:
    loader = unittest.TestLoader()
    suite = loader.discover(start_dir=os.path.join(ROOT, "tests"),
                            pattern="test_*.py",
                            top_level_dir=ROOT)
    result = unittest.TextTestRunner(verbosity=2).run(suite)
    return 0 if result.wasSuccessful() else 1


if __name__ == "__main__":
    raise SystemExit(main())
