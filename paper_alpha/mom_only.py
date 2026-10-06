"""Fixed development-only industry-portfolio MOM project, independent of fixture contracts.

The only reused calculation helpers are ranking, weights and finite reductions.
This module has no database, HTTP, author-data or final-test execution path.
"""
from __future__ import annotations

from calendar import monthrange
from copy import deepcopy
from decimal import Decimal, localcontext
import math
import re
import statistics

from .monthly_evaluation import _terciles, _weights, _sum, _mean
from .storage import digest

SEMANTICS_VERSION = "industry-mom-project-v1"
SOURCE_CONTRACT = "kenneth-french-49-value-weighted-monthly-v1"
STUDY_ID = "ff49-industry-mom-project-v1"
SOURCE_ID = "kenneth-french-49-industry-monthly-value-weighted"
SOURCE_SECTION = "Average Value Weighted Returns -- Monthly"
SOURCE_URL = "https://mba.tuck.dartmouth.edu/pages/faculty/ken.french/ftp/49_Industry_Portfolios_CSV.zip"
STRATEGIES = ("momentum", "same_sample_equal_weight_long_only")
ASSETS = "Agric Food Soda Beer Smoke Toys Fun Books Hshld Clths Hlth MedEq Drugs Chems Rubbr Txtls BldMt Cnstr Steel FabPr Mach ElcEq Autos Aero Ships Guns Gold Mines Coal Oil Util Telcm PerSv BusSv Hardw Softw Chips LabEq Paper Boxes Trans Whlsl Rtail Meals Banks Insur RlEst Fin Other".split()
WARNINGS = [
    "project_modification: Precomputed US industry portfolios are not individual stocks or an original-paper reproduction.",
    "development_only: Selected historical 2010-2011 development interval; no reserved 2012-2013 returns are admitted, and no genuinely blind-test claim is made.",
    "revised_source: The current public source contains retrospectively revised historical returns, not an as-of-2010 investable data snapshot.",
    "tool_declared_choices: Source, minimum sample, terciles, weights, holding labels and intervals are project choices, not human-confirmed research judgments.",
    "gross_only: No costs, financing, borrow, execution or portfolio tradability model is included.",
    "unequal_exposures: Momentum gross/net exposure is 1/0; the long-only baseline is 1/1. Return differences are not same-risk performance or evidence of an advantage.",
    "complete_formation: Eleven H-12..H-2 calendar-month returns are required; zero returns remain valid and missing returns are never filled.",
    "frozen_weights: Holding-label gaps never alter formation membership or weights; each portfolio and paired comparison declares its available-month denominator.",
    "return_index: Gross monthly return indices require an uninterrupted evaluable path with positive gross factors; missing months are not treated as zero returns.",
    "source_identity: Raw-file hashes are declarations here; the source parser and workflow must separately verify the actual archived bytes.",
]


class MomOnlyError(ValueError):
    def __init__(self, code, message):
        self.code = code
        super().__init__(f"{code}: {message}")


def _fail(code, message):
    raise MomOnlyError(code, message)


def _closed(value, fields, label):
    if not isinstance(value, dict) or set(value) != set(fields):
        _fail("invalid_fields", f"{label} must have exactly the declared fields.")


def _fixed(value, expected, label):
    if type(value) is not type(expected) or value != expected:
        _fail("fixed_contract", f"{label} is fixed in this research version.")


def _move(month, offset):
    year, number = map(int, month.split("-"))
    year, number = divmod(year * 12 + number - 1 + offset, 12)
    return f"{year:04d}-{number + 1:02d}"


def _months(start, end):
    result = []
    while start <= end:
        result.append(start)
        start = _move(start, 1)
    return result


def default_config():
    """An explicit tool-selected example contract, never a human approval."""
    return {
        "schema_version": 1, "study_id": STUDY_ID,
        "research_scope": "project_modification", "source_contract": SOURCE_CONTRACT,
        "development": {"start": "2010-01", "end": "2011-12"},
        "reserved": {"start": "2012-01", "end": "2013-12"},
        "signal": {"window_months": 11, "skip_months": 1,
                   "missing_policy": "complete", "fill_policy": "none"},
        "min_formation_assets": 30,
        "portfolio": "tercile_equal_weight_gross1_net0",
        "baseline": "same_sample_equal_weight_long_only",
        "cost_model": "gross_only_no_cost_model",
    }


def validate_config(config):
    expected = default_config()
    _closed(config, expected, "config")
    for key, value in expected.items():
        if isinstance(value, dict):
            _closed(config[key], value, key)
            for child, selected in value.items():
                _fixed(config[key][child], selected, f"{key}.{child}")
        else:
            _fixed(config[key], value, key)
    return deepcopy(config)


def validate_panel(panel):
    """Admit only the fixed 36-month, 49-industry source-derived panel."""
    _closed(panel, ("schema_version", "data_kind", "source_id", "asset_kind",
                    "return_semantics", "months", "assets", "returns", "source"), "panel")
    for key, value in {
        "schema_version": 1, "data_kind": "market_derived_portfolio_returns",
        "source_id": SOURCE_ID, "asset_kind": "industry_portfolio",
        "return_semantics": "monthly_total_return_decimal",
    }.items():
        _fixed(panel[key], value, "panel." + key)
    months, assets, rows = panel["months"], panel["assets"], panel["returns"]
    if not isinstance(months, list) or months != _months("2009-01", "2011-12"):
        _fail("month_axis", "Provide exactly ordered, unique months 2009-01 through 2011-12; reserved values are forbidden.")
    if not isinstance(assets, list) or assets != ASSETS:
        _fail("asset_axis", "Provide exactly the 49 frozen source industry column names in source order.")
    source = panel["source"]
    _closed(source, ("archive_sha256", "csv_sha256", "download_url", "section",
                     "source_contract", "header_preamble"), "source")
    for key in ("archive_sha256", "csv_sha256"):
        if not isinstance(source[key], str) or not re.fullmatch(r"[0-9a-f]{64}", source[key]):
            _fail("source_digest", "Source file digests must be SHA-256 hex strings.")
    _fixed(source["download_url"], SOURCE_URL, "source.download_url")
    _fixed(source["section"], SOURCE_SECTION, "source.section")
    _fixed(source["source_contract"], SOURCE_CONTRACT, "source.source_contract")
    preamble = source["header_preamble"]
    if (not isinstance(preamble, list) or not 1 <= len(preamble) <= 128
            or any(not isinstance(line, str) or len(line) > 4096 for line in preamble)
            or sum(len(line.encode("utf8")) for line in preamble) > 32768
            or any(re.match(r"\s*(?:\d{6}|\d{4}-\d{2})(?:\s|,)", line) for line in preamble)):
        _fail("source_preamble", "Provide bounded source text lines, with no return-data rows.")
    if not isinstance(rows, list) or len(rows) > 36 * 49:
        _fail("return_axis", "Provide a bounded return panel; omitted cells remain missing rows, never filled returns.")
    known_months, known_assets, seen = set(months), set(assets), set()
    values = {month: {} for month in months}
    for row in rows:
        _closed(row, ("month", "asset", "value"), "return row")
        month, asset, value = row["month"], row["asset"], row["value"]
        if not isinstance(month, str) or month not in known_months:
            _fail("return_month", "Return rows must stay inside the development input; reserved rows are forbidden.")
        if not isinstance(asset, str) or asset not in known_assets:
            _fail("return_asset", "Unknown source industry.")
        if (month, asset) in seen:
            _fail("duplicate_return", "Duplicate industry-month return.")
        seen.add((month, asset))
        if value is not None:
            try:
                valid = type(value) in (int, float) and math.isfinite(float(value)) and value >= -1
            except (OverflowError, ValueError):
                valid = False
            if not valid:
                _fail("invalid_return", "Returns must be null or finite decimal numbers >= -1; booleans are invalid.")
        values[month][asset] = value
    return values


def _compound(values):
    # Eleven factors only. 16,384 decimal digits cover the exact products of
    # eleven binary64 values, including subnormals. The independent oracle uses
    # bounded rational arithmetic instead. No ordering-dependent rounded ties.
    with localcontext() as context:
        context.prec = 16384
        total = Decimal(1)
        for value in values:
            total *= 1 + Decimal.from_float(float(value))
        result = float(total - 1)
    return result if math.isfinite(result) else None


def _portfolio(identity, weights, reasons, labels, state):
    out = {"id": identity, "status": "unavailable",
           "weights": [{"asset": a, "weight": weights[a]} for a in sorted(weights)],
           "gross_exposure": _sum(abs(w) for w in weights.values()) if weights else None,
           "net_exposure": _sum(weights.values()) if weights else None,
           "gross_return": None, "gross_return_index": None, "gross_drawdown": None,
           "reasons": list(reasons)}
    if weights:
        missing = [a for a in sorted(weights) if labels[a] is None]
        if missing:
            out["reasons"].extend("holding_label_missing:" + a for a in missing)
        else:
            value = _sum(weights[a] * labels[a] for a in sorted(weights))
            if value is None:
                out["reasons"].append("nonfinite_portfolio_return")
            else:
                out.update(status="evaluated", gross_return=value)
    value = out["gross_return"]
    if state["index"] is not None and value is not None and value > -1:
        index = state["index"] * (1 + value)
        if math.isfinite(index) and index > 0:
            state["index"] = index
            state["peak"] = max(state["peak"], index)
            out.update(gross_return_index=index, gross_drawdown=index / state["peak"] - 1)
        else:
            state["index"] = None
            out["reasons"].append("cumulative_numeric_unavailable")
    else:
        if state["index"] is None:
            out["reasons"].append("cumulative_path_unavailable")
        elif value is not None and value <= -1:
            out["reasons"].append("cumulative_nonpositive_factor")
        state["index"] = None
    return out


def _moments(values):
    mean, volatility, ratio = _mean(values), None, None
    if len(values) >= 2:
        try:
            deviation = statistics.stdev(values)
            annual = deviation * math.sqrt(12)
            if math.isfinite(annual):
                volatility = annual
            if deviation > 0 and mean is not None:
                value = mean / deviation * math.sqrt(12)
                ratio = value if math.isfinite(value) else None
        except (OverflowError, ValueError, ZeroDivisionError):
            pass
    return {"mean_gross_return": mean, "annualized_sample_volatility": volatility,
            "annualized_mean_over_volatility_zero_rf": ratio}


def _summary(months, identity):
    rows = [next(s for s in month["strategies"] if s["id"] == identity) for month in months]
    values = [row["gross_return"] for row in rows if row["gross_return"] is not None]
    complete = bool(rows) and all(row["gross_return_index"] is not None for row in rows)
    return {"strategy_id": identity, "months_total": len(rows), "months_evaluated": len(values),
            "excluded_months": [{"month": month["month"], "reasons": row["reasons"]}
                                for month, row in zip(months, rows) if row["gross_return"] is None],
            **_moments(values), "cumulative_complete": complete,
            "terminal_gross_return_index": rows[-1]["gross_return_index"] if complete else None,
            "max_gross_drawdown": min(row["gross_drawdown"] for row in rows) if complete else None}


def evaluate(panel, config):
    config = validate_config(config)
    values = validate_panel(panel)
    states = {s: {"index": 1., "peak": 1.} for s in STRATEGIES}
    months = []
    for target in _months(config["development"]["start"], config["development"]["end"]):
        history = [_move(target, offset) for offset in range(-12, -1)]
        signals, excluded = [], []
        # This entire formation pass accesses history only, before H labels.
        for asset in sorted(panel["assets"]):
            absent = [month for month in history if asset not in values[month]]
            null = [month for month in history if asset in values[month] and values[month][asset] is None]
            missing = sorted(absent + null)
            if missing:
                excluded.append({"asset": asset, "reasons": (["formation_missing_row"] if absent else []) + (["formation_null_value"] if null else []),
                                 "missing_months": missing, "observed_months": 11 - len(missing)})
                continue
            momentum = _compound([values[month][asset] for month in history])
            if momentum is None:
                excluded.append({"asset": asset, "reasons": ["nonfinite_formation_signal"],
                                 "missing_months": [], "observed_months": 11})
                continue
            signals.append({"asset": asset, "momentum": momentum})
        groups = _terciles({r["asset"]: r["momentum"] for r in signals})
        for row in signals:
            row["mom_group"] = groups[row["asset"]]
        weights, reasons = _weights(signals, "momentum", config["min_formation_assets"])
        baseline = ({r["asset"]: 1 / len(signals) for r in signals}
                    if len(signals) >= config["min_formation_assets"] else {})
        baseline_reasons = [] if baseline else ["insufficient_eligible_assets"]
        labels = {r["asset"]: values[target].get(r["asset"]) for r in signals}
        strategies = [_portfolio("momentum", weights, reasons, labels, states["momentum"]),
                      _portfolio(STRATEGIES[1], baseline, baseline_reasons, labels, states[STRATEGIES[1]])]
        paired = all(s["gross_return"] is not None for s in strategies)
        as_of = _move(target, -1)
        year, number = map(int, as_of.split("-"))
        months.append({"month": target,
                       "as_of": f"{as_of}-{monthrange(year, number)[1]:02d}",
                       "formation_months": history, "skipped_month": _move(target, -1),
                       "status": "evaluated" if paired else "partial" if any(s["gross_return"] is not None for s in strategies) else "unavailable",
                       "eligible_assets": [r["asset"] for r in signals], "exclusions": excluded,
                       "signals": signals,
                       "labels": [{"asset": a, "return_value": labels[a], "reasons": [] if labels[a] is not None else ["holding_missing_row" if a not in values[target] else "holding_null_value"]}
                                  for a in sorted(labels)],
                       "strategies": strategies,
                       "difference_gross_return": _sum([strategies[0]["gross_return"], -strategies[1]["gross_return"]]) if paired else None})
    differences = [m["difference_gross_return"] for m in months if m["difference_gross_return"] is not None]
    paired = {"definition": "momentum minus same_sample_equal_weight_long_only; unequal net exposure",
              "months_total": len(months), "months_evaluated": len(differences),
              "evaluated_months": [m["month"] for m in months if m["difference_gross_return"] is not None],
              "excluded_months": [{"month": m["month"], "reasons": {s["id"]: s["reasons"] for s in m["strategies"]}}
                                  for m in months if m["difference_gross_return"] is None],
              "mean_gross_difference": _mean(differences)}
    return {"schema_version": 1, "semantics_version": SEMANTICS_VERSION,
            "research_scope": "project_modification", "data_kind": panel["data_kind"],
            "study_id": STUDY_ID, "source_id": SOURCE_ID, "asset_kind": panel["asset_kind"],
            "return_semantics": panel["return_semantics"], "source": deepcopy(panel["source"]),
            "config": config, "config_digest": digest(config), "input_digest": digest(panel),
            "status": "evaluated" if len(differences) == len(months) else "partial" if any(s["gross_return"] is not None for m in months for s in m["strategies"]) else "not_evaluable",
            "human_judgment": None, "reserved_evaluated": False, "warnings": list(WARNINGS),
            "months": months, "summary": [_summary(months, s) for s in STRATEGIES], "paired_comparison": paired}
