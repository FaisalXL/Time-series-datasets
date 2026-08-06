#!/usr/bin/env python3
"""Render the FNSPID devset to a local HTML page for owner inspection.

Deliberately written as a LOCAL file, not a published page: the text is third-party
CC BY-NC news article prose tagged `proprietary-review`, so hosting it anywhere would be
redistribution — the exact thing the B8 licence decision is about.

Design notes (per the data-viz method):
  * ONE sparkline per record, close price only. Volume and OHLC are shown as text, never as a
    second y-axis -- a dual-axis sparkline is the single most common charting mistake.
  * One series hue, so there is no categorical palette to CVD-validate. Record class is carried
    by a TEXT chip, never by colour alone.
  * Contrast of every token against both surfaces was computed, not eyeballed (all >= 3:1).
  * Dark mode is stepped from the same ramp for the dark surface, not an automatic flip, and the
    theme toggle wins over the OS setting in both directions.
  * A single delegated hover handler drives the crosshair for all sparklines, so the page stays
    interactive without attaching hundreds of listeners.

Usage:
    python3 scripts/render_devset.py            # -> output/devset.html
"""
from __future__ import annotations

import argparse
import html
import json
from pathlib import Path
from typing import Any, Dict, List

ROOT = Path(__file__).resolve().parents[1]

CSS = """
:root { color-scheme: light;
  --surface-1:#fcfcfb; --surface-2:#f4f3ef; --text-primary:#0b0b0b; --text-secondary:#52514e;
  --muted:#898781; --grid:#e1e0d9; --baseline:#c3c2b7; --border:rgba(11,11,11,0.10);
  --series-1:#2a78d6; }
@media (prefers-color-scheme: dark) { :root:where(:not([data-theme="light"])) {
  color-scheme: dark;
  --surface-1:#1a1a19; --surface-2:#232322; --text-primary:#ffffff; --text-secondary:#c3c2b7;
  --muted:#898781; --grid:#2c2c2a; --baseline:#383835; --border:rgba(255,255,255,0.10);
  --series-1:#3987e5; } }
:root[data-theme="dark"] { color-scheme: dark;
  --surface-1:#1a1a19; --surface-2:#232322; --text-primary:#ffffff; --text-secondary:#c3c2b7;
  --muted:#898781; --grid:#2c2c2a; --baseline:#383835; --border:rgba(255,255,255,0.10);
  --series-1:#3987e5; }
* { box-sizing:border-box; }
body { margin:0; background:var(--surface-1); color:var(--text-primary);
  font:14px/1.55 ui-sans-serif,system-ui,-apple-system,"Segoe UI",sans-serif; }
.wrap { max-width:1100px; margin:0 auto; padding:28px 20px 80px; }
h1 { font-size:22px; margin:0 0 4px; letter-spacing:-0.01em; }
h2 { font-size:15px; margin:34px 0 10px; padding-bottom:6px; border-bottom:1px solid var(--border);
  text-transform:uppercase; letter-spacing:0.06em; color:var(--text-secondary); }
.sub { color:var(--text-secondary); margin:0 0 20px; }
.warn { background:var(--surface-2); border:1px solid var(--border); border-left:3px solid var(--series-1);
  padding:10px 14px; border-radius:6px; margin:0 0 22px; color:var(--text-secondary); font-size:13px; }
table.sum { border-collapse:collapse; width:100%; margin:0 0 8px; font-variant-numeric:tabular-nums; }
table.sum th,table.sum td { text-align:right; padding:5px 8px; border-bottom:1px solid var(--border); }
table.sum th:first-child,table.sum td:first-child { text-align:left; }
table.sum th { color:var(--muted); font-weight:600; font-size:12px; text-transform:uppercase;
  letter-spacing:0.05em; }
.rec { border:1px solid var(--border); border-radius:8px; padding:14px 16px; margin:0 0 12px;
  background:var(--surface-2); }
.hd { display:flex; flex-wrap:wrap; gap:8px; align-items:baseline; margin-bottom:8px; }
.tk { font-weight:700; font-size:16px; letter-spacing:-0.01em; }
.dt { color:var(--text-secondary); font-variant-numeric:tabular-nums; }
.chip { font-size:11px; padding:2px 7px; border-radius:999px; border:1px solid var(--border);
  color:var(--text-secondary); background:var(--surface-1); text-transform:uppercase;
  letter-spacing:0.05em; white-space:nowrap; }
.txt { white-space:pre-wrap; font-size:13px; color:var(--text-primary); margin:6px 0 10px;
  max-height:150px; overflow:auto; padding:8px 10px; background:var(--surface-1);
  border:1px solid var(--border); border-radius:6px; }
.ts { color:var(--series-1); font-weight:700; }
.grid2 { display:grid; grid-template-columns:minmax(0,1fr) 300px; gap:16px; align-items:center; }
@media (max-width:760px){ .grid2 { grid-template-columns:1fr; } }
.nums { font-size:12px; color:var(--text-secondary); font-variant-numeric:tabular-nums; }
.nums b { color:var(--text-primary); font-weight:600; }
.sparkwrap { position:relative; }
svg.spark { display:block; width:100%; height:56px; overflow:visible; touch-action:none; }
svg.spark .ln { fill:none; stroke:var(--series-1); stroke-width:2; stroke-linejoin:round;
  stroke-linecap:round; }
svg.spark .base { stroke:var(--baseline); stroke-width:1; }
svg.spark .dot { fill:var(--series-1); stroke:var(--surface-2); stroke-width:2; }
svg.spark .cx { stroke:var(--muted); stroke-width:1; stroke-dasharray:2 2; opacity:0; }
svg.spark .cxdot { fill:var(--series-1); stroke:var(--surface-2); stroke-width:2; opacity:0; }
.tip { position:absolute; pointer-events:none; opacity:0; transform:translate(-50%,-130%);
  background:var(--text-primary); color:var(--surface-1); font-size:11px; padding:3px 7px;
  border-radius:4px; white-space:nowrap; font-variant-numeric:tabular-nums; z-index:5; }
a { color:var(--series-1); }
.toggle { position:fixed; top:12px; right:14px; z-index:9; font-size:12px; padding:6px 11px;
  border-radius:999px; border:1px solid var(--border); background:var(--surface-2);
  color:var(--text-secondary); cursor:pointer; }
details > summary { cursor:pointer; color:var(--text-secondary); font-size:13px; margin:6px 0; }
"""

JS = """
document.querySelector('.toggle').addEventListener('click', () => {
  const r = document.documentElement;
  const dark = getComputedStyle(r).getPropertyValue('--surface-1').trim() === '#1a1a19';
  r.setAttribute('data-theme', dark ? 'light' : 'dark');
});
// One delegated handler drives the crosshair for every sparkline on the page.
document.addEventListener('pointermove', (ev) => {
  const svg = ev.target.closest ? ev.target.closest('svg.spark') : null;
  if (!svg) return;
  const pts = JSON.parse(svg.dataset.pts), ds = JSON.parse(svg.dataset.ds);
  const r = svg.getBoundingClientRect();
  const W = svg.viewBox.baseVal.width;
  const x = (ev.clientX - r.left) / r.width * W;
  let i = 0, best = 1e9;
  for (let k = 0; k < pts.length; k++) { const d = Math.abs(pts[k][0] - x); if (d < best) { best = d; i = k; } }
  const cx = svg.querySelector('.cx'), cd = svg.querySelector('.cxdot');
  cx.setAttribute('x1', pts[i][0]); cx.setAttribute('x2', pts[i][0]);
  cx.style.opacity = 1;
  cd.setAttribute('cx', pts[i][0]); cd.setAttribute('cy', pts[i][1]); cd.style.opacity = 1;
  const tip = svg.parentNode.querySelector('.tip');
  tip.textContent = ds[i][0] + '  ' + ds[i][1];
  tip.style.left = (pts[i][0] / W * 100) + '%';
  tip.style.top = (pts[i][1] / svg.viewBox.baseVal.height * 100) + '%';
  tip.style.opacity = 1;
});
document.addEventListener('pointerleave', (ev) => {
  const svg = ev.target.closest ? ev.target.closest('svg.spark') : null;
  if (!svg) return;
  svg.querySelector('.cx').style.opacity = 0;
  svg.querySelector('.cxdot').style.opacity = 0;
  svg.parentNode.querySelector('.tip').style.opacity = 0;
}, true);
"""


def spark(dates: List[str], vals: List[float], w: int = 300, h: int = 56) -> str:
    good = [(i, v) for i, v in enumerate(vals) if v is not None]
    if len(good) < 2:
        return '<div class="nums">(no close series)</div>'
    lo = min(v for _, v in good)
    hi = max(v for _, v in good)
    span = (hi - lo) or 1.0
    pad = 4
    n = len(vals)
    pts = []
    ds = []
    for i, v in good:
        x = pad + (w - 2 * pad) * (i / max(1, n - 1))
        y = pad + (h - 2 * pad) * (1 - (v - lo) / span)
        pts.append([round(x, 2), round(y, 2)])
        ds.append([dates[i] if i < len(dates) else "", f"{v:,.2f}"])
    path = "M" + " L".join(f"{x},{y}" for x, y in pts)
    last = pts[-1]
    return (
        f'<div class="sparkwrap">'
        f'<svg class="spark" viewBox="0 0 {w} {h}" preserveAspectRatio="none" role="img" '
        f'aria-label="Close price, {ds[0][0]} to {ds[-1][0]}, low {lo:,.2f} high {hi:,.2f}" '
        f"data-pts='{json.dumps(pts)}' data-ds='{json.dumps(ds)}'>"
        f'<title>close {lo:,.2f}–{hi:,.2f} over {len(good)} trading days</title>'
        f'<line class="base" x1="{pad}" y1="{h-pad}" x2="{w-pad}" y2="{h-pad}"/>'
        f'<path class="ln" d="{path}"/>'
        f'<circle class="dot" cx="{last[0]}" cy="{last[1]}" r="3.5"/>'
        f'<line class="cx" y1="{pad}" y2="{h-pad}" x1="0" x2="0"/>'
        f'<circle class="cxdot" r="4" cx="0" cy="0"/>'
        f'</svg><div class="tip"></div></div>'
    )


def render_record(r: Dict[str, Any]) -> str:
    m = r["meta"]
    body = r["text"].split("\n\n<ts></ts>")[0]
    ch = {s["unit"]: s["values"] for s in r["timeseries"]}
    close = ch.get("close_price_usd", [])
    vol = [v for v in ch.get("volume_shares", []) if v is not None]
    cl = [v for v in close if v is not None]
    chips = [m.get("devset_class", "?"), m.get("era", ""), f"tier {m.get('ticker_tier')}",
             f"{m['history_days']}d window", f"{m['text_chars']} chars"]
    if m.get("text_truncated"):
        chips.append("truncated")
    if m.get("article_ticker_spread", 1) > 1:
        chips.append(f"article on {m['article_ticker_spread']} tickers")
    if m.get("figures_matching_own_series"):
        chips.append(f"{m['figures_matching_own_series']} figure(s) in own series")
    chips.append(f"{m['n_articles_used']}/{m['n_articles_seen']} articles")

    nums = (f"close <b>{cl[0]:,.2f}</b> &rarr; <b>{cl[-1]:,.2f}</b> "
            f"(low {min(cl):,.2f} / high {max(cl):,.2f})<br>"
            f"volume median <b>{sorted(vol)[len(vol)//2]:,}</b>" if cl and vol else "")
    src = m["article_urls"][0] if m.get("article_urls") else ""
    return (
        '<div class="rec">'
        f'<div class="hd"><span class="tk">{html.escape(m["ticker"])}</span>'
        f'<span class="dt">{m["news_date"]}</span>'
        + "".join(f'<span class="chip">{html.escape(str(c))}</span>' for c in chips if c) +
        "</div>"
        f'<div class="txt">{html.escape(body)}<span class="ts">&nbsp;&lt;ts&gt;&lt;/ts&gt;</span></div>'
        '<div class="grid2">'
        f'<div class="nums">{nums}<br><span style="color:var(--muted)">'
        f'{html.escape(r["series_id"])} &middot; {r["alignment"]} &middot; {r["license"]}</span>'
        + (f'<br><a href="{html.escape(src)}" rel="noreferrer">source article</a>' if src else "")
        + "</div>"
        f"<div>{spark(r.get('timestamps', []), close)}</div>"
        "</div></div>"
    )


def summary_table(rep: Dict[str, Any]) -> str:
    rows = []
    for key, label in (("kept", "kept (symbol-named)"), ("rejects", "symbol-filter rejects"),
                       ("roundups", "wire round-ups (&gt;5 tickers)")):
        s = rep.get(key) or {}
        if not s:
            continue
        rows.append(
            f"<tr><td>{label}</td><td>{s['n']}</td><td>{s['distinct_tickers']}</td>"
            f"<td>{s['text_chars']['median']:,.0f}</td><td>{s['truncated_pct']}%</td>"
            f"<td>{s['history_days']['median']:,.0f}</td>"
            f"<td>{s['figures_matching_own_series_pct_records_ge1']}%</td>"
            f"<td>{s['PERMUTATION_CONTROL_pct_records_ge1']}%</td></tr>")
    return (
        '<table class="sum"><thead><tr><th>class</th><th>n</th><th>tickers</th>'
        "<th>median chars</th><th>truncated</th><th>median window</th>"
        "<th>&ge;1 figure in own series</th><th>permutation control</th></tr></thead>"
        f"<tbody>{''.join(rows)}</tbody></table>")


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--dir", default="output")
    ap.add_argument("--out", default="output/devset.html")
    args = ap.parse_args()
    d = ROOT / args.dir
    rep = json.load(open(d / "devset_report.json"))

    sections = []
    for fn, title, note in (
        ("devset.jsonl", "Kept records", "What the corpus would actually contain."),
        ("devset_rejects.jsonl", "Rejected by require_symbol_in_text",
         "These would be DROPPED. Read a few: is the filter removing noise, or removing data?"),
        ("devset_roundups.jsonl", "Wire round-ups (article tagged to &gt;5 tickers)",
         "2.2% of articles but 26.3% of all volume. This is the class the LLM judge targets."),
    ):
        p = d / fn
        if not p.exists():
            continue
        recs = [json.loads(l) for l in open(p) if l.strip()]
        sections.append(f"<h2>{title} &middot; {len(recs)}</h2>"
                        f'<p class="sub">{note}</p>'
                        + "".join(render_record(r) for r in recs))

    u = rep["universe"]
    st = rep["settings"]
    head = (
        "<h1>FNSPID devset &mdash; inspection</h1>"
        f'<p class="sub">Built by the same code path as the full run '
        f"(<code>build_devset.py</code> &rarr; <code>fnspid_emit.make_record</code> &rarr; "
        f"<code>schema/emit.py</code>). All records pass <code>validate.py --strict</code>.</p>"
        '<div class="warn"><b>Local file, deliberately not published.</b> This text is '
        "third-party CC BY-NC news prose tagged <code>proprietary-review</code>; hosting it "
        "would be redistribution, which is exactly what the B8 licence decision governs.</div>"
        f'<p class="sub">Universe: <b>{u["candidate_pairs"]:,}</b> candidate (ticker, date) pairs '
        f'from {u["news_rows_scanned"]:,} news rows over {u["tickers_with_prices"]:,} tickers '
        f'with price history. <b>{u["pairs_symbol_named"]:,}</b> ({u["pairs_symbol_named_pct"]}%) '
        f"name their ticker in the text. Settings: window "
        f'{st["history_days"]}/{st["min_history_days"]} trading days, text cap '
        f'{st["text_max_chars"]:,} chars, &le;{st["max_articles_per_record"]} articles/record.</p>'
        + summary_table(rep) +
        '<p class="sub" style="font-size:12.5px">The permutation control re-runs the '
        "figure-match against a <i>different</i> record's series. Read the two right-hand "
        "columns as a pair: the gap is the signal, the control is the coincidence floor.</p>"
    )

    doc = (f"<!doctype html><html lang=en><head><meta charset=utf-8>"
           f'<meta name=viewport content="width=device-width,initial-scale=1">'
           f"<title>FNSPID devset &mdash; inspection</title><style>{CSS}</style></head><body>"
           f'<button class="toggle">light / dark</button><div class="wrap">'
           f"{head}{''.join(sections)}</div><script>{JS}</script></body></html>")

    outp = ROOT / args.out
    outp.write_text(doc, encoding="utf-8")
    print(f"wrote {outp}  ({len(doc)/1024:.0f} KB)")


if __name__ == "__main__":
    main()
