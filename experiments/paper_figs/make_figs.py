"""Build every figure of the final-report set: ``PYTHONPATH=$PWD python experiments/paper_figs/make_figs.py``.

Outputs (all inside experiments/paper_figs/): figs/*.pdf (vector), figs/*.png (300 dpi),
annotations.json (every number drawn, with its source), MANIFEST.md, figure_book.pdf.
Pass figure ids (e.g. ``F04 F07``) to rebuild a subset while iterating; the manifest and the
book are only written on a full build.
"""

from __future__ import annotations

import sys
import time
from collections.abc import Callable
from typing import Any

from experiments.paper_figs import data, paper_style
from experiments.paper_figs.fig_a1 import f01, f02, f03, f04, f05, f06

BUILDERS: dict[str, Callable[[], list[Any]]] = {
    "F01": f01,
    "F02": f02,
    "F03": f03,
    "F04": f04,
    "F05": f05,
    "F06": f06,
}


def _register_more() -> None:
    try:
        from experiments.paper_figs import fig_a2

        BUILDERS.update({"F07": fig_a2.f07, "F08": fig_a2.f08, "F09": fig_a2.f09, "F10": fig_a2.f10, "F11": fig_a2.f11, "F12": fig_a2.f12})
    except ImportError:
        pass
    try:
        from experiments.paper_figs import fig_b

        BUILDERS.update({"F13": fig_b.f13, "F14": fig_b.f14, "F15": fig_b.f15})
    except ImportError:
        pass


def main(argv: list[str]) -> int:
    paper_style.setup()
    _register_more()
    wanted = argv or list(BUILDERS)
    for fid in wanted:
        t = time.time()
        BUILDERS[fid]()
        print(f"{fid} done in {time.time() - t:.1f}s")
    if not argv:
        data.dump()
        from experiments.paper_figs import book

        book.write_manifest()
        book.write_book()
        print("annotations:", len(data.REG))
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
