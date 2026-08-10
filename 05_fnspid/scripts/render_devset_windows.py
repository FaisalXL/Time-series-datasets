#!/usr/bin/env python3
"""Render the window devset as an audit page: is the LLM's work actually correct?

Built for a reviewer who has not seen the pipeline. Every record puts the four LLM outputs --
role, relation, summary, ranked extraction -- next to the source articles they came from, so
each can be checked rather than trusted:

  * the SUMMARY is the record text. Every number in it is machine-checked against the source
    and marked inline: a green-bordered token was found in the articles, a flagged one was
    not. A reviewer reading a flagged number is looking at the exact failure mode that
    matters -- an invented figure in a corpus whose value is real figures.
  * the SOURCE ARTICLES are one click away, full text, so the summary can be verified against
    what the model was actually shown.
  * the EXTRACTION is shown too, because it is the fallback text if the schema conversation
    goes against `generated` -- so the reviewer is judging both candidate corpora at once.
  * records where the fidelity gate REJECTED the summary are included on purpose and flagged,
    so the guard can be judged and not just its output.

The sparkline marks the days news was published, which is the whole point of the window
redesign: the text now sits INSIDE the series rather than after it.

LOCAL FILE, never published: third-party CC BY-NC prose tagged `proprietary-review`.

Usage:
    python3 scripts/render_devset_windows.py
"""
from __future__ import annotations

import argparse
import collections
import html
import json
import re
import sys
from pathlib import Path
from typing import Any, Dict, List

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(Path(__file__).resolve().parent))
from build_window_records import _NUMTOK, numeric_fidelity  # noqa: E402
from render_devset import CSS, JS  # noqa: E402
from render_sample import EXTRA_CSS  # noqa: E402
from render_windows import MORE_CSS, spark_news  # noqa: E402

AUDIT_CSS = """
.summary { font-size:14px; line-height:1.65; background:var(--surface-1);
  border:1px solid var(--border); border-left:3px solid var(--series-1);
  border-radius:0 6px 6px 0; padding:12px 14px; margin:8px 0 10px; }
.num-ok { border-bottom:2px solid var(--series-1); }
.num-bad { background:var(--flag); border-bottom:2px solid var(--text-primary);
  font-weight:700; padding:0 3px; border-radius:3px; }
.src { font-size:12.5px; color:var(--text-secondary); }
.src h4 { margin:12px 0 4px; font-size:12px; color:var(--text-primary);
  text-transform:uppercase; letter-spacing:0.05em; }
.alt { font-size:12.5px; color:var(--text-secondary); background:var(--surface-1);
  border:1px solid var(--border); border-radius:6px; padding:10px 12px; margin:6px 0; }
.bad-banner { background:var(--surface-1); border:1px solid var(--border);
  border-left:3px solid var(--text-primary); padding:8px 12px; border-radius:0 6px 6px 0;
  font-size:12.5px; margin:6px 0; }
:root { --flag:#f7e6a8; }
:root:not([data-theme="light"]) { }
@media (prefers-color-scheme: dark) { :root:where(:not([data-theme="light"])) { --flag:#5c4a12; } }
:root[data-theme="dark"] { --flag:#5c4a12; }
"""


def mark_numbers(text: str, bad_tokens: List[str]) -> str:
    """Underline every number; flag the ones the GATE rejected.

    The flagged set comes from `numeric_fidelity` itself rather than being recomputed here,
    so the page can never highlight a number the gate accepted (or miss one it rejected).
    """
    remaining = collections.Counter(bad_tokens)
    out, last = [], 0
    for m in _NUMTOK.finditer(text):
        out.append(html.escape(text[last:m.start()]))
        tok = m.group(0)
        if remaining.get(tok, 0) > 0:
            remaining[tok] -= 1
            cls = "num-bad"
        else:
            cls = "num-ok"
        out.append(f'<span class="{cls}">{html.escape(tok)}</span>')
        last = m.end()
    out.append(html.escape(text[last:]))
    return "".join(out)


def render_record(r: Dict[str, Any], w: dict) -> str:
    m, e = r["meta"], r["meta"]["extraction"]
    is_sum = r["text_quality"] == "generated"
    body = r["text"].split("\n\n<ts></ts>")[0]
    dates = list(w.get("days", [])) + [w["w_start"], w["w_end"]]
    fid = m.get("summary_numeric_fidelity") or {}
    _n, _b, bad_tokens = numeric_fidelity(body, " ".join(w["bodies"]), dates)

    ch = {s["unit"]: s["values"] for s in r["timeseries"]}
    close = ch.get("close_price_usd", [])
    cl = [v for v in close if v is not None]

    chips = [f"{m['devset_window_len']}d window", e["role"], m.get("era", ""),
             f"{m['n_articles_used']} articles", f"{m['news_days_in_window']} news days",
             f"{m['text_chars']} chars", f"text_quality {r['text_quality']}"]
    if not is_sum:
        chips.append("GATE REJECTED SUMMARY")

    banner = ""
    if not is_sum:
        ex = ", ".join((fid.get("tokens") or [])[:5])
        banner = (f'<div class="bad-banner"><b>The numeric-fidelity gate rejected this '
                  f'summary</b> ({fid.get("unsupported")} of {fid.get("numbers")} numbers not '
                  f'found in the source{": " + html.escape(ex) if ex else ""}). The record '
                  f'fell back to verbatim extraction, so nothing invented ships. The rejected '
                  f'summary is shown below for review.</div>')

    alt_label = "Extraction (fallback text, verbatim)" if is_sum else "Rejected summary (NOT used)"
    alt_text = m.get("extractive_text") if is_sum else m.get("summary_text")

    srcs = []
    for i, b in enumerate(w["bodies"]):
        day = w["days"][i] if i < len(w.get("days", [])) else ""
        ttl = w["titles"][i] if i < len(w.get("titles", [])) else ""
        srcs.append(f'<h4>article {i+1} &mdash; {html.escape(day)}</h4>'
                    f'<div><b>{html.escape(ttl[:140])}</b></div><div>{html.escape(b)}</div>')

    nums = (f"close <b>{cl[0]:,.2f}</b> &rarr; <b>{cl[-1]:,.2f}</b> "
            f"({100*(cl[-1]-cl[0])/cl[0]:+.1f}% across the window)" if cl else "")
    return (
        '<div class="rec">'
        f'<div class="hd"><span class="tk">{html.escape(m["ticker"])}</span>'
        f'<span class="dt">{m["window_start"]} &rarr; {m["window_end"]}</span>'
        + "".join(f'<span class="chip">{html.escape(str(c))}</span>' for c in chips if c) +
        "</div>"
        f'<div class="rel"><b>relation:</b> {html.escape(str(e.get("relation") or ""))} '
        f'<span style="color:var(--muted)">(confidence {e.get("confidence")})</span></div>'
        + banner +
        f'<div class="summary">{mark_numbers(body, bad_tokens)}</div>'
        + (f'<details><summary>{alt_label}</summary><div class="alt">'
           f'{html.escape(alt_text or "(none)")}</div></details>' if alt_text else "")
        + f'<details><summary>Source articles ({len(w["bodies"])}) &mdash; verify the summary '
          f'against these</summary><div class="src">{"".join(srcs)}</div></details>'
        '<div class="grid2">'
        f'<div class="nums">{nums}<br><span style="color:var(--muted)">'
        f'{html.escape(r["series_id"])} &middot; {r["alignment"]} &middot; lookahead_safe false'
        f'</span></div>'
        f'<div>{spark_news(r.get("timestamps", []), close, m.get("news_dates") or [])}</div>'
        "</div></div>"
    )


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--records", default="output/devset.jsonl")
    ap.add_argument("--report", default="output/devset_report.json")
    ap.add_argument("--out", default="output/devset.html")
    args = ap.parse_args()

    recs = [json.loads(l) for l in open(ROOT / args.records, encoding="utf-8")]
    rep = json.load(open(ROOT / args.report))

    wins: Dict[str, dict] = {}
    for W in (90, 30):
        keys = {f"{r['meta']['ticker']}|{r['meta']['window_start']}" for r in recs
                if r["meta"]["devset_window_len"] == W}
        with open(ROOT / f".cache/windows_{W}.jsonl", encoding="utf-8") as fh:
            for line in fh:
                x = json.loads(line)
                k = f"{x['t']}|{x['w_start']}"
                if k in keys:
                    wins[f"{W}|{k}"] = x
                    if len([1 for kk in wins if kk.startswith(f"{W}|")]) == len(keys):
                        break

    sections = []
    for W in (90, 30):
        part = [r for r in recs if r["meta"]["devset_window_len"] == W]
        body = []
        for r in part:
            k = f"{W}|{r['meta']['ticker']}|{r['meta']['window_start']}"
            if k in wins:
                body.append(render_record(r, wins[k]))
        sections.append(f'<h2>{W} trading days &mdash; {len(body)} records</h2>' + "".join(body))

    n_bad = rep["numbers_unsupported"]
    n_tot = rep["numbers_checked"]
    head = (
        f'<div class="warn"><b>Local audit page &mdash; do not host or share.</b> '
        f'Third-party CC BY-NC news prose tagged <code>proprietary-review</code>. '
        f'Every record: text and series overlap in time by design '
        f'(<code>lookahead_safe: false</code>).</div>'
        f'<table class="sum"><tr><th>what to check</th><th></th></tr>'
        f'<tr><td>records</td><td>{rep["total"]} &mdash; 30 at 90 trading days, 30 at 30</td></tr>'
        f'<tr><td>record text</td><td>LLM summary; falls back to verbatim extraction if a '
        f'number fails the check</td></tr>'
        f'<tr><td>numbers machine-checked against source</td><td>{n_tot:,}</td></tr>'
        f'<tr><td>numbers not found in source</td><td>{n_bad} '
        f'({100*n_bad/max(1,n_tot):.2f}%)</td></tr>'
        f'<tr><td>records where the gate forced extraction</td>'
        f'<td>{rep["records_where_gate_forced_extraction"]} (deliberately over-sampled here)</td></tr>'
        f'</table>'
        f'<div class="legend">'
        f'<span><b>underlined number</b> &mdash; found in the source articles</span>'
        f'<span><b>highlighted number</b> &mdash; NOT found; this is what to scrutinise</span>'
        f'<span>expand <b>Source articles</b> to verify any summary</span></div>')

    doc = (f"<meta charset='utf-8'><meta name='viewport' content='width=device-width,initial-scale=1'>"
           f"<title>FNSPID window devset &mdash; LLM audit</title>"
           f"<style>{CSS}{EXTRA_CSS}{MORE_CSS}{AUDIT_CSS}</style>"
           f"<button class='toggle'>theme</button><div class='wrap'>"
           f"<h1>FNSPID window devset &mdash; LLM audit</h1>"
           f"<p class='sub'>60 records. For each one the model chose a role, ranked the "
           f"sentences, and wrote the summary that became the record text. All three are "
           f"shown against their sources so they can be checked.</p>"
           f"{head}{''.join(sections)}</div><script>{JS}</script>")

    outp = ROOT / args.out
    outp.write_text(doc, encoding="utf-8")
    print(f"wrote {len(recs)} records -> {outp}  ({outp.stat().st_size/1024:.0f} KB)")


if __name__ == "__main__":
    main()
