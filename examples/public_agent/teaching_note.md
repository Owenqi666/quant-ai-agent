# A public teaching signal: close minus open

Original project teaching note, version 1. This document contains no external
paper text and uses no real market observations. Its purpose is to show a
traceable software workflow, not to propose an investment strategy.

## Page 1: formula and source meaning

Formula: score = close - open.

Fields: open is the opening price for session t; close is the closing price for
the same asset and session t. Both prices use the same arbitrary synthetic unit.
The score is available only after session t closes. No trailing window is used.

Mathematical direction: a higher score means a larger close-minus-open price
displacement for that session. A negative score means close is below open. This
statement does not assert a relationship with any future return.

Scientific unknown: no economic mechanism or predictive validity is asserted.
Keep the economic mechanism unresolved. A successful calculation is not a
human review, causal finding, or investment-performance result.

The following identifier belongs only to this original project teaching note;
it is not an external paper or a published alpha label.

Alpha#9001: (close - open)

## Page 2: explicit engineering protocol

Input: 6 invented assets on 48 synthetic weekday sessions. The CSV contains a
complete daily OHLCV grid, with quarter-unit prices and invented volume. One
validation session has the same displacement for every asset to exercise the
constant-signal skip rule. These are teaching fixtures, not an exchange calendar.

Engineering timing: form the signal after session t closes, enter at session
t+1 open, and exit at session t+2 open. Signal, entry and exit must all fall
inside validation. The final two validation signal days are purged. Test rows
remain reserved; the calculation uses only the prefix through validation end.

Engineering weights: average cross-sectional ranks of score, centered by their
mean and divided by the sum of absolute centered ranks. Skip a constant signal
cross-section. Gross exposure is one and net exposure is zero on evaluated days.
Costs, slippage, borrow, liquidity, capacity and fills are not modeled.

Reported quantities: validation-only rank IC, gross return, coverage and exact
day counts. They test software arithmetic and alignment on invented data only.
Human review and research validity remain pending.

Demo transport: responses are hand-authored fixtures passed through the normal
proposal parser and validator. No model account, paid model, or network model
request is involved. Literal citation checks do not establish semantic fidelity.
