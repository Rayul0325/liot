#!/bin/zsh
# E5: run the five readable local models on one bench context (operational | structured), one
# llama-server at a time, niced. Resumable: run_bench.py skips records already written.
# Usage (from the worktree root):  PYTHONPATH=$PWD zsh experiments/e5_models/run_local_context.sh structured
set -u
CTX=${1:?context: operational or structured}
V=/Users/rayul/liot/.venv/bin/python
for m in jevk5-4b-q4 jevk5-4b-q8 apus-openjev-4b-q4 jevk5-9b-q8 jev-9b-q6; do
  echo "=== $m $CTX $(date +%H:%M:%S) load1=$(uptime | sed 's/.*averages: //')"
  nice -n 10 "$V" bench/risk/run_bench.py run --model "$m" --context "$CTX"
  echo "=== $m done rc=$? $(date +%H:%M:%S)"
done
echo "ALL DONE $(date +%H:%M:%S)"
