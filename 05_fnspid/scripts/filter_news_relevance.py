#!/usr/bin/env python3
"""Optional post-build stage: filter FNSPID CPT records by news relevance.

Uses a **local LLM as a judge** (not a rewriter) to decide whether each record's
news text is *specifically about the paired ticker* versus broad market/macro
commentary merely tagged to it. Relevant records are kept **with their original
real text unchanged**; off-topic ones are dropped. This directly attacks the
text-ticker attribution noise (CHALLENGES.md §B1) while preserving
``text_quality: "real"``.

Important guardrails:
  * The judge sees **only the article + ticker** — never the price window or its
    direction — so there is no label leakage / lookahead.
  * Text is never rewritten; we only keep or drop. Provenance of the decision is
    stored on each kept record under ``relevance``.

Works with any OpenAI-compatible chat endpoint (vLLM, SGLang, Ollama, LM Studio).
Configure under the ``relevance:`` block in config.example.yaml.

Examples:
  # quick test on 20 records, print verdicts
  python scripts/filter_news_relevance.py --limit 20 --verbose
  # full filter pass
  python scripts/filter_news_relevance.py
  # point at a different server/model
  python scripts/filter_news_relevance.py \
      --set relevance.base_url=http://localhost:8001/v1 \
      --set relevance.model=Qwen/Qwen2.5-14B-Instruct
"""

from __future__ import annotations

import argparse
import json
import re
import sys
import time
import urllib.error
import urllib.request
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Dict, List, Optional, Sequence, Tuple

try:
    import yaml
except ImportError as exc:
    raise SystemExit(
        "PyYAML is required. Install with: pip install -r requirements.txt"
    ) from exc


ROOT = Path(__file__).resolve().parents[1]
DEFAULT_CONFIG = ROOT / "config.example.yaml"

# Marker that begins the templated time-series sentence; everything before it
# (after the "TICKER, DATE. " prefix) is the real article block.
TS_SENTENCE_MARKER = "Daily open, high, low,"

SYSTEM_PROMPT = (
    "You are a financial news classifier. Given a news article and a stock ticker, "
    "decide whether the article is SPECIFICALLY about that company/stock, as opposed "
    "to broad market or macro commentary that merely mentions or is tagged to it "
    "(e.g. index round-ups, Fed/rates pieces, sector-wide lists). "
    "Respond with ONLY compact JSON and nothing else: "
    '{"relevant": true|false, "confidence": 0.0-1.0, "reason": "<=12 words"}'
)


# ---------------------------------------------------------------------------
# Config helpers (same boilerplate as the build script)
# ---------------------------------------------------------------------------


def deep_merge(base: Dict[str, Any], override: Dict[str, Any]) -> Dict[str, Any]:
    merged = dict(base)
    for key, value in override.items():
        if key in merged and isinstance(merged[key], dict) and isinstance(value, dict):
            merged[key] = deep_merge(merged[key], value)
        else:
            merged[key] = value
    return merged


def coerce_value(raw: str) -> Any:
    lowered = raw.strip().lower()
    if lowered in {"true", "yes"}:
        return True
    if lowered in {"false", "no"}:
        return False
    if lowered in {"null", "none", "~"}:
        return None
    if re.fullmatch(r"-?\d+", raw.strip()):
        return int(raw)
    if re.fullmatch(r"-?\d+\.\d+", raw.strip()):
        return float(raw)
    return raw


def parse_set_args(set_args: Sequence[str]) -> Dict[str, Any]:
    result: Dict[str, Any] = {}
    for item in set_args:
        if "=" not in item:
            raise ValueError(f"Invalid --set value (need key=value): {item}")
        key, raw = item.split("=", 1)
        parts = key.split(".")
        cursor = result
        for part in parts[:-1]:
            cursor = cursor.setdefault(part, {})
        cursor[parts[-1]] = coerce_value(raw)
    return result


def load_config(config_path: Path, set_overrides: Sequence[str]) -> Dict[str, Any]:
    with config_path.open(encoding="utf-8") as fh:
        cfg = yaml.safe_load(fh)
    if set_overrides:
        cfg = deep_merge(cfg, parse_set_args(set_overrides))
    return cfg


def resolve_path(path_str: str) -> Path:
    p = Path(path_str)
    return p if p.is_absolute() else ROOT / p


# ---------------------------------------------------------------------------
# Article extraction + LLM call
# ---------------------------------------------------------------------------


def extract_article(rec: Dict[str, Any]) -> str:
    """Recover the real article block from a record's text (drop ts sentence)."""
    text = rec.get("text", "")
    marker = text.rfind(TS_SENTENCE_MARKER)
    body = text[:marker].rstrip() if marker != -1 else text
    prefix = f"{rec.get('ticker', '')}, {rec.get('news_date', '')}. "
    if body.startswith(prefix):
        body = body[len(prefix):]
    return body.strip()


def build_messages(cfg_r: Dict[str, Any], ticker: str, article: str) -> List[Dict[str, str]]:
    user_body = (
        f"Ticker: {ticker}\n"
        f"Article:\n{article[: int(cfg_r.get('max_chars', 2500))]}\n\n"
        f"Is this article specifically about {ticker} (the company)?"
    )
    if cfg_r.get("system_role", True):
        return [
            {"role": "system", "content": SYSTEM_PROMPT},
            {"role": "user", "content": user_body},
        ]
    # Models without system-role support (e.g. Gemma): fold instructions into user turn.
    return [{"role": "user", "content": f"{SYSTEM_PROMPT}\n\n{user_body}"}]


def call_judge(cfg_r: Dict[str, Any], base_url: str, ticker: str, article: str) -> Dict[str, Any]:
    """Call the OpenAI-compatible chat endpoint; return parsed verdict dict."""
    url = base_url.rstrip("/") + "/chat/completions"
    payload: Dict[str, Any] = {
        "model": cfg_r["model"],
        "temperature": float(cfg_r.get("temperature", 0.0)),
        "max_tokens": 80,
        "messages": build_messages(cfg_r, ticker, article),
    }
    enable_thinking = cfg_r.get("enable_thinking")
    if enable_thinking is not None:
        payload["chat_template_kwargs"] = {"enable_thinking": bool(enable_thinking)}
    data = json.dumps(payload).encode("utf-8")
    headers = {
        "Content-Type": "application/json",
        "Authorization": f"Bearer {cfg_r.get('api_key', 'EMPTY')}",
    }
    last_err: Optional[str] = None
    for attempt in range(int(cfg_r.get("max_retries", 3))):
        try:
            req = urllib.request.Request(url, data=data, headers=headers, method="POST")
            with urllib.request.urlopen(req, timeout=int(cfg_r.get("timeout_s", 60))) as resp:
                body = json.loads(resp.read().decode("utf-8"))
            content = body["choices"][0]["message"]["content"]
            return parse_verdict(content)
        except (urllib.error.URLError, urllib.error.HTTPError, TimeoutError, OSError) as exc:
            last_err = str(exc)
            time.sleep(1.5 * (attempt + 1))
        except (KeyError, ValueError, json.JSONDecodeError) as exc:
            last_err = f"bad response: {exc}"
            break
    return {"relevant": None, "confidence": 0.0, "reason": "", "error": last_err or "unknown"}


def parse_verdict(content: str) -> Dict[str, Any]:
    match = re.search(r"\{.*\}", content, re.DOTALL)
    if not match:
        return {"relevant": None, "confidence": 0.0, "reason": "", "error": "no_json"}
    try:
        obj = json.loads(match.group(0))
    except json.JSONDecodeError:
        return {"relevant": None, "confidence": 0.0, "reason": "", "error": "bad_json"}
    return {
        "relevant": obj.get("relevant"),
        "confidence": float(obj.get("confidence", 0.0) or 0.0),
        "reason": str(obj.get("reason", ""))[:120],
    }


# ---------------------------------------------------------------------------
# Pipeline
# ---------------------------------------------------------------------------


def decide(cfg_r: Dict[str, Any], base_url: str, rec: Dict[str, Any]
           ) -> Tuple[Dict[str, Any], Dict[str, Any]]:
    article = extract_article(rec)
    verdict = call_judge(cfg_r, base_url, rec.get("ticker", ""), article)
    return rec, verdict


def run(cfg: Dict[str, Any], limit: Optional[int], verbose: bool) -> Dict[str, Any]:
    cfg_r = cfg["relevance"]
    in_path = resolve_path(cfg_r["input_path"])
    if not in_path.exists():
        raise SystemExit(f"Input not found: {in_path}\nRun scripts/build_cpt_from_hf.py first.")

    records = [json.loads(l) for l in in_path.open(encoding="utf-8") if l.strip()]
    if limit:
        records = records[:limit]

    min_conf = float(cfg_r.get("min_confidence", 0.5))
    keep_on_err = bool(cfg_r.get("keep_on_parse_error", True))
    model_name = cfg_r["model"]
    lanes = [u for u in (cfg_r.get("base_urls") or []) if u] or [cfg_r["base_url"]]

    kept: List[Dict[str, Any]] = []
    counts = {"total": len(records), "kept": 0, "dropped_irrelevant": 0,
              "dropped_low_conf": 0, "kept_on_error": 0, "errors": 0}
    drop_examples: List[Dict[str, Any]] = []

    lane_for = [lanes[i % len(lanes)] for i in range(len(records))]
    with ThreadPoolExecutor(max_workers=int(cfg_r.get("concurrency", 8))) as ex:
        for rec, v in ex.map(lambda args: decide(cfg_r, args[1], args[0]),
                             zip(records, lane_for)):
            err = v.get("error")
            relevant = v.get("relevant")
            conf = float(v.get("confidence", 0.0))

            if err or relevant is None:
                counts["errors"] += 1
                if keep_on_err:
                    counts["kept_on_error"] += 1
                    rec["relevance"] = {"model": model_name, "decision": "kept_on_error",
                                        "error": err}
                    kept.append(rec)
                if verbose:
                    print(f"[err ] {rec['series_id']}: {err}", file=sys.stderr)
                continue

            decision = "drop"
            if relevant and conf >= min_conf:
                decision = "keep"
            elif relevant and conf < min_conf:
                counts["dropped_low_conf"] += 1
            else:
                counts["dropped_irrelevant"] += 1

            if decision == "keep":
                counts["kept"] += 1
                rec["relevance"] = {"model": model_name, "relevant": True,
                                    "confidence": round(conf, 3), "reason": v.get("reason", "")}
                kept.append(rec)
            elif len(drop_examples) < 25:
                drop_examples.append({"series_id": rec["series_id"], "ticker": rec["ticker"],
                                      "confidence": round(conf, 3), "reason": v.get("reason", "")})

            if verbose:
                print(f"[{decision:4s}] {rec['series_id']} conf={conf:.2f} {v.get('reason','')}",
                      file=sys.stderr)

    out_path = resolve_path(cfg_r["output_path"])
    out_path.parent.mkdir(parents=True, exist_ok=True)
    with out_path.open("w", encoding="utf-8") as fh:
        for rec in kept:
            fh.write(json.dumps(rec, ensure_ascii=False) + "\n")

    report = {
        "run_date": datetime.now(UTC).replace(microsecond=0).isoformat().replace("+00:00", "Z"),
        "model": model_name,
        "lanes": lanes,
        "min_confidence": min_conf,
        "input_path": str(cfg_r["input_path"]),
        "output_path": str(cfg_r["output_path"]),
        "counts": counts,
        "keep_rate": round(counts["kept"] / counts["total"], 4) if counts["total"] else 0.0,
        "drop_examples": drop_examples,
    }
    report_path = resolve_path(cfg_r["report_path"])
    with report_path.open("w", encoding="utf-8") as fh:
        json.dump(report, fh, indent=2, ensure_ascii=False)
        fh.write("\n")
    return report


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="LLM relevance filter for FNSPID CPT records (keep real text, drop off-topic).",
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument("--config", type=Path, default=DEFAULT_CONFIG)
    parser.add_argument("--set", action="append", default=[], metavar="KEY=VALUE",
                        help="Override a config key (dotted path). Repeatable.")
    parser.add_argument("--limit", type=int, default=None, help="Only process the first N records.")
    parser.add_argument("--verbose", action="store_true", help="Print per-record verdicts.")
    return parser.parse_args()


def main() -> None:
    args = parse_args()
    cfg = load_config(args.config, args.set)
    report = run(cfg, args.limit, args.verbose)
    c = report["counts"]
    print(
        f"\nDone: kept {c['kept']}/{c['total']} ({report['keep_rate']*100:.1f}%) — "
        f"dropped {c['dropped_irrelevant']} irrelevant, {c['dropped_low_conf']} low-conf, "
        f"{c['errors']} errors ({c['kept_on_error']} kept on error). "
        f"Output: {report['output_path']}",
        file=sys.stderr,
    )


if __name__ == "__main__":
    main()
