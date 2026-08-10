#!/usr/bin/env bash
# Full-scale window build, end to end. Re-runnable: stage 2 resumes from its own output, and
# stages 2.5 and 3 are cheap enough to simply redo.
#
#   ./scripts/run_window_build.sh 30 [concurrency]
#
# Stage 1 (build_scan) and the window forming (build_windows) are NOT here -- they are run
# once and their output is reused across window lengths.
set -euo pipefail

W="${1:?usage: run_window_build.sh <window_trading_days> [concurrency]}"
CONC="${2:-64}"
PY=/usr/local/anaconda3/bin/python3.11
cd "$(dirname "$0")/.."

: "${VLLM_KEY:?export VLLM_KEY first (see llm-api.txt; never commit it)}"

WIN=".cache/windows_${W}.jsonl"
VERD=".cache/verdicts_full${W}.jsonl"
ADJ=".cache/adjud_full${W}.jsonl"
OUT="output/fnspid_cpt_window${W}.jsonl"

[ -f "$WIN" ] || { echo "missing $WIN -- run build_windows.py --window $W first"; exit 1; }
echo "=== window ${W}d: $(wc -l < "$WIN") windows, concurrency ${CONC} ==="

echo "--- stage 2: extraction + summary (resumable) ---"
$PY scripts/build_extract.py --candidates "$WIN" --out "$VERD" \
    --report "output/extract_report_window${W}.json" \
    --prompt v3 --summary-words 360 --concurrency "$CONC" \
    --char-cap 24000 --max-tokens 1600

echo "--- stage 2.5: adjudicate figures the value matcher could not place ---"
$PY scripts/build_adjudicate.py --windows "$WIN" --verdicts "$VERD" --out "$ADJ" \
    --concurrency "$CONC"

echo "--- stage 3: assemble (free to re-run with different policy) ---"
$PY scripts/build_window_records.py --windows "$WIN" --verdicts "$VERD" \
    --adjudications "$ADJ" --out "$OUT" \
    --report "output/run_report_window${W}.json" --text-from summary

echo "--- gate: schema v1 strict ---"
$PY ../schema/validate.py "$OUT" --strict

echo "=== done: $OUT ==="
