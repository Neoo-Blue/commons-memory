#!/bin/bash
# The benchmarks that need no AI model, on a clean Linux machine: LoCoMo and LongMemEval retrieval, search latency and
# footprint at scale, and the tool list an AI client is sent. Writes everything to bench-out/.
#   From the repo root:  bash benchmarks/run-clean.sh
#   In a sandbox:        e2bx run --timeout 7000 --pull bench-out -- 'bash benchmarks/run-clean.sh'
set -euo pipefail
OUT=bench-out
mkdir -p "$OUT"
command -v uv >/dev/null || pip3 install --break-system-packages -q uv
uv venv -q .bench-venv
. .bench-venv/bin/activate
uv pip install -q -e '.[embed,http,tokens]'
{ echo "commons $(commons --version 2>/dev/null || python -c 'import commons_memory as c; print(c.__version__)')"
  echo "cpus $(nproc)"; grep -m1 'model name' /proc/cpuinfo || true; grep MemTotal /proc/meminfo; uname -sr; python --version; } > "$OUT/machine.txt"
cat "$OUT/machine.txt"

[ -f locomo10.json ] || curl -sfLO https://raw.githubusercontent.com/snap-research/locomo/main/data/locomo10.json
[ -f longmemeval_s_cleaned.json ] || curl -sfLO https://huggingface.co/datasets/xiaowu0162/longmemeval-cleaned/resolve/main/longmemeval_s_cleaned.json

echo "== locomo"; commons bench locomo --data locomo10.json --embedder fastembed --out "$OUT" > "$OUT/locomo.stdout.json"
echo "== scale"; commons bench scale --sizes 1000,10000,30000,60000 --embedder fastembed > "$OUT/scale.json"
echo "== tools"; python - > "$OUT/tools.json" <<'PY'
import asyncio, json, os, sys, tempfile
from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client
async def main():
    env = dict(os.environ, COMMONS_DB=os.path.join(tempfile.mkdtemp(), "t.db"), COMMONS_BACKGROUND="off", COMMONS_EMBEDDER="none")
    async with stdio_client(StdioServerParameters(command="commons", args=["serve", "--transport", "stdio"], env=env)) as (r, w):
        async with ClientSession(r, w) as s:
            await s.initialize()
            tools = (await s.list_tools()).tools
            json.dump([{"name": t.name, "description": t.description or "", "input_schema": t.inputSchema} for t in tools], sys.stdout)
asyncio.run(main())
PY
python -c "import json; t=json.load(open('$OUT/tools.json')); print(len(t), 'tools,', len(json.dumps(t)), 'chars')"
echo "== context"; commons bench context-locomo --data locomo10.json --embedder fastembed --k 20 --neighbors 2 --out "$OUT" > "$OUT/context-locomo.stdout.json"
commons bench context-longmemeval --data longmemeval_s_cleaned.json --embedder fastembed --k 20 --neighbors 2 --limit 100 --out "$OUT" > "$OUT/context-longmemeval.stdout.json"
echo "== longmemeval"; commons bench longmemeval --data longmemeval_s_cleaned.json --embedder fastembed --granularity turn --out "$OUT" > "$OUT/longmemeval.stdout.json"
ls -la "$OUT"
