#!/bin/zsh
# Runs the local judges, two processes at a time (ports 18770/18790), niced. Resumable.
# Usage: run_local_all.sh   (from anywhere; paths are derived from this file)
HERE="${0:A:h}"
ROOT="${HERE:h:h}"
cd "$ROOT" || exit 1
export PYTHONPATH="$ROOT"
PY=/Users/rayul/liot/.venv/bin/python
mkdir -p "$HERE/logs"
lane() {
  port=$1; shift
  for m in "$@"; do
    nice -n 10 "$PY" -m experiments.e11_wording_fields.run_local "$m" --port "$port" >> "$HERE/logs/local_$m.log" 2>&1
  done
}
lane 18770 jevk5-9b-q8 jevk5-4b-q4 jev-9b-q6 &
lane 18790 jevk5-4b-q8 apus-openjev-4b-q4 &
wait
echo done
