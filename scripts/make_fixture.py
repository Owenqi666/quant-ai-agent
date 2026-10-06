#!/usr/bin/env python3
"""Generate deterministic synthetic OHLCV for software validation only.

No network calls, external market data, holiday calendar, or predictive signal
are used. Integer arithmetic and an explicitly defined pseudorandom generator
make the CSV and metadata bytes independent of a numerical-library version.
"""

from __future__ import annotations

import argparse
import csv
import hashlib
import io
import json
from datetime import date, timedelta
from pathlib import Path


DEFAULT_SEED = 20260920
GENERATOR_VERSION = "synthetic-ohlcv-v1"
SCALE = 1_000_000


class DeterministicRandom:
    """Explicit 64-bit LCG; suitable for fixtures, never cryptography."""

    def __init__(self, seed: int) -> None:
        self.state = seed & ((1 << 64) - 1)

    def integer(self, lower: int, upper: int) -> int:
        self.state = (
            6364136223846793005 * self.state + 1442695040888963407
        ) & ((1 << 64) - 1)
        return lower + ((self.state >> 16) % (upper - lower + 1))


def fixture_calendar(start: date, count: int) -> list[str]:
    """Produce expected dates independently of observations, Monday-Friday."""
    dates = []
    current = start
    while len(dates) < count:
        if current.weekday() < 5:
            dates.append(current.isoformat())
        current += timedelta(days=1)
    return dates


def price_text(value: int) -> str:
    return f"{value // SCALE}.{value % SCALE:06d}"


def generate(output_dir: Path, seed: int = DEFAULT_SEED) -> dict:
    rng = DeterministicRandom(seed)
    calendar = fixture_calendar(date(2022, 1, 3), 400)
    universe = [f"SYN{i:03d}" for i in range(1, 13)]
    previous = {asset: (35 + i * 9) * SCALE for i, asset in enumerate(universe)}
    buffer = io.StringIO(newline="")
    writer = csv.writer(buffer, lineterminator="\n")
    writer.writerow(["date", "asset", "open", "high", "low", "close", "volume"])
    for session in calendar:
        market_move = rng.integer(-7500, 7500)
        for i, asset in enumerate(universe):
            overnight = rng.integer(-4000, 4000)
            opening = previous[asset] * (SCALE + overnight) // SCALE
            intraday = market_move + rng.integer(-12000, 12000)
            closing = opening * (SCALE + intraday) // SCALE
            high = max(opening, closing) * (SCALE + rng.integer(500, 7500)) // SCALE
            low = min(opening, closing) * (SCALE - rng.integer(500, 7500)) // SCALE
            volume = (i + 3) * 100_000 + rng.integer(0, 900_000) + abs(intraday) * 20
            writer.writerow(
                [session, asset, *map(price_text, (opening, high, low, closing)), volume]
            )
            previous[asset] = closing
    csv_bytes = buffer.getvalue().encode("utf-8")
    metadata = {
        "data_kind": "synthetic",
        "version": GENERATOR_VERSION,
        "seed": seed,
        "generator": "scripts/make_fixture.py",
        "random_generator": {
            "name": "LCG64",
            "multiplier": 6364136223846793005,
            "increment": 1442695040888963407,
            "modulus": "2**64",
            "output": "(state >> 16) modulo inclusive integer range",
        },
        "adjustment": "none; synthetic prices have no splits, dividends or corporate actions",
        "universe": universe,
        "calendar_kind": "synthetic Monday-Friday dates; NOT an exchange calendar",
        "calendar_dates": calendar,
        "sessions": len(calendar),
        "rows": len(calendar) * len(universe),
        "columns": ["date", "asset", "open", "high", "low", "close", "volume"],
        "price_units": "synthetic currency units; fixed 6 decimal places",
        "volume_units": "synthetic shares",
        "field_availability": {
            "open": "session open",
            "high": "after session close",
            "low": "after session close",
            "close": "after session close",
            "volume": "after session close",
        },
        "market_sha256": hashlib.sha256(csv_bytes).hexdigest(),
        "limitations": [
            "Software fixture only; metrics provide no evidence of investment performance.",
            "The calendar includes public holidays and must not be treated as an exchange calendar.",
            "A fixed complete universe excludes delistings, missing bars and real survivorship effects.",
            "There are no corporate actions, fundamental data, quotes, spreads or intraday timestamps.",
            "No alpha profitability or causal economic relationship is engineered into this generator.",
            "Daily high, low, close and volume are available only after the session; execution must be later.",
        ],
    }
    output_dir.mkdir(parents=True, exist_ok=True)
    (output_dir / "market.csv").write_bytes(csv_bytes)
    (output_dir / "metadata.json").write_text(
        json.dumps(metadata, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )
    return metadata


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--output-dir",
        type=Path,
        default=Path(__file__).resolve().parents[1] / "examples" / "alpha101",
    )
    parser.add_argument("--seed", type=int, default=DEFAULT_SEED)
    args = parser.parse_args()
    metadata = generate(args.output_dir, args.seed)
    print(
        json.dumps(
            {"rows": metadata["rows"], "market_sha256": metadata["market_sha256"]}
        )
    )


if __name__ == "__main__":
    main()
