# Synthetic intraday return

An original project-authored engineering fixture for Paper-to-Alpha. The
companion single-page PDF is original content prepared for public software
demonstration. It is not an academic paper, an investment hypothesis, or a
strategy result.

The executable definition is `close / open - 1`, describing the completed
session's open-to-close change. Daily close is available after the session
closes. Higher values form the long side under a local demo convention; no
predictive economic mechanism is claimed.

Inputs use deterministic synthetic OHLCV, twelve synthetic assets, and weekday
dates. The local evaluation enters at the next open and exits at the following
open. Test evaluation stays locked. Transaction costs, slippage, borrowing,
and a complete execution model are absent.

A successful run exercises PDF evidence checks and actual factor calculation.
Saved metrics, events, input snapshots, and reports can then be verified.
Metrics on this fixture provide no evidence of investment performance.
