#!/bin/bash
# One shard of LongMemEval-S retrieval, so a full run takes minutes on several machines instead of 80 on one.
#   SHARD=0 NSHARDS=8 [RERANK=1] [IDS=file.json] bash benchmarks/shard-lme.sh   -> lme-s$SHARD/
# Extra COMMONS_RANK_* settings pass through from the environment.
set -euo pipefail
SHARD=${SHARD:-0}; NSHARDS=${NSHARDS:-1}; OUT=${PREFIX:-lme}-s$SHARD
command -v uv >/dev/null || pip3 install --break-system-packages -q uv
uv venv -q .bench-venv; . .bench-venv/bin/activate
uv pip install -q -e '.[embed]' >/dev/null 2>&1
[ -f longmemeval_s_cleaned.json ] || curl -sfLO https://huggingface.co/datasets/xiaowu0162/longmemeval-cleaned/resolve/main/longmemeval_s_cleaned.json
python - <<PY
import json, os
qs = [q for q in json.load(open("longmemeval_s_cleaned.json")) if not q["question_id"].endswith("_abs")]
ids = set(json.load(open("${IDS:-}"))) if "${IDS:-}" else None
if ids is not None:
    qs = [q for q in qs if q["question_id"] in ids]
qs = [q for i, q in enumerate(qs) if i % $NSHARDS == $SHARD]
json.dump(qs, open("shard.json", "w")); print(len(qs), "questions in shard $SHARD of $NSHARDS")
PY
commons bench longmemeval --data shard.json --embedder fastembed --granularity turn --out "$OUT" ${RERANK:+--rerank} > "$OUT.stdout.json"
echo "SHARD $SHARD done: $(python -c "import json; s=json.load(open('$OUT.stdout.json')); print(s['n'], {k: round(v*100,1) for k,v in s['metrics'].items() if k in ('any@1','any@5','any@10')})")"
