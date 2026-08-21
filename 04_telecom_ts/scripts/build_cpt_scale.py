#!/usr/bin/env python3
"""Build 04_telecom_ts at full scale from the cached TelecomTS file tree.

REPLACES the original builder, which could not do this job:
  * it used `datasets.load_dataset(..., split="train")`, but the HF repo is a FILE TREE
    (33 `processed/chunked.jsonl` files under normal/ and anomalous/), not a split dataset;
  * it emitted no `license`, `alignment`, `domain`, `region` or `period_*`, so every record
    failed `--strict`;
  * its `text_source` values (`dataset_description`, `generated_gpt4`) are not in the SCHEMA §6
    enum {first_party_official, first_party_human, third_party, generated};
  * its `source` was the bare string "AliMaatouk/TelecomTS" with no scheme -- the same defect
    that made 06_stocknet fail 0/4,907 on strict.

MEASURED CEILING: 32,000 windows. The README claimed "~1,260 train samples", a 25x
under-estimate; it was presumably reading one split of an earlier revision.

TEXT: the window `description` only. It is unique per window (32,000/32,000 distinct) and it
states the window's own KPI statistics, which is what ties it to the series.

`anomalies.troubleshooting_tickets` is deliberately NOT concatenated into `text`, even though it
is the most interesting prose in the dataset: there are only **11 distinct tickets across 1,235
anomaly windows**, so appending them would give 112 records the same text and reintroduce the
duplicate-text defect this corpus already tracks. They are preserved in `meta` instead.

LICENCE: the HF dataset is MIT, which SCHEMA §6's frozen v1 enum cannot express. Tagged
`proprietary-review` per team decision, following §6's own listing of "GPT-generated text" under
that value. The MIT origin is recorded in `meta.license_as_published` so the choice is auditable
and reversible if the enum ever gains a slot.

ALIGNMENT is not asserted. Each record is tested with the validator's own `_recites_a_value`
and tagged `recites` or `describes` by the result, so this builder cannot overclaim the way §7
warns builders repeatedly do.

Usage: build_cpt_scale.py [--out PATH] [--limit N]
"""
from __future__ import annotations

import argparse, glob, importlib.util, json, re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
CACHE = ROOT / ".cache/telecom_ts"

_spec = importlib.util.spec_from_file_location(
    "v", "/data/defu/Time-series-datasets/schema/validate.py")
_v = importlib.util.module_from_spec(_spec); _spec.loader.exec_module(_v)

DATASET_URL = "https://huggingface.co/datasets/AliMaatouk/TelecomTS"

# All 14 KPI channels the windows carry. `unit` names follow the corpus convention of
# embedding the physical unit, since the raw KPI names do not state one.
CHANNELS = {
    "RSRP": "rsrp_dbm",
    "DL_BLER": "dl_bler_frac",
    "UL_BLER": "ul_bler_frac",
    "DL_MCS": "dl_mcs_index",
    "UL_MCS": "ul_mcs_index",
    "UL_SNR": "ul_snr_db",
    "UL_NPRB": "ul_nprb_count",
    "TX_Bytes": "tx_bytes",
    "RX_Bytes": "rx_bytes",
    "Estimated_UL_Buffer": "ul_buffer_bytes",
    "PRBs_DL_Current": "prbs_dl_current",
    "PRBs_UL_Current": "prbs_ul_current",
    "PRB_Utilization_DL": "prb_utilization_dl_pct",
    "PRB_Utilization_UL": "prb_utilization_ul_pct",
}


def scenario_of(path: str) -> str:
    return path.replace(str(CACHE) + "/", "").replace("/processed/chunked.jsonl", "")


def iso(ts: str) -> str | None:
    """'2025-01-02 00:06:26.000' -> '2025-01-02T00:06:26'. Times are testbed-relative."""
    if not ts:
        return None
    m = re.match(r"(\d{4}-\d{2}-\d{2})[ T](\d{2}:\d{2}:\d{2})", str(ts))
    return f"{m.group(1)}T{m.group(2)}" if m else str(ts)[:10] or None


def build_record(row: dict, scen: str, scen_desc: str, idx: int) -> dict | None:
    text = (row.get("description") or "").strip()
    kpis = row.get("KPIs") or {}
    if not text or not kpis:
        return None

    freq = f"{int(1000 / (row.get('sampling_rate') or 10))}ms"
    series = []
    for raw, unit in CHANNELS.items():
        vals = kpis.get(raw)
        if not vals:
            continue
        series.append({"values": [None if v is None else round(float(v), 6) for v in vals],
                       "unit": unit, "freq": freq})
    if len(series) < 2:
        return None

    labels = row.get("labels") or {}
    anom = row.get("anomalies") or {}
    kind = "anomaly" if anom.get("exists") else "normal"

    rec = {
        "text": text + "\n\n<ts></ts>",
        "timeseries": series,
        "task_type": "world_knowledge",
        # The dataset's own paper states every description and ticket is model-written; there is
        # no human-authored prose anywhere in this source.
        "text_quality": "generated",
        "text_source": "generated",
        "alignment": "describes",          # replaced below by the validator's own verdict
        "license": "proprietary-review",
        "domain": "telecom",
        "region": "lab",                   # controlled 5G testbed, not a geography
        "source": DATASET_URL,
        "dataset": "telecom_ts",
        "series_id": f"telecomts_{scen.replace('/', '_')}_{idx:05d}",
        "period_start": iso(row.get("start_time")),
        "period_end": iso(row.get("end_time")),
        "meta": {
            "scenario": scen,
            "scenario_description": scen_desc,
            "record_type": kind,
            "sampling_rate_hz": row.get("sampling_rate"),
            "window_steps": len(series[0]["values"]),
            "n_channels": len(series),
            "channels": [c["unit"] for c in series],
            "labels": labels,
            "anomaly_type": anom.get("type"),
            "anomaly_affected_kpis": anom.get("affected_kpis"),
            # 11 distinct tickets serve 1,235 windows, so this is context, not `text`.
            "troubleshooting_ticket": (anom.get("troubleshooting_tickets") or "").strip() or None,
            "license_as_published": "mit",
            "text_source_as_published": "generated_gpt4",
            "source_dataset": "AliMaatouk/TelecomTS",
            "series_source": DATASET_URL,
        },
    }
    rec["alignment"] = "recites" if _v._recites_a_value(rec) else "describes"
    return rec


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--out", default=str(ROOT / "output/telecom_ts_cpt.jsonl"))
    ap.add_argument("--limit", type=int, default=0)
    a = ap.parse_args()

    files = sorted(glob.glob(str(CACHE / "**/chunked.jsonl"), recursive=True))
    if not files:
        raise SystemExit(f"no cached chunked.jsonl under {CACHE}")

    out = Path(a.out); out.parent.mkdir(parents=True, exist_ok=True)
    n = kept = 0
    align = {"recites": 0, "describes": 0}
    kinds = {"normal": 0, "anomaly": 0}
    with out.open("w", encoding="utf-8") as fh:
        for f in files:
            scen = scenario_of(f)
            dpath = Path(f).parent.parent / "raw" / "description.txt"
            scen_desc = " ".join(dpath.read_text(encoding="utf-8").split()) if dpath.exists() else None
            for i, line in enumerate(open(f, encoding="utf-8")):
                if not line.strip():
                    continue
                n += 1
                r = build_record(json.loads(line), scen, scen_desc, i)
                if not r:
                    continue
                fh.write(json.dumps(r, ensure_ascii=False) + "\n")
                kept += 1
                align[r["alignment"]] += 1
                kinds[r["meta"]["record_type"]] += 1
                if a.limit and kept >= a.limit:
                    break
            if a.limit and kept >= a.limit:
                break
    print(f"windows read   {n:,}")
    print(f"records written {kept:,}  -> {out}")
    print(f"alignment      {align}")
    print(f"record_type    {kinds}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
