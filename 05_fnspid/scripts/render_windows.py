#!/usr/bin/env python3
"""Side-by-side inspection of the two window lengths, showing the ranking and the budget.

Renders both candidate record shapes into one page so 90 and 30 trading days can be compared
on the same screen rather than from two reports.

Per record it shows three sentence classes, which together make the v2 mechanism auditable:

  * used        -- the model ranked it high enough to fit the ~500-token budget
  * over budget -- the model selected it, but it ranked below the cut. This is the honest
                   replacement for v1's blind tail-cut: what gets dropped is what the model
                   itself ranked last, not whatever happened to fall past 2,240 chars.
  * unselected  -- the model passed on it. Reading these is how recall gets judged.

The sparkline marks the days news was published, so the central claim of the redesign -- that
the text now sits INSIDE the series rather than after it -- is visible rather than asserted.

LOCAL FILE, never published: third-party CC BY-NC prose tagged `proprietary-review`.

Usage:
    python3 scripts/render_windows.py --limit 30
"""
from __future__ import annotations

import argparse
import html
import json
import sys
from pathlib import Path
from typing import Any, Dict, List

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(Path(__file__).resolve().parent))
from build_extract import numbered_window  # noqa: E402
from render_devset import CSS, JS  # noqa: E402
from render_sample import EXTRA_CSS  # noqa: E402

MORE_CSS = """
.s-over { border-left-color:var(--muted); background:var(--surface-1);
  color:var(--text-secondary); }
.s-over .tag { border-style:dashed; }
svg.spark .news { stroke:var(--text-secondary); stroke-width:1; opacity:0.45; }
.cmp { display:grid; grid-template-columns:1fr 1fr; gap:14px; margin:0 0 26px; }
@media (max-width:820px){ .cmp { grid-template-columns:1fr; } }
.card { border:1px solid var(--border); border-radius:8px; padding:12px 14px;
  background:var(--surface-2); }
.card h3 { margin:0 0 8px; font-size:14px; }
.big { font-size:24px; font-weight:700; letter-spacing:-0.02em; }
"""


def spark_news(dates: List[str], vals: List[float], news: List[str],
               w: int = 320, h: int = 62) -> str:
    good = [(i, v) for i, v in enumerate(vals) if v is not None]
    if len(good) < 2:
        return '<div class="nums">(no close series)</div>'
    lo = min(v for _, v in good)
    hi = max(v for _, v in good)
    span = (hi - lo) or 1.0
    pad, n = 4, len(vals)
    pts, ds = [], []
    for i, v in good:
        x = pad + (w - 2 * pad) * (i / max(1, n - 1))
        y = pad + (h - 2 * pad) * (1 - (v - lo) / span)
        pts.append([round(x, 2), round(y, 2)])
        ds.append([dates[i] if i < len(dates) else "", f"{v:,.2f}"])
    path = "M" + " L".join(f"{x},{y}" for x, y in pts)
    newsset = set(news or [])
    ticks = "".join(
        f'<line class="news" x1="{pad + (w-2*pad)*(i/max(1,n-1)):.2f}" '
        f'x2="{pad + (w-2*pad)*(i/max(1,n-1)):.2f}" y1="{h-pad}" y2="{h-pad-7}"/>'
        for i, d in enumerate(dates) if d in newsset)
    return (
        f'<div class="sparkwrap">'
        f'<svg class="spark" viewBox="0 0 {w} {h}" preserveAspectRatio="none" role="img" '
        f'aria-label="Close price {ds[0][0]} to {ds[-1][0]}, low {lo:,.2f} high {hi:,.2f}; '
        f'ticks mark days news was published" '
        f"data-pts='{json.dumps(pts)}' data-ds='{json.dumps(ds)}'>"
        f'<line class="base" x1="{pad}" y1="{h-pad}" x2="{w-pad}" y2="{h-pad}"/>'
        f'{ticks}<path class="ln" d="{path}"/>'
        f'<circle class="dot" cx="{pts[-1][0]}" cy="{pts[-1][1]}" r="3.5"/>'
        f'<line class="cx" y1="{pad}" y2="{h-pad}" x1="0" x2="0"/>'
        f'<circle class="cxdot" r="4" cx="0" cy="0"/>'
        f'</svg><div class="tip"></div></div>'
    )


def load_windows(path: Path, keys: set) -> Dict[str, dict]:
    out: Dict[str, dict] = {}
    with open(path, encoding="utf-8") as fh:
        for line in fh:
            w = json.loads(line)
            k = f"{w['t']}|{w['w_start']}"
            if k in keys:
                out[k] = w
                if len(out) == len(keys):
                    break
    return out


def render_record(r: Dict[str, Any], w: dict, char_cap: int) -> str:
    m = r["meta"]
    e = m["extraction"]
    sents, art_of, _b, _c = numbered_window(w["bodies"], w.get("days", []), char_cap)
    used = set(e["sentences_used"])
    ranked = list(e["ranked_sentences"])
    rank_pos = {s: i + 1 for i, s in enumerate(ranked)}

    rows = []
    for i, s in enumerate(sents, 1):
        if i in used:
            cls, tag = "s-kept", f"used &middot; rank {rank_pos.get(i, '?')}"
        elif i in rank_pos:
            cls, tag = "s-over", f"over budget &middot; rank {rank_pos[i]}"
        else:
            cls, tag = "s-uns", "unselected"
        day = w["days"][art_of[i - 1]] if art_of[i - 1] < len(w.get("days", [])) else ""
        rows.append(f'<span class="sent {cls}"><span class="n">[{i}] {day}</span>'
                    f'<span class="tag">{tag}</span>{html.escape(s)}</span>')

    ch = {s["unit"]: s["values"] for s in r["timeseries"]}
    close = ch.get("close_price_usd", [])
    cl = [v for v in close if v is not None]
    n_over = len(ranked) - len(used)
    chips = [e["role"], m.get("era", ""), f"{m['window_trading_days']} trading days",
             f"{m['news_days_in_window']} news days", f"{m['n_articles_used']} articles used",
             f"{m['text_chars']} chars"]
    if n_over:
        chips.append(f"{n_over} ranked but over budget")
    if m.get("article_spread_gt5"):
        chips.append(f"{m['article_spread_gt5']} wire round-up article(s)")

    nums = (f"close <b>{cl[0]:,.2f}</b> &rarr; <b>{cl[-1]:,.2f}</b> "
            f"({100*(cl[-1]-cl[0])/cl[0]:+.1f}% over the window)" if cl else "")
    src = m["article_urls"][0] if m.get("article_urls") else ""
    return (
        '<div class="rec">'
        f'<div class="hd"><span class="tk">{html.escape(m["ticker"])}</span>'
        f'<span class="dt">{m["window_start"]} &rarr; {m["window_end"]}</span>'
        + "".join(f'<span class="chip">{html.escape(str(c))}</span>' for c in chips if c) +
        "</div>"
        f'<div class="rel"><b>relation:</b> {html.escape(str(e.get("relation") or ""))} '
        f'<span style="color:var(--muted)">(confidence {e.get("confidence")})</span></div>'
        f'<div class="article">{"".join(rows)}</div>'
        '<div class="grid2">'
        f'<div class="nums">{nums}<br><span style="color:var(--muted)">'
        f'{html.escape(r["series_id"])} &middot; {r["alignment"]} &middot; '
        f'text_quality {r["text_quality"]} &middot; lookahead_safe false</span>'
        + (f'<br><a href="{html.escape(src)}" rel="noreferrer">source article</a>' if src else "")
        + "</div>"
        f'<div>{spark_news(r.get("timestamps", []), close, m.get("news_dates") or [])}</div>'
        "</div></div>"
    )


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--limit", type=int, default=30)
    ap.add_argument("--char-cap", type=int, default=24000)
    ap.add_argument("--out", default="output/window_compare.html")
    args = ap.parse_args()

    sections, cards = [], []
    for W in (90, 30):
        recs = [json.loads(l) for l in open(ROOT / f"output/window{W}.jsonl", encoding="utf-8")]
        rep = json.load(open(ROOT / f".cache/assemble_w{W}.json"))
        wins = load_windows(ROOT / f".cache/windows_{W}.jsonl",
                            {f"{r['meta']['ticker']}|{r['meta']['window_start']}" for r in recs})
        fm = rep.get("figures_matching_own_series_mean") or 0
        pc = rep.get("PERMUTATION_CONTROL_mean") or 1
        cards.append(
            f'<div class="card"><h3>{W} trading days</h3>'
            f'<div class="big">{fm/pc:.2f}&times;</div>'
            f'<div class="nums">figure match over permutation control<br>'
            f'({fm} vs {pc})</div>'
            f'<table class="sum" style="margin-top:10px">'
            f'<tr><td>records in sample</td><td>{rep.get("records_kept")}</td></tr>'
            f'<tr><td>median text</td><td>{rep.get("text_chars",{}).get("median")} chars</td></tr>'
            f'<tr><td>model ranking used</td><td>{100*(rep.get("ranked_share_used") or 0):.0f}%</td></tr>'
            f'<tr><td>kept roles</td><td>{", ".join(rep.get("policy",{}).get("roles_kept",[]))}</td></tr>'
            f'</table></div>')
        body = []
        for r in recs[:args.limit]:
            k = f"{r['meta']['ticker']}|{r['meta']['window_start']}"
            if k in wins:
                body.append(render_record(r, wins[k], args.char_cap))
        sections.append(f'<h2>{W} trading days &mdash; {len(body)} records</h2>' + "".join(body))

    doc = (f"<meta charset='utf-8'><meta name='viewport' content='width=device-width,initial-scale=1'>"
           f"<title>FNSPID window records &mdash; 90d vs 30d</title>"
           f"<style>{CSS}{EXTRA_CSS}{MORE_CSS}</style>"
           f"<button class='toggle'>theme</button><div class='wrap'>"
           f"<h1>FNSPID window records &mdash; 90d vs 30d</h1>"
           f"<p class='sub'>The series now SPANS the news instead of preceding it. Ticks under "
           f"each sparkline mark the days news was published.</p>"
           f'<div class="warn"><b>Local inspection page.</b> Third-party CC BY-NC prose tagged '
           f'<code>proprietary-review</code> &mdash; do not host or share. '
           f'<code>lookahead_safe: false</code> on every record: text and series overlap in '
           f'time by design.</div>'
           f'<div class="cmp">{"".join(cards)}</div>'
           f'<div class="legend"><span><b>used</b> &mdash; ranked high enough to fit the budget</span>'
           f'<span><b>over budget</b> &mdash; model selected it, ranked below the cut</span>'
           f'<span><b>unselected</b> &mdash; model passed on it</span></div>'
           f"{''.join(sections)}</div><script>{JS}</script>")

    outp = ROOT / args.out
    outp.write_text(doc, encoding="utf-8")
    print(f"wrote -> {outp}  ({outp.stat().st_size/1024:.0f} KB)")


if __name__ == "__main__":
    main()
