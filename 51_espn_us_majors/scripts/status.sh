#!/bin/bash
# One-shot status for a running (or finished) harvest. Read-only -- safe to run any time, as often
# as you like. Counts come from the shard reports on disk, not from a log, so they are true even if
# a log was rotated or a process was restarted.
cd "$(dirname "$0")/.." || exit 1
PY=/usr/local/anaconda3/bin/python3.11

echo "============= 51_espn_us_majors harvest status ============="
date

echo
echo "-- processes --"
# Filter on the process's own executable (comm), not on the command line: a nohup wrapper shell
# carries the whole harvest command as its args, so an args-only grep matches the wrapper too and
# the listing double-counts.
RUNNING=$(ps -eo pid,etimes,comm,args | awk '$3 ~ /^python3/ && /scripts\/harvest\.py/')
if [ -n "$RUNNING" ]; then
  echo "$RUNNING" | while read -r pid secs comm rest; do
    printf "  RUNNING  pid %-8s %dh%02dm  %s\n" "$pid" "$((secs/3600))" "$(((secs%3600)/60))" \
      "$(echo "$rest" | sed 's|.*scripts/harvest.py|harvest.py|')"
  done
else
  echo "  no harvest running (finished, or not started)"
fi

echo
echo "-- records banked in shards (source of truth) --"
$PY - <<'PY'
import json, glob, collections
tot = g = 0
per = collections.Counter(); pg = collections.Counter(); done = collections.Counter()
for f in sorted(glob.glob("output/shards/*.report.json")):
    d = json.load(open(f))
    per[d["league"]] += d["records"]; pg[d["league"]] += d["games"]; done[d["league"]] += 1
    tot += d["records"]; g += d["games"]
# Games per league in the recap era (calendar 2012-2026), from output/census_walk.json.
UNIVERSE = {"NBA": 20351, "NFL": 4633, "NHL": 20004}
YEARS = 15                                    # 2012..2026 inclusive
print(f"  {'league':6s} {'shards':>8s} {'records':>9s} {'games':>9s} {'yield':>7s} {'% walked':>10s}")
for k in ("NBA", "NFL", "NHL"):
    if not done[k]:
        print(f"  {k:6s} {'-':>8s} {'-':>9s} {'-':>9s} {'-':>7s} {'not started':>10s}")
        continue
    y = pg[k] and per[k] / pg[k]
    print(f"  {k:6s} {done[k]:>5d}/{YEARS} {per[k]:>9,d} {pg[k]:>9,d} {y:>7.3f} "
          f"{pg[k]/UNIVERSE[k]:>9.0%}")
print(f"  {'TOTAL':6s} {sum(done.values()):>5d}/{YEARS*3} {tot:>9,d} {g:>9,d}")
# Remaining work is counted from SHARDS NOT YET BUILT, never from universe-minus-walked: the
# latter reports phantom work left on a finished build (65's status.sh had exactly that bug).
left = [(k, UNIVERSE[k], YEARS - done[k]) for k in UNIVERSE if done[k] < YEARS]
if left:
    est = sum(u * n / YEARS for _, u, n in left)
    print(f"\n  {sum(n for _, _, n in left)} shards left (~{est:,.0f} games); at the measured "
          f"~13 games/s that is ~{est/13/3600:.1f} h if uncached")
else:
    print(f"\n  ✅ all {YEARS*3} shards built.")
PY

echo
echo "-- live fetch rate (last line of each log) --"
for f in output/harvest_nba.out output/harvest_nflnhl.out; do
  [ -f "$f" ] && printf "  %-24s %s\n" "$(basename $f)" \
    "$(grep -E 'req/s|records from' $f | tail -1 | sed 's/^ *//')"
done

echo
echo "-- final report --"
if grep -q "TOTAL:" output/finalize.out 2>/dev/null; then
  echo "  ✅ DONE. Full aggregate + strict validation in: output/finalize.out"
  grep -E '"records"|series_id_unique|distinct_text_share|final_anchor_all_records|series_unhealthy|TOTAL:' \
    output/finalize.out | sed 's/^/     /'
else
  echo "  not finished yet. When the harvests exit, run:"
  echo "     python scripts/aggregate.py && python ../schema/validate.py --strict output/shards/*.jsonl"
fi
echo
echo "  follow live:  tail -f output/harvest_nba.out"
echo "==========================================================="
