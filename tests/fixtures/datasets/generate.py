"""Reproduce the second controlled synthetic dataset without external data."""
from __future__ import annotations

import argparse
import csv
from datetime import date
import hashlib
import io
import json
from pathlib import Path
import sys

PROJECT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(PROJECT))
from scripts.make_fixture import DeterministicRandom, SCALE, fixture_calendar, price_text


def generate(destination):
    rng = DeterministicRandom(20261001)
    calendar = fixture_calendar(date(2024, 1, 2), 96)
    universe = [f"ALT{index:03d}" for index in range(1, 8)]
    previous = {asset: (17 + index * 7) * SCALE for index, asset in enumerate(universe)}
    output = io.StringIO(newline="")
    writer = csv.writer(output, lineterminator="\n")
    columns = ["date", "asset", "open", "high", "low", "close", "volume"]
    writer.writerow(columns)
    for session in calendar:
        market_move = rng.integer(-5500, 6500)
        for index, asset in enumerate(universe):
            opening = previous[asset] * (SCALE + rng.integer(-3500, 4500)) // SCALE
            intraday = market_move + rng.integer(-13500, 11500)
            closing = opening * (SCALE + intraday) // SCALE
            high = max(opening, closing) * (SCALE + rng.integer(400, 8000)) // SCALE
            low = min(opening, closing) * (SCALE - rng.integer(400, 8000)) // SCALE
            volume = (index + 2) * 70_000 + rng.integer(0, 800_000) + abs(intraday) * 11
            writer.writerow([session, asset, *map(price_text, (opening, high, low, closing)), volume])
            previous[asset] = closing
    payload = output.getvalue().encode()
    metadata = {"data_kind": "synthetic", "version": "controlled-import-ohlcv-v1", "seed": 20261001,
                "generator": "tests/fixtures/datasets/generate.py",
                "generator_sources": {str(path.relative_to(PROJECT)): hashlib.sha256(path.read_bytes()).hexdigest()
                                      for path in (Path(__file__).resolve(), PROJECT / "scripts/make_fixture.py")},
                "adjustment": "none; generated prices have no corporate actions",
                "calendar_kind": "synthetic Monday-Friday sessions; not an exchange calendar",
                "calendar_dates": calendar, "universe": universe, "sessions": len(calendar),
                "rows": len(calendar) * len(universe), "columns": columns,
                "field_availability": {field: "session open" if field == "open" else "after session close" for field in columns[2:]},
                "market_sha256": hashlib.sha256(payload).hexdigest(),
                "limitations": ["Controlled software fixture only; no investment-performance evidence.",
                                "Independent seed, dates and asset universe; shared documented LCG utility.",
                                "No holiday calendar, survivorship, costs, borrow, capacity or fills are modeled.",
                                "No predictive economic relationship is designed into these random prices."]}
    configuration = {"splits": {name: {"start": calendar[start], "end": calendar[end]}
                                 for name, start, end in (("train", 0, 31), ("validation", 32, 63), ("test", 64, 95))},
                     "min_assets": 5}
    destination = Path(destination)
    destination.mkdir(parents=True, exist_ok=True)
    (destination / "market.csv").write_bytes(payload)
    for name, value in (("metadata.json", metadata), ("research_config.json", configuration)):
        (destination / name).write_text(json.dumps(value, indent=2, ensure_ascii=False) + "\n")
    return metadata


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--out", type=Path, required=True)
    args = parser.parse_args()
    result = generate(args.out)
    print(json.dumps({"rows": result["rows"], "market_sha256": result["market_sha256"]}))
