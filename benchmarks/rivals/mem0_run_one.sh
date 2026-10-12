#!/bin/sh
# One LoCoMo conversation per process: mem0 locks its migrations store per Memory() instance.
D="$(cd "$(dirname "$0")" && pwd)"; cd "$D"
mkdir -p "m0_$1" && MEM0_DIR="$D/m0_$1" sesame run anthropic -- "$D/venv/bin/python" mem0_locomo.py "$1" "mem0_c$1.jsonl" > "mem0_c$1.log" 2>&1
