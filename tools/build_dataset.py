"""Build the dataset from the live index - thin CLI wrapper around kya.run.

Usage:
    python tools/build_dataset.py            # live index, no page enrichment
    python tools/build_dataset.py --pages    # also enrich ~260 pages (~1/s)
    python tools/build_dataset.py --limit 5  # smoke run

The canonical implementation lives in ``kya.run`` (also installed as the
``kya`` console script); this wrapper exists only so the tool runs from a
source checkout without installing the package.
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from kya.run import main  # noqa: E402

if __name__ == "__main__":
    raise SystemExit(main())

