#!/usr/bin/env python3
"""Download Zerodha's instrument master and save it as a CSV.

Kite publishes the full tradable-instrument list per exchange as a free,
public CSV dump — no API key or access token required:

    https://api.kite.trade/instruments/NSE
    https://api.kite.trade/instruments        (every exchange in one file)

``tradingsymbol`` in that dump is Kite's bare symbol (``RELIANCE``, ``TCS``,
...), not the Yahoo-suffixed form ``watchlist.txt`` uses (``RELIANCE.NS``) —
this script only fetches and saves the raw dump; building a watchlist from it
is a separate step.

Usage:
    python scripts/make_list.py                       # NSE -> instruments_NSE.csv
    python scripts/make_list.py --exchange BSE
    python scripts/make_list.py --exchange all --out all_instruments.csv
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

import requests

INSTRUMENTS_URL = "https://api.kite.trade/instruments"
TIMEOUT_SECONDS = 30


def download_instruments(exchange: str | None = None) -> bytes:
    """Fetch the instrument dump CSV as raw bytes.

    ``exchange`` (e.g. ``"NSE"``, ``"BSE"``, ``"NFO"``) scopes the dump to one
    exchange; ``None`` fetches every exchange in one (much larger) file.
    """
    url = INSTRUMENTS_URL if exchange is None else f"{INSTRUMENTS_URL}/{exchange}"
    response = requests.get(url, timeout=TIMEOUT_SECONDS)
    response.raise_for_status()
    return response.content


def save_instruments(data: bytes, out_path: Path) -> None:
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_bytes(data)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description=__doc__.splitlines()[0],
        formatter_class=argparse.ArgumentDefaultsHelpFormatter,
    )
    parser.add_argument(
        "--exchange",
        default="NSE",
        help="Exchange to fetch (NSE, BSE, NFO, MCX, ...), or 'all' for every exchange",
    )
    parser.add_argument(
        "--out",
        type=Path,
        default=None,
        help="Output CSV path (default: instruments_<EXCHANGE>.csv in the current directory)",
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)

    exchange = None if args.exchange.lower() == "all" else args.exchange.upper()
    out_path = args.out or Path(f"instruments_{exchange or 'all'}.csv")

    try:
        data = download_instruments(exchange)
    except requests.RequestException as exc:
        print(f"Could not download instrument list: {exc}", file=sys.stderr)
        return 1

    save_instruments(data, out_path)
    row_count = data.count(b"\n")  # header + one row per instrument
    print(f"Saved {row_count} rows to {out_path.resolve()}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
