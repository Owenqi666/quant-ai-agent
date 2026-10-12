# Public examples

## Original demo: no third-party download

From the repository root, after installing the Python package:

```bash
python scripts/prepare_public_demo.py --output-dir .demo/inputs
python -m paper_alpha run .demo/inputs/task.json --mode fixed --out .demo/run
python -m paper_alpha verify .demo/run
```

The preparation command uses the original one-page engineering note in
`public_demo/`, verifies its PDF hash and cited physical page, and generates
deterministic synthetic OHLCV. The ordinary workflow performs PDF extraction
checks, source-formula checks, bounded subprocess calculation, validation
evaluation, and saved-result verification. It does not insert fabricated
metrics. No model or network call is involved. The candidate is a toy
definition; a completed run is software evidence, not a financial finding.

Inspect `.demo/run/report.md`, `state.json`, and `manifest.json`. A completed
run can be read again with the same `run` command plus `--resume`; input,
source, environment, and policy changes are rejected. Keep the run directory
outside published source. Use a fresh input/run directory for a new demo.

## Alpha101: obtain the paper yourself

The maintained source is Zura Kakushadze's [101 Formulaic Alphas, arXiv
1601.00991v3](https://arxiv.org/abs/1601.00991v3). The repository includes small
formula fixtures, source identity, and synthetic CSV data. It does not
redistribute the third-party PDF, page images, or full extracted paper text.
Obtain the fixed [v3 PDF from its original source](https://arxiv.org/pdf/1601.00991v3)
under terms applicable to your use, then run:

```bash
python scripts/prepare_alpha101.py --pdf ~/Downloads/1601.00991v3.pdf
python -m paper_alpha run examples/alpha101/task.json --mode normalized_fixed --out .demo/alpha101
python -m paper_alpha verify .demo/alpha101
```

The preparation script performs no download. It requires PDF SHA256
`1f9c21afe32dcb3ee77b31548acdaea00451fbfa1c0ee10c907867bcc736fce9`,
re-extracts text with the current environment, and validates every task
citation against its one-based physical PDF page. It creates local
`examples/alpha101/paper.pdf` and `paper.json`, both excluded from Git.
A different edition or mismatched citation fails; the evidence guard is
preserved.

The Alpha101 fixture evaluates supported formulas #006 and #101. Formula #005
remains blocked because the synthetic dataset lacks `vwap`; `close` is never
silently substituted. `normalized_fixed` applies only documented alias
normalization before execution. The deterministic `agent` policy can instead
record and repair supported representation errors within its configured
budget. Neither mode is general model-based self-repair or a reproduction of
WorldQuant BRAIN.

## Test inputs

The default public tests cover expression restrictions, alignment, timing,
synthetic evaluation, and formula presentation without requiring the
third-party PDF. Evidence, workflow, provenance, and recovery tests that use
Alpha101 require `prepare_alpha101.py` first. Synthetic-data limitations are
recorded in `alpha101/metadata.json`; weekday dates are not an exchange
calendar, and the universe has no real survivorship, corporate-action, or
point-in-time guarantees.
