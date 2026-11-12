"""Estimate the cloud spend of this unit from the derived answer rows (tokens x config/models.yaml prices).

    PYTHONPATH=$PWD .venv/bin/python -m experiments.e11_wording_fields.cost
"""

from __future__ import annotations

import collections

import yaml

from experiments.common import io
from experiments.e11_wording_fields.prep_inputs import HERE

PRICE_MODEL = {"jev-api": "jev-latest", "haiku": "claude-haiku-5-5", "gemini": "gemini-3.8-flash"}


def main() -> None:
    prices = yaml.safe_load((HERE.parents[1] / "config" / "models.yaml").read_text(encoding="utf-8"))["prices"]
    tot: dict[str, float] = collections.defaultdict(float)
    n: dict[str, int] = collections.defaultdict(int)
    err: dict[str, int] = collections.defaultdict(int)
    for r in io.read_jsonl(HERE / "raw" / "answers_cloud.jsonl"):
        p = prices[PRICE_MODEL[r["judge"]]]
        tot[r["judge"]] += ((r.get("input_tokens") or 0) * p["input"] + (r.get("output_tokens") or 0) * p["output"]) / 1e6
        n[r["judge"]] += 1
        err[r["judge"]] += int(bool(r.get("error")))
    for j in tot:
        print(f"{j}: rows {n[j]} errors {err[j]} est ${tot[j]:.4f}")
    print(f"total est ${sum(tot.values()):.4f} (cap 0.60)")


if __name__ == "__main__":
    main()
