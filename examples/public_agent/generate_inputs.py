"""Original, deterministic public teaching inputs; no market feed or paper data."""
from datetime import date, timedelta
import csv
import hashlib
import json
from pathlib import Path


def generate(output):
    output = Path(output); output.mkdir(parents=True, exist_ok=True)
    dates = []; day = date(2025, 1, 6)
    while len(dates) < 48:
        if day.weekday() < 5:
            dates.append(day.isoformat())
        day += timedelta(days=1)
    assets = ['DEMO%02d' % n for n in range(1, 7)]
    with (output / 'market.csv').open('w', newline='') as stream:
        writer = csv.writer(stream, lineterminator='\n')
        writer.writerow(['date', 'asset', 'open', 'high', 'low', 'close', 'volume'])
        for t, day in enumerate(dates):
            for a, asset in enumerate(assets):
                opening = 40 + 3 * a + ((t * (a + 2) + a * a) % 17) / 4
                displacement = 0.5 if t == 22 else (((t * 5 + a * 3) % 13) - 6) / 4
                close = opening + displacement
                writer.writerow([day, asset, '%.2f' % opening, '%.2f' % (max(opening, close) + .5),
                                 '%.2f' % (min(opening, close) - .5), '%.2f' % close,
                                 10000 + 113 * t + 701 * a])
    metadata = {'data_kind': 'synthetic', 'version': 'public-agent-quarter-grid-v1',
        'generator': 'examples/public_agent/generate_inputs.py (integer modular quarter-price grid)',
        'adjustment': 'none; synthetic prices have no corporate actions',
        'calendar_kind': 'synthetic Monday-Friday dates; not an exchange calendar',
        'calendar_dates': dates, 'universe': assets, 'sessions': len(dates), 'rows': len(dates) * len(assets),
        'columns': ['date', 'asset', 'open', 'high', 'low', 'close', 'volume'],
        'field_availability': {'open': 'session open', **{f: 'after session close' for f in ('high', 'low', 'close', 'volume')}},
        'market_sha256': hashlib.sha256((output / 'market.csv').read_bytes()).hexdigest(),
        'limitations': ['Entirely invented teaching grid; no real market observations or predictive claim.']}
    evaluation = {'splits': {n: {'start': dates[s], 'end': dates[e]} for n, s, e in
        [('train', 0, 15), ('validation', 16, 35), ('test', 36, 47)]}, 'min_assets': 3}
    for name, value in [('metadata.json', metadata), ('evaluation.json', evaluation)]:
        (output / name).write_text(json.dumps(value, indent=2) + '\n')
    return metadata, evaluation


if __name__ == '__main__':
    generate(Path(__file__).parent)
