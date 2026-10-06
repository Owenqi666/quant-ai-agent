"""Independent bounded MOM-only oracle; no production calculator imports.

Own closed admission, exact rational products, pairwise midranks, independently
constructed weights and Decimal reductions. Canonical hashing is the only shared
utility. A failure is never promoted to a supported passing calculation.
"""
from __future__ import annotations

from calendar import monthrange
from copy import deepcopy
from decimal import Decimal, localcontext
from fractions import Fraction
import json
import math
import re

from .storage import digest

_ASSETS = "Agric Food Soda Beer Smoke Toys Fun Books Hshld Clths Hlth MedEq Drugs Chems Rubbr Txtls BldMt Cnstr Steel FabPr Mach ElcEq Autos Aero Ships Guns Gold Mines Coal Oil Util Telcm PerSv BusSv Hardw Softw Chips LabEq Paper Boxes Trans Whlsl Rtail Meals Banks Insur RlEst Fin Other".split()
_STRATEGIES = ("momentum", "same_sample_equal_weight_long_only")
_WARNINGS = [
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


def _month(number):
    year, month = divmod(number, 12)
    return f"{year:04d}-{month + 1:02d}"


def _ordinal(month):
    year, number = map(int, month.split("-"))
    return year * 12 + number - 1


def _admit(panel, config):
    def fields(obj, names):
        if not isinstance(obj, dict) or set(obj) != set(names.split()):
            raise ValueError("reference closed fields mismatch")
    expected = {
        "schema_version": 1, "study_id": "ff49-industry-mom-project-v1",
        "research_scope": "project_modification",
        "source_contract": "kenneth-french-49-value-weighted-monthly-v1",
        "development": {"start": "2010-01", "end": "2011-12"},
        "reserved": {"start": "2012-01", "end": "2013-12"},
        "signal": {"window_months": 11, "skip_months": 1,
                   "missing_policy": "complete", "fill_policy": "none"},
        "min_formation_assets": 30,
        "portfolio": "tercile_equal_weight_gross1_net0",
        "baseline": "same_sample_equal_weight_long_only",
        "cost_model": "gross_only_no_cost_model",
    }
    # JSON representations distinguish bool/int and reject nonfinite numbers.
    if (not isinstance(config, dict)
            or json.dumps(config, sort_keys=True, allow_nan=False) != json.dumps(expected, sort_keys=True, allow_nan=False)):
        raise ValueError("reference fixed config mismatch")
    fields(panel, "schema_version data_kind source_id asset_kind return_semantics months assets returns source")
    for key, value in {
        "schema_version": 1, "data_kind": "market_derived_portfolio_returns",
        "source_id": "kenneth-french-49-industry-monthly-value-weighted",
        "asset_kind": "industry_portfolio", "return_semantics": "monthly_total_return_decimal",
    }.items():
        if type(panel[key]) is not type(value) or panel[key] != value:
            raise ValueError("reference fixed source identity mismatch")
    months = [_month(n) for n in range(2009 * 12, 2012 * 12)]
    if not isinstance(panel["months"], list) or panel["months"] != months:
        raise ValueError("reference month axis mismatch or reserved values")
    if not isinstance(panel["assets"], list) or panel["assets"] != _ASSETS:
        raise ValueError("reference source asset axis mismatch")
    source = panel["source"]
    fields(source, "archive_sha256 csv_sha256 download_url section source_contract header_preamble")
    for key in ("archive_sha256", "csv_sha256"):
        if not isinstance(source[key], str) or not re.fullmatch("[0-9a-f]{64}", source[key]):
            raise ValueError("reference source digest mismatch")
    for key, value in {
        "download_url": "https://mba.tuck.dartmouth.edu/pages/faculty/ken.french/ftp/49_Industry_Portfolios_CSV.zip",
        "section": "Average Value Weighted Returns -- Monthly",
        "source_contract": "kenneth-french-49-value-weighted-monthly-v1",
    }.items():
        if type(source[key]) is not str or source[key] != value:
            raise ValueError("reference source selection mismatch")
    text = source["header_preamble"]
    if (not isinstance(text, list) or not 1 <= len(text) <= 128
            or any(not isinstance(line, str) or len(line) > 4096 for line in text)
            or sum(len(line.encode("utf8")) for line in text) > 32768
            or any(re.match(r"\s*(?:\d{6}|\d{4}-\d{2})(?:\s|,)", line) for line in text)):
        raise ValueError("reference source preamble bounds or data rows")
    rows = panel["returns"]
    if not isinstance(rows, list) or len(rows) > 1764:
        raise ValueError("reference return bounds")
    records = {month: {} for month in months}
    for row in rows:
        fields(row, "month asset value")
        month, asset, value = row["month"], row["asset"], row["value"]
        if not isinstance(month, str) or month not in records:
            raise ValueError("reference return date outside development input")
        if not isinstance(asset, str) or asset not in _ASSETS or asset in records[month]:
            raise ValueError("reference unknown or duplicate industry-month")
        if value is not None:
            if type(value) not in (int, float) or not math.isfinite(float(value)) or value < -1:
                raise ValueError("reference invalid decimal return")
        records[month][asset] = value
    return records


def _finite(value):
    try:
        number = float(value)
    except (OverflowError, ValueError):
        return None
    return number if math.isfinite(number) else None


def _compound(values):
    result = Fraction(1)
    for value in values:
        result = result * (Fraction(1) + Fraction.from_float(float(value)))
        if result.numerator.bit_length() + result.denominator.bit_length() > 40000:
            raise ValueError("reference rational budget exceeded")
    return _finite(result - 1)


def _total(values):
    with localcontext() as context:
        context.prec = 4096
        return _finite(sum((Decimal.from_float(float(v)) for v in values), Decimal(0)))


def _average(values):
    if not values:
        return None
    with localcontext() as context:
        context.prec = 4096
        return _finite(sum((Decimal.from_float(float(v)) for v in values), Decimal(0)) / len(values))


def _portfolio(identity, weights, reasons, labels, state):
    gross = None
    problems = list(reasons)
    if weights:
        missing = sorted(asset for asset in weights if labels[asset] is None)
        problems += ["holding_label_missing:" + asset for asset in missing]
        if not missing:
            with localcontext() as context:
                context.prec = 4096
                gross = _finite(sum((Decimal.from_float(float(weight)) * Decimal.from_float(float(labels[asset]))
                                     for asset, weight in weights.items()), Decimal(0)))
            if gross is None:
                problems.append("nonfinite_portfolio_return")
    index = drawdown = None
    if state["index"] is not None and gross is not None and gross > -1:
        value = state["index"] * (1 + gross)
        if math.isfinite(value) and value > 0:
            state["index"] = value
            state["peak"] = max(state["peak"], value)
            index, drawdown = value, value / state["peak"] - 1
        else:
            state["index"] = None
            problems.append("cumulative_numeric_unavailable")
    else:
        if state["index"] is None:
            problems.append("cumulative_path_unavailable")
        elif gross is not None and gross <= -1:
            problems.append("cumulative_nonpositive_factor")
        state["index"] = None
    return {"id": identity, "status": "evaluated" if gross is not None else "unavailable",
            "weights": [{"asset": asset, "weight": weights[asset]} for asset in sorted(weights)],
            "gross_exposure": _total(abs(v) for v in weights.values()) if weights else None,
            "net_exposure": _total(weights.values()) if weights else None,
            "gross_return": gross, "gross_return_index": index, "gross_drawdown": drawdown, "reasons": problems}


def _summary(months, identity):
    rows = [next(strategy for strategy in month["strategies"] if strategy["id"] == identity) for month in months]
    observed = [row["gross_return"] for row in rows if row["gross_return"] is not None]
    mean, vol, ratio = _average(observed), None, None
    if len(observed) > 1:
        with localcontext() as context:
            context.prec = 4096
            samples = [Decimal.from_float(v) for v in observed]
            center = sum(samples) / len(samples)
            variance = sum((v - center) ** 2 for v in samples) / (len(samples) - 1)
            deviation = variance.sqrt()
            vol = _finite(deviation * Decimal(12).sqrt())
            if deviation:
                ratio = _finite(center / deviation * Decimal(12).sqrt())
    complete = bool(rows) and all(row["gross_return_index"] is not None for row in rows)
    return {"strategy_id": identity, "months_total": len(rows), "months_evaluated": len(observed),
            "excluded_months": [{"month": month["month"], "reasons": row["reasons"]}
                                for month, row in zip(months, rows) if row["gross_return"] is None],
            "mean_gross_return": mean, "annualized_sample_volatility": vol,
            "annualized_mean_over_volatility_zero_rf": ratio, "cumulative_complete": complete,
            "terminal_gross_return_index": rows[-1]["gross_return_index"] if complete else None,
            "max_gross_drawdown": min(row["gross_drawdown"] for row in rows) if complete else None}


def _expected(panel, config, records):
    months = []
    states = {name: {"index": 1., "peak": 1.} for name in _STRATEGIES}
    for ordinal in range(2010 * 12, 2012 * 12):
        month = _month(ordinal)
        history = [_month(n) for n in range(ordinal - 12, ordinal - 1)]
        signals, excluded = [], []
        for asset in sorted(_ASSETS):
            missing = [date for date in history if asset not in records[date]]
            null = [date for date in history if asset in records[date] and records[date][asset] is None]
            if missing or null:
                excluded.append({"asset": asset,
                                 "reasons": (["formation_missing_row"] if missing else []) + (["formation_null_value"] if null else []),
                                 "missing_months": sorted(missing + null), "observed_months": 11 - len(missing) - len(null)})
            else:
                signal = _compound([records[date][asset] for date in history])
                if signal is None:
                    excluded.append({"asset": asset, "reasons": ["nonfinite_formation_signal"], "missing_months": [], "observed_months": 11})
                else:
                    signals.append({"asset": asset, "momentum": signal})
        for row in signals:
            lower = sum(other["momentum"] < row["momentum"] for other in signals)
            equal = sum(other["momentum"] == row["momentum"] for other in signals)
            row["mom_group"] = min(3, (3 * (2 * lower + equal)) // (2 * len(signals)) + 1)
        candidate, long_only, problems = {}, {}, []
        if len(signals) < 30:
            problems = ["insufficient_eligible_assets"]
        else:
            long_only = {row["asset"]: 1 / len(signals) for row in signals}
            for group, amount in ((3, .5), (1, -.5)):
                leg = [row["asset"] for row in signals if row["mom_group"] == group]
                if not leg:
                    candidate = {}
                    problems = [f"empty_required_group:mom={group},id=None"]
                    break
                candidate.update({asset: amount / len(leg) for asset in leg})
        labels = {row["asset"]: records[month].get(row["asset"]) for row in signals}
        strategies = [_portfolio(_STRATEGIES[0], candidate, problems, labels, states[_STRATEGIES[0]]),
                      _portfolio(_STRATEGIES[1], long_only, [] if long_only else ["insufficient_eligible_assets"], labels, states[_STRATEGIES[1]])]
        both = all(strategy["gross_return"] is not None for strategy in strategies)
        as_of = _month(ordinal - 1)
        year, number = map(int, as_of.split("-"))
        months.append({"month": month, "as_of": f"{as_of}-{monthrange(year, number)[1]:02d}",
                       "formation_months": history, "skipped_month": as_of,
                       "status": "evaluated" if both else "partial" if any(strategy["gross_return"] is not None for strategy in strategies) else "unavailable",
                       "eligible_assets": [row["asset"] for row in signals], "exclusions": excluded, "signals": signals,
                       "labels": [{"asset": asset, "return_value": labels[asset], "reasons": [] if labels[asset] is not None else ["holding_missing_row" if asset not in records[month] else "holding_null_value"]}
                                  for asset in sorted(labels)],
                       "strategies": strategies,
                       "difference_gross_return": _total([strategies[0]["gross_return"], -strategies[1]["gross_return"]]) if both else None})
    differences = [month["difference_gross_return"] for month in months if month["difference_gross_return"] is not None]
    paired = {"definition": "momentum minus same_sample_equal_weight_long_only; unequal net exposure",
              "months_total": 24, "months_evaluated": len(differences),
              "evaluated_months": [month["month"] for month in months if month["difference_gross_return"] is not None],
              "excluded_months": [{"month": month["month"], "reasons": {strategy["id"]: strategy["reasons"] for strategy in month["strategies"]}}
                                  for month in months if month["difference_gross_return"] is None],
              "mean_gross_difference": _average(differences)}
    return {"schema_version": 1, "semantics_version": "industry-mom-project-v1",
            "research_scope": "project_modification", "data_kind": panel["data_kind"],
            "study_id": "ff49-industry-mom-project-v1", "source_id": panel["source_id"],
            "asset_kind": panel["asset_kind"], "return_semantics": panel["return_semantics"],
            "source": deepcopy(panel["source"]), "config": deepcopy(config),
            "config_digest": digest(config), "input_digest": digest(panel),
            "status": "evaluated" if len(differences) == 24 else "partial" if any(strategy["gross_return"] is not None for month in months for strategy in month["strategies"]) else "not_evaluable",
            "human_judgment": None, "reserved_evaluated": False, "warnings": list(_WARNINGS),
            "months": months, "summary": [_summary(months, name) for name in _STRATEGIES], "paired_comparison": paired}


def _compare(actual, expected, path, issues):
    if len(issues) >= 64:
        return
    if isinstance(expected, dict):
        if not isinstance(actual, dict) or set(actual) != set(expected):
            issues.append(path + ": object fields differ")
            return
        for key in expected:
            _compare(actual[key], expected[key], path + "." + key, issues)
    elif isinstance(expected, list):
        if not isinstance(actual, list) or len(actual) != len(expected):
            issues.append(path + ": array shape differs")
            return
        for number, (value, wanted) in enumerate(zip(actual, expected)):
            _compare(value, wanted, path + f"[{number}]", issues)
    elif type(expected) is float:
        try:
            equal = type(actual) in (int, float) and math.isfinite(float(actual)) and math.isclose(actual, expected, rel_tol=1e-10, abs_tol=1e-12)
        except (OverflowError, ValueError):
            equal = False
        if not equal:
            issues.append(path + ": numeric value differs")
    elif type(actual) is not type(expected) or actual != expected:
        issues.append(path + ": value differs")


def check(panel, config, result):
    try:
        records = _admit(panel, config)
        expected = _expected(panel, config, records)
    except (ValueError, TypeError, KeyError, OverflowError, ArithmeticError) as exc:
        return {"schema_version": 1, "supported": False, "passed": None,
                "issues": ["input_or_reference_unavailable: " + str(exc)[:512]],
                "expected_result_digest": None, "result_digest": None,
                "numeric_tolerance": {"relative": 1e-10, "absolute": 1e-12},
                "reserved_evaluated": False}
    issues = []
    _compare(result, expected, "result", issues)
    try:
        result_digest = digest(result)
    except (ValueError, TypeError, OverflowError):
        result_digest = None
        issues.append("result: not canonical finite JSON")
    return {"schema_version": 1, "supported": True, "passed": not issues, "issues": issues,
            "expected_result_digest": digest(expected), "result_digest": result_digest,
            "numeric_tolerance": {"relative": 1e-10, "absolute": 1e-12}, "reserved_evaluated": False}
