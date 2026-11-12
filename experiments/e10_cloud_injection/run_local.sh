#!/bin/zsh
# E10: the five local models on the injected / control items, one llama-server at a time, niced.
# Resumable (rows already in raw/answers_local.jsonl are skipped).
# Usage (from the worktree root):  PYTHONPATH=$PWD zsh experiments/e10_cloud_injection/run_local.sh
set -u
V=/Users/rayul/liot/.venv/bin/python
for m in jevk5-4b-q4 jevk5-4b-q8 apus-openjev-4b-q4 jevk5-9b-q8 jev-9b-q6; do
  echo "=== $m $(date +%H:%M:%S) load1=$(uptime | sed 's/.*averages: //')"
  nice -n 10 "$V" -m experiments.e10_cloud_injection.local --model "$m"
  echo "=== $m done rc=$? $(date +%H:%M:%S)"
done
echo "ALL DONE $(date +%H:%M:%S)"
