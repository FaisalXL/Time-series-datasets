#!/usr/bin/env python3
"""Download the raw FNSPID dataset (Zihan1004/FNSPID) from HuggingFace.

Raw files are written **outside** the git repo, under a top-level ``raw_data/``
directory (default: ``<repo_parent>/raw_data/05_fnspid``), so the multi-GB
downloads never get caught by git. The build script reads from there.

Default download (~6.3 GB), the recommended "start here" slice:
  - Stock_news/All_external.csv      (5.73 GB) curated news set
  - Stock_price/full_history.zip     (590 MB)  per-ticker daily OHLCV CSVs

Pass --include-full-news to additionally pull the full 23.2 GB news wire
(Stock_news/nasdaq_exteral_data.csv).

Examples:
  python scripts/download_fnspid.py                  # default slice + extract prices
  python scripts/download_fnspid.py --include-full-news
  python scripts/download_fnspid.py --no-extract     # skip unzipping prices
  python scripts/download_fnspid.py --out-dir /some/other/raw_data
"""

from __future__ import annotations

import argparse
import sys
import zipfile
from pathlib import Path

REPO_ID = "Zihan1004/FNSPID"
REPO_TYPE = "dataset"

# Script lives at <repo>/05_fnspid/scripts/download_fnspid.py
SCRIPT_DIR = Path(__file__).resolve().parent
DATASET_DIR = SCRIPT_DIR.parent              # .../05_fnspid
REPO_ROOT = DATASET_DIR.parent               # .../Time-series-datasets (git root)
REPO_PARENT = REPO_ROOT.parent               # .../defu

# raw_data sibling of the git repo -> never tracked by git
DEFAULT_OUT_DIR = REPO_PARENT / "raw_data" / "05_fnspid"

DEFAULT_FILES = [
    "Stock_news/All_external.csv",
    "Stock_price/full_history.zip",
]
FULL_NEWS_FILE = "Stock_news/nasdaq_exteral_data.csv"
PRICE_ZIP = "Stock_price/full_history.zip"


def human_size(num_bytes: int) -> str:
    size = float(num_bytes)
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if size < 1024 or unit == "TB":
            return f"{size:.1f} {unit}"
        size /= 1024
    return f"{size:.1f} TB"


def assert_outside_repo(out_dir: Path) -> None:
    """Refuse to download into the git repo, to keep raw GBs out of git."""
    try:
        out_dir.resolve().relative_to(REPO_ROOT.resolve())
    except ValueError:
        return  # out_dir is NOT inside the repo -> good
    raise SystemExit(
        f"Refusing to download into the git repo.\n"
        f"  out_dir : {out_dir}\n"
        f"  repo    : {REPO_ROOT}\n"
        f"Pick a path outside the repo (default: {DEFAULT_OUT_DIR})."
    )


def download_files(files: list[str], out_dir: Path) -> list[Path]:
    try:
        from huggingface_hub import hf_hub_download
    except ImportError as exc:
        raise SystemExit(
            "huggingface_hub is required. Install with:\n"
            "  pip install -r requirements.txt\n"
            "(or: pip install huggingface_hub)"
        ) from exc

    paths: list[Path] = []
    for f in files:
        print(f"[download] {REPO_ID}:{f} -> {out_dir}", flush=True)
        local = hf_hub_download(
            repo_id=REPO_ID,
            filename=f,
            repo_type=REPO_TYPE,
            local_dir=str(out_dir),
        )
        p = Path(local)
        print(f"[done]     {p}  ({human_size(p.stat().st_size)})", flush=True)
        paths.append(p)
    return paths


def extract_prices(out_dir: Path) -> None:
    zip_path = out_dir / PRICE_ZIP
    if not zip_path.exists():
        print(f"[extract]  skip: {zip_path} not present", flush=True)
        return
    dest = out_dir / "prices"
    dest.mkdir(parents=True, exist_ok=True)
    print(f"[extract]  {zip_path} -> {dest}", flush=True)
    with zipfile.ZipFile(zip_path) as zf:
        # Skip macOS resource-fork junk (__MACOSX/, ._* AppleDouble files).
        members = [
            m
            for m in zf.namelist()
            if "__MACOSX" not in m and not Path(m).name.startswith("._")
        ]
        zf.extractall(dest, members=members)
    n_csv = sum(1 for _ in dest.rglob("*.csv"))
    print(f"[extract]  done: {n_csv} price CSV files under {dest}", flush=True)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Download raw FNSPID data from HuggingFace into raw_data/ (outside git).",
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument(
        "--out-dir",
        type=Path,
        default=DEFAULT_OUT_DIR,
        help=f"Where to download raw files (default: {DEFAULT_OUT_DIR})",
    )
    parser.add_argument(
        "--include-full-news",
        action="store_true",
        help=f"Also download the full 23.2 GB news wire ({FULL_NEWS_FILE}).",
    )
    parser.add_argument(
        "--files",
        nargs="+",
        default=None,
        metavar="HF_PATH",
        help="Explicit list of HF file paths to download (overrides defaults).",
    )
    parser.add_argument(
        "--no-extract",
        action="store_true",
        help="Do not unzip full_history.zip after downloading.",
    )
    return parser.parse_args()


def main() -> None:
    args = parse_args()
    out_dir = args.out_dir
    assert_outside_repo(out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)

    if args.files is not None:
        files = list(args.files)
    else:
        files = list(DEFAULT_FILES)
        if args.include_full_news and FULL_NEWS_FILE not in files:
            files.append(FULL_NEWS_FILE)

    print(f"[plan]     repo={REPO_ID}  out_dir={out_dir}", flush=True)
    print(f"[plan]     files={files}", flush=True)

    download_files(files, out_dir)

    if not args.no_extract:
        extract_prices(out_dir)

    print("[ok]       FNSPID raw download complete.", flush=True)
    print(f"[ok]       Raw data lives at: {out_dir}", flush=True)


if __name__ == "__main__":
    sys.exit(main())
