#!/bin/bash
# One-shot status for a running (or finished) harvest. Read-only -- safe to run any time,
# as often as you like. Counts come from the shard reports on disk, not from a log, so they
# are true even if a log was rotated or a process was restarted.
cd "$(dirname "$0")/.." || exit 1
PY=/usr/local/anaconda3/bin/python3.11

echo "=============== 65_espn_college harvest status ==============="
date

echo
echo "-- processes --"
# Filter on the process's own executable (comm), not on the command line: the nohup wrapper
# shells carry the whole harvest command inside a heredoc, so any args-based grep matches them
# too and the listing becomes unreadable.
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
tot=g=0; per=collections.Counter(); pg=collections.Counter(); done=collections.Counter()
for f in sorted(glob.glob("output/shards/*.report.json")):
    d=json.load(open(f))
    per[d["league"]]+=d["records"]; pg[d["league"]]+=d["games"]; done[d["league"]]+=1
    tot+=d["records"]; g+=d["games"]
UNIVERSE={"CFB":13261,"FCS":11889,"MCB":82763,"WCB":77540}
print(f"  {'tier':5s} {'shards':>7s} {'records':>9s} {'games':>9s} {'yield':>7s} {'% of universe walked':>22s}")
for k in ("CFB","FCS","MCB","WCB"):
    if not done[k]:
        print(f"  {k:5s} {'-':>7s} {'-':>9s} {'-':>9s} {'-':>7s} {'not started':>22s}"); continue
    y=pg[k] and per[k]/pg[k]
    print(f"  {k:5s} {done[k]:>4d}/14 {per[k]:>9,d} {pg[k]:>9,d} {y:>7.3f} {pg[k]/UNIVERSE[k]:>21.0%}")
print(f"  {'TOTAL':5s} {sum(done.values()):>4d}/56 {tot:>9,d} {g:>9,d}")
rem=sum(UNIVERSE.values())-g
if rem>0:
    print(f"\n  ~{rem:,} games left; at the measured ~13 games/s that is ~{rem/13/3600:.1f} h")
PY

echo
echo "-- live fetch rate (last line of each log) --"
for f in output/harvest_rest.out output/harvest_wcb.out; do
  [ -f "$f" ] && printf "  %-24s %s\n" "$(basename $f)" "$(grep 'req/s' $f | tail -1 | sed 's/^ *//')"
done

echo
echo "-- final report --"
if grep -q "TOTAL:" output/finalize.out 2>/dev/null; then
  echo "  ✅ DONE. Full aggregate + strict validation in: output/finalize.out"
  grep -E '"records"|series_id_unique|distinct_text_share|final_anchor_all_records|series_unhealthy|TOTAL:' \
    output/finalize.out | sed 's/^/     /'
else
  echo "  not finished yet. When the harvests exit, the aggregate + strict validation run"
  echo "  automatically and land in output/finalize.out"
fi
echo
echo "  follow live:  tail -f output/harvest_rest.out"
echo "=============================================================="
