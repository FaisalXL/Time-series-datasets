#!/usr/bin/env python3
"""Render full-scale-builder output for owner inspection, with the extraction shown.

The point of this page is not to show the records -- it is to show the DECISIONS. For each
record it lays the model's sentence selection over the original article, so three things are
directly readable rather than taken on trust:

  * kept      -- the sentence was selected AND survived the character cap
  * cut       -- the sentence was selected but the 2,240-char cap removed it. This is the
                 class that matters: the cap is a blind tail-cut applied AFTER the model
                 chose, so every `cut` sentence is content the model judged relevant and the
                 builder then threw away.
  * unselected-- the model saw it and passed on it. Reading these is how you judge recall.

Class is carried by a text label and a border, never by colour alone.

LOCAL FILE, never published: the prose is third-party CC BY-NC news tagged
`proprietary-review`, so hosting it would be the redistribution the B8 decision is about.
`**/output/*.html` is gitignored for the same reason.

Usage:
    python3 scripts/render_sample.py --records output/sample60.jsonl --limit 60
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
from build_extract import numbered_sentences  # noqa: E402
from fnspid_emit import truncate_at_sentence  # noqa: E402
from render_devset import CSS, JS, spark  # noqa: E402

EXTRA_CSS = """
.sent { display:block; padding:3px 8px 3px 10px; margin:2px 0; border-left:3px solid transparent;
  border-radius:0 4px 4px 0; }
.sent .n { color:var(--muted); font-size:11px; font-variant-numeric:tabular-nums;
  margin-right:6px; }
.s-kept { border-left-color:var(--series-1); background:var(--surface-1); }
.s-cut  { border-left-color:var(--muted); background:var(--surface-1); color:var(--text-secondary);
  text-decoration:line-through; text-decoration-color:var(--muted); }
.s-uns  { color:var(--muted); }
.tag { font-size:10px; letter-spacing:0.06em; text-transform:uppercase; margin-right:6px;
  padding:1px 5px; border-radius:3px; border:1px solid var(--border); color:var(--text-secondary);
  text-decoration:none; display:inline-block; }
.legend { display:flex; gap:14px; flex-wrap:wrap; font-size:12px; color:var(--text-secondary);
  margin:0 0 18px; }
.rel { font-size:13px; color:var(--text-primary); margin:2px 0 8px; }
.rel b { color:var(--text-secondary); font-weight:600; }
.article { max-height:340px; overflow:auto; padding:6px 4px; background:var(--surface-1);
  border:1px solid var(--border); border-radius:6px; margin:8px 0 10px; font-size:13px; }
"""


def load_bodies(cand_path: Path, keys: set) -> Dict[str, List[str]]:
    out: Dict[str, List[str]] = {}
    with open(cand_path, encoding="utf-8") as fh:
        for line in fh:
            # cheap pre-filter before paying for json.loads on 1.17M lines
            c = json.loads(line)
            k = f"{c['t']}|{c['d']}"
            if k in keys:
                out[k] = c["bodies"]
                if len(out) == len(keys):
                    break
    return out


def render_record(r: Dict[str, Any], bodies: List[str], char_cap: int, text_cap: int) -> str:
    m = r["meta"]
    e = m["extraction"]
    sents, art_of, _ = numbered_sentences(bodies, char_cap)
    picks = list(e["sentences"])

    # which selected sentences actually survived the post-extraction character cap
    joined = " ".join(sents[i - 1] for i in picks if 1 <= i <= len(sents))
    kept_text, _tr = truncate_at_sentence(joined, text_cap)
    survived, acc = set(), 0
    for i in picks:
        if not (1 <= i <= len(sents)):
            continue
        acc_end = acc + len(sents[i - 1])
        if acc_end <= len(kept_text):
            survived.add(i)
        acc = acc_end + 1

    pick_set = set(picks)
    rows = []
    for i, s in enumerate(sents, 1):
        if i in survived:
            cls, tag = "s-kept", "kept"
        elif i in pick_set:
            cls, tag = "s-cut", "cut by cap"
        else:
            cls, tag = "s-uns", "unselected"
        art = f"a{art_of[i-1]+1}" if len(bodies) > 1 else ""
        rows.append(f'<span class="sent {cls}"><span class="n">[{i}]{(" " + art) if art else ""}</span>'
                    f'<span class="tag">{tag}</span>{html.escape(s)}</span>')

    ch = {s["unit"]: s["values"] for s in r["timeseries"]}
    close = ch.get("close_price_usd", [])
    cl = [v for v in close if v is not None]
    vol = [v for v in ch.get("volume_shares", []) if v is not None]

    n_cut = len(pick_set) - len(survived)
    chips = [e["role"], m.get("era", ""), f"{m['history_days']}d window",
             f"{len(picks)}/{e['n_sentences_available']} sentences",
             f"{m['text_chars']} chars", f"{m['n_articles_used']}/{m['n_articles_available']} articles"]
    if n_cut:
        chips.append(f"{n_cut} selected sentence(s) cut")
    if e.get("input_capped"):
        chips.append("article truncated before judging")
    if not m.get("symbol_named_in_text"):
        chips.append("symbol filter would have REJECTED")
    if m.get("article_ticker_spread_gt5"):
        chips.append("wire round-up (>5 tickers)")
    if m.get("price_staleness_days", 0) > 4:
        chips.append(f"prices {m['price_staleness_days']}d stale")

    nums = (f"close <b>{cl[0]:,.2f}</b> &rarr; <b>{cl[-1]:,.2f}</b> "
            f"(low {min(cl):,.2f} / high {max(cl):,.2f})<br>"
            f"volume median <b>{sorted(vol)[len(vol)//2]:,}</b>" if cl and vol else "")
    src = m["article_urls"][0] if m.get("article_urls") else ""
    title = (m.get("article_titles") or [""])[0]

    return (
        '<div class="rec">'
        f'<div class="hd"><span class="tk">{html.escape(m["ticker"])}</span>'
        f'<span class="dt">{m["news_date"]}</span>'
        + "".join(f'<span class="chip">{html.escape(str(c))}</span>' for c in chips if c) +
        "</div>"
        f'<div class="rel"><b>relation:</b> {html.escape(str(e.get("relation") or ""))} '
        f'<span style="color:var(--muted)">(confidence {e.get("confidence")})</span></div>'
        + (f'<div class="rel" style="color:var(--text-secondary)"><b>headline:</b> '
           f'{html.escape(title[:150])}</div>' if title else "")
        + f'<div class="article">{"".join(rows)}</div>'
        '<div class="grid2">'
        f'<div class="nums">{nums}<br><span style="color:var(--muted)">'
        f'{html.escape(r["series_id"])} &middot; {r["alignment"]} &middot; {r["license"]}</span>'
        + (f'<br><a href="{html.escape(src)}" rel="noreferrer">source article</a>' if src else "")
        + "</div>"
        f"<div>{spark(r.get('timestamps', []), close)}</div>"
        "</div></div>"
    )


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--records", default="output/sample60.jsonl")
    ap.add_argument("--candidates", default=".cache/candidates.jsonl")
    ap.add_argument("--report", default=".cache/assemble_report_sample.json")
    ap.add_argument("--out", default="output/sample60.html")
    ap.add_argument("--limit", type=int, default=60)
    ap.add_argument("--char-cap", type=int, default=12000)
    ap.add_argument("--text-cap", type=int, default=2240)
    args = ap.parse_args()

    recs = [json.loads(l) for l in open(ROOT / args.records, encoding="utf-8")][:args.limit]
    keys = {f"{r['meta']['ticker']}|{r['meta']['news_date']}" for r in recs}
    print(f"{len(recs)} records; fetching bodies from {args.candidates} ...", flush=True)
    bodies = load_bodies(ROOT / args.candidates, keys)
    print(f"  matched {len(bodies)}/{len(keys)}", flush=True)

    rep = {}
    rp = ROOT / args.report
    if rp.exists():
        rep = json.load(open(rp))

    n_cut_recs = sum(1 for r in recs if r["meta"]["text_truncated"])
    n_would_reject = sum(1 for r in recs if not r["meta"].get("symbol_named_in_text"))
    head = (
        f'<div class="warn"><b>Local inspection page.</b> Third-party CC BY-NC article prose, '
        f'tagged <code>proprietary-review</code> &mdash; do not host or share this file. '
        f'Built by the full-scale pipeline: <code>build_scan</code> &rarr; '
        f'<code>build_extract</code> &rarr; <code>build_assemble</code>.</div>'
        f'<table class="sum"><tr><th>metric</th><th>value</th></tr>'
        f'<tr><td>records shown</td><td>{len(recs)}</td></tr>'
        f'<tr><td>candidate pairs in corpus</td><td>{rep.get("candidates_seen", 0):,}</td></tr>'
        f'<tr><td>roles kept</td><td>{", ".join(rep.get("policy", {}).get("roles_kept", []))}</td></tr>'
        f'<tr><td>records with selected sentences cut by the {args.text_cap}-char cap</td>'
        f'<td>{n_cut_recs} of {len(recs)}</td></tr>'
        f'<tr><td>records the old symbol filter would have rejected</td>'
        f'<td>{n_would_reject} of {len(recs)}</td></tr>'
        f'<tr><td>figure match vs permutation control</td>'
        f'<td>{rep.get("figures_matching_own_series_mean")} vs '
        f'{rep.get("PERMUTATION_CONTROL_mean")}</td></tr>'
        f'</table>'
        f'<div class="legend"><span><b>kept</b> &mdash; model selected it and it is in the record</span>'
        f'<span><b>cut by cap</b> &mdash; model selected it, the char cap removed it</span>'
        f'<span><b>unselected</b> &mdash; model passed on it</span></div>'
    )

    parts = []
    for r in recs:
        k = f"{r['meta']['ticker']}|{r['meta']['news_date']}"
        b = bodies.get(k)
        if not b:
            continue
        parts.append(render_record(r, b, args.char_cap, args.text_cap))

    doc = (f"<meta charset='utf-8'><meta name='viewport' content='width=device-width,initial-scale=1'>"
           f"<title>FNSPID full-scale sample &mdash; inspection</title>"
           f"<style>{CSS}{EXTRA_CSS}</style>"
           f"<button class='toggle'>theme</button><div class='wrap'>"
           f"<h1>FNSPID full-scale sample &mdash; inspection</h1>"
           f"<p class='sub'>Each record shows the model's sentence selection laid over the "
           f"original article.</p>"
           f"{head}<h2>Records</h2>{''.join(parts)}</div><script>{JS}</script>")

    outp = ROOT / args.out
    outp.write_text(doc, encoding="utf-8")
    print(f"wrote {len(parts)} records -> {outp}  ({outp.stat().st_size/1024:.0f} KB)")


if __name__ == "__main__":
    main()
