"""하루 시뮬레이터 가짜 모드 지문 — 사건·요약의 sha256 을 씨앗별로 낸다.

trace·cloud 를 켜지 않은 기본 실행(가짜 판정기·결정적)이 예전과 바이트까지 같은지 확인한다:

    .venv/bin/python scripts/day_fingerprint.py                      # 이 저장소 코드
    git archive 79d3ffd | tar -x -C /tmp/liot-79d3ffd
    .venv/bin/python scripts/day_fingerprint.py --tree /tmp/liot-79d3ffd   # 그 트리의 코드

두 출력이 같으면 같다. 각 실행은 대상 트리를 ``PYTHONPATH`` 맨 앞에 두고 따로 띄운 파이썬에서
돌며, 실제로 그 트리의 ``liot`` 를 불러왔는지 경로로 확인한다(아니면 실패).
"""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
CASES = ((7, 3), (8, 3), (11, 5))

# 대상 트리에서 도는 코드(그 트리의 liot 를 쓴다). 결과는 JSON 한 줄.
_PROBE = r"""
import asyncio, hashlib, json, sys
from pathlib import Path
import liot
tree = Path(sys.argv[1]).resolve()
if not Path(liot.__file__).resolve().is_relative_to(tree):
    raise SystemExit(f"liot imported from {liot.__file__}, not from {tree}")
from liot.day_sim import DaySim
out = {}
for seed, days in json.loads(sys.argv[2]):
    sim = DaySim(seed=seed, days=days)
    events = []
    asyncio.run(sim.run(lambda stage, data: events.append([stage, data])))
    blob = json.dumps({"events": events, "summary": sim.summary()},
                      ensure_ascii=False, sort_keys=True, default=str)
    out[f"{seed}x{days}"] = [len(events), hashlib.sha256(blob.encode()).hexdigest()]
print(json.dumps(out, sort_keys=True))
"""


def fingerprint(tree: Path) -> dict[str, list[object]]:
    """``tree`` 의 코드로 가짜 모드 실행 지문을 낸다(따로 띄운 파이썬)."""
    tree = tree.resolve()
    env = {**os.environ, "PYTHONPATH": str(tree)}
    proc = subprocess.run(  # noqa: S603 — 고정 인자
        [sys.executable, "-c", _PROBE, str(tree), json.dumps(CASES)],
        cwd=tree, env=env, capture_output=True, text=True, check=False,
    )
    if proc.returncode != 0:
        raise SystemExit(f"fingerprint run failed in {tree}:\n{proc.stderr.strip()}")
    result: dict[str, list[object]] = json.loads(proc.stdout.strip().splitlines()[-1])
    return result


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--tree", type=Path, default=REPO,
                        help="이 폴더의 liot 코드로 돌린다(기본: 이 저장소)")
    args = parser.parse_args(argv)
    print(json.dumps(fingerprint(args.tree), sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
