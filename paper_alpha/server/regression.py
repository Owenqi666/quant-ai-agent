"""Frozen scientific contracts and a deliberately small independent oracle.

No generated code is executed. Numeric verification uses Python's standard
library, not the production panel interpreter/evaluator. Only seven explicitly registered
formulas (including three attributed Alpha101 transformations) are supported; a different formula is
reported as not comparable, never silently given a correctness pass. Citation
matching establishes quote provenance, not economic or semantic fidelity.
"""
from __future__ import annotations

import ast
from copy import deepcopy
import csv
from decimal import Context, Decimal, ROUND_HALF_EVEN, localcontext
from fractions import Fraction
import hashlib
import math
from pathlib import Path
import re
import statistics
import unicodedata

from ..evaluation import EXECUTION
from ..expressions import OPERATOR_SEMANTICS_VERSION
from ..storage import read_json
from .oracle_temporal import TEMPORAL_FORMULAS, temporal_factors


CONTRACT_VERSION = 1
ORACLE_VERSION = "stdlib-seven-formulas-v4-exact-corr"
# This is the reviewed oracle scope, deliberately not an alias of the live
# production version constant. A future operator release needs oracle review.
ORACLE_OPERATOR_SEMANTICS = "local-panel-v3-scaled-corr-exact-boundaries"
ABS_TOLERANCE = 1e-10
REL_TOLERANCE = 1e-8
MAX_BYTES = 64 * 1024 * 1024
MAX_ORACLE_CELLS = 100_000
MAX_CORRELATION_OPERATIONS = 1_000_000
MAX_CORRELATION_WINDOW = 252
ALIASES = {"correlation": "ts_corr", "delta": "ts_delta", "stddev": "ts_std"}
FORMULAS = {
    "alpha006": "(-1 * ts_corr(open, volume, 10))",
    "alpha101": "((close - open) / ((high - low) + .001))",
    "alpha012": "(sign(ts_delta(volume, 1)) * (-1 * ts_delta(close, 1)))",
    # The PDF uses exponent one; this local form is an explicitly attributed
    # mathematical simplification, never silently described as the raw formula.
    "alpha033_modified": "rank(-1 * (1 - open / close))",
    **TEMPORAL_FORMULAS,
}


class OracleUnsupported(NotImplementedError):
    def __init__(self, code, message):
        super().__init__(message)
        self.code = code

SCOPE = ("Exact quote/page provenance plus an independent synthetic-data oracle for "
         "seven registered formulas: Alpha006, Alpha101, Alpha012, modified Alpha033, and "
         "Alpha101 mean5/std5/delay1 modifications; complete factor history through "
         "validation end, next-open labels, daily ranks/weights/contributions and "
         "aggregate validation metrics. Economic fidelity requires human review; "
         "this is not a profitability or execution-quality assessment.")


def _canonical(expression):
    if not isinstance(expression, str) or len(expression) > 2048:
        raise ValueError("Expression must be a bounded string")
    try:
        tree = ast.parse(expression.strip(), mode="eval")
    except (SyntaxError, ValueError, RecursionError):
        # Invalid syntax is still a meaningful frozen negative regression case.
        return {"invalid_syntax": expression.strip()}
    pending = [(tree, 0)]
    count = 0
    while pending:
        node, depth = pending.pop()
        count += 1
        if count > 256 or depth > 32:
            raise ValueError("Expression exceeds regression contract complexity limits")
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Name):
            node.func.id = ALIASES.get(node.func.id, node.func.id)
        pending.extend((child, depth + 1) for child in ast.iter_child_nodes(node))
    return ast.dump(tree, annotate_fields=True, include_attributes=False)


def _one(items, item_id, label):
    matches = [item for item in items if isinstance(item, dict) and item.get("id") == item_id]
    if len(matches) != 1:
        raise ValueError(f"Exactly one {label} {item_id!r} is required")
    return matches[0]


def _subject(task, candidate_id):
    candidate = _one(task.get("candidates", []), candidate_id, "candidate")
    hypothesis = _one(task.get("hypotheses", []), candidate["hypothesis_id"], "hypothesis")
    references = hypothesis.get("evidence_ids", [])
    if not references or len(set(references)) != len(references):
        raise ValueError("Hypothesis requires unique, nonempty evidence references")
    evidence = [_one(task.get("evidence", []), key, "evidence") for key in references]
    return candidate, hypothesis, evidence


def freeze_contract(task: dict, candidate_id: str, paper_sha256: str,
                    dataset_sha256: str, metadata: dict) -> dict:
    """Freeze the candidate's scientific meaning, not display or run controls.

    Operator alias repair/whitespace is comparable. Changed fields, windows,
    hypothesis, evidence, dataset, operator semantics or evaluation boundaries
    require a newly reviewed case. Budget/mode changes may legitimately repair
    a failed run and therefore do not change this scientific contract.
    """
    candidate, hypothesis, evidence = _subject(task, candidate_id)
    candidate = deepcopy(candidate)
    candidate["expression"] = _canonical(candidate["expression"])
    evaluation = deepcopy(task["evaluation"])
    evaluation.setdefault("min_assets", 3)
    return deepcopy({
        "contract_version": CONTRACT_VERSION,
        "candidate": candidate,
        "hypothesis": hypothesis,
        "evidence": sorted(evidence, key=lambda item: item["id"]),
        "paper_sha256": paper_sha256,
        "dataset_sha256": dataset_sha256,
        "data_semantics": metadata,
        "evaluation": evaluation,
        "operator_semantics": OPERATOR_SEMANTICS_VERSION,
        "execution_semantics": EXECUTION,
    })


def compare_contract(expected: dict, current: dict) -> list[str]:
    """Return changed JSON paths; retain list order where it can affect meaning."""
    changed = []

    def visit(left, right, path):
        if type(left) is not type(right):
            changed.append(path or "$")
        elif isinstance(left, dict):
            for key in sorted(set(left) | set(right)):
                child = f"{path}.{key}" if path else key
                if key not in left or key not in right:
                    changed.append(child)
                else:
                    visit(left[key], right[key], child)
        elif isinstance(left, list):
            if len(left) != len(right):
                changed.append(path)
            else:
                for index, (a, b) in enumerate(zip(left, right)):
                    visit(a, b, f"{path}[{index}]")
        elif left != right:
            changed.append(path)

    visit(expected, current, "")
    return changed


def _file(root, relative):
    relative = Path(relative)
    if relative.is_absolute() or ".." in relative.parts:
        raise ValueError("Artifact path must remain inside its output directory")
    path = root / relative
    if any(parent.is_symlink() for parent in (path, *path.parents) if parent != root.parent):
        raise ValueError("Symlink artifacts are not supported")
    if not path.is_file() or not path.resolve().is_relative_to(root.resolve()):
        raise ValueError(f"Missing recorded artifact: {relative}")
    if path.stat().st_size > MAX_BYTES:
        raise ValueError(f"Recorded artifact exceeds the {MAX_BYTES} byte bound")
    return path


def _json(root, relative):
    return read_json(_file(root, relative))


def recorded_semantics(output_dir: Path) -> dict:
    """Read literal semantic declarations from verified immutable source files.

    Never import or execute historical code. Missing, duplicated, computed or
    dynamically assigned declarations cannot establish a comparable contract.
    The caller must verify the source snapshot hashes before using these values.
    """
    root = Path(output_dir).resolve()
    result = {}
    for relative, variable, key, expected_type in (
        ("source/paper_alpha/expressions.py", "OPERATOR_SEMANTICS_VERSION", "operator_semantics", str),
        ("source/paper_alpha/evaluation.py", "EXECUTION", "execution_semantics", dict),
    ):
        source = _file(root, relative)
        if source.stat().st_size > 1024 * 1024:
            raise ValueError("Historical semantic source exceeds its 1 MiB limit")
        try:
            tree = ast.parse(source.read_text(encoding="utf-8"))
            assignments = [node.value for node in tree.body if isinstance(node, ast.Assign)
                           and any(isinstance(target, ast.Name) and target.id == variable
                                   for target in node.targets)]
            if len(assignments) != 1:
                raise ValueError(f"Historical {variable} requires one literal assignment")
            value = ast.literal_eval(assignments[0])
            if not isinstance(value, expected_type) or not value:
                raise ValueError(f"Historical {variable} has an invalid type or is empty")
            if expected_type is dict and not all(isinstance(k, str) and isinstance(v, (str, int, float))
                                                and not isinstance(v, bool)
                                                and (not isinstance(v, float) or math.isfinite(v))
                                                for k, v in value.items()):
                raise ValueError("Historical execution semantics must be a flat literal mapping")
            result[key] = value
        except (SyntaxError, TypeError, RecursionError) as exc:
            raise ValueError(f"Historical {variable} is not a safe literal declaration") from exc
    return result


def _normal(text):
    return re.sub(r"\s+", " ", unicodedata.normalize("NFKC", text)).strip()


def _evidence_check(root, task, candidate_id, state):
    from pypdf import PdfReader

    _, _, evidence = _subject(task, candidate_id)
    paper = _json(root, "inputs/paper.json")
    pdf = _file(root, "inputs/paper.pdf")
    checksum = hashlib.sha256(pdf.read_bytes()).hexdigest()
    if paper.get("document_sha256") != checksum:
        raise ValueError("Recorded PDF digest differs from paper metadata")
    reader = PdfReader(pdf)
    if len(reader.pages) > 500:
        raise ValueError("Independent citation check exceeds its 500-page limit")
    pages = {index + 1: _normal(page.extract_text() or "") for index, page in enumerate(reader.pages)}
    supplied = {item["page"]: _normal(item["text"]) for item in paper["pages"]}
    if len(supplied) != len(paper["pages"]) or supplied != pages:
        raise ValueError("Paper page text does not match fresh PDF extraction")
    outputs = []
    for relative in sorted(state.get("tool_artifacts", {})):
        if re.fullmatch(r"tools/\d+/request\.json", relative):
            request = _json(root, relative)
            if request.get("action") == "preflight":
                response = _json(root, str(Path(relative).with_name("response.json")))
                if response.get("ok") is True:
                    outputs.append(response["value"])
    if not outputs or outputs[-1] != state.get("preflight"):
        raise ValueError("Saved preflight evidence lacks a matching successful tool response")
    verified = outputs[-1].get("evidence", [])
    ids = []
    for item in evidence:
        quote = _normal(item["quote"])
        page = pages.get(item["page"], "")
        if not quote or quote not in page:
            raise ValueError(f"Evidence {item['id']}: quote is absent from its PDF page")
        start = page.index(quote)
        recorded = _one(verified, item["id"], "verified evidence")
        expected = {**item, "paper_id": paper["id"], "document_sha256": checksum,
                    "normalized_start": start, "normalized_end": start + len(quote),
                    "citation_verified": True, "semantic_fidelity": "requires_human_review"}
        if recorded != expected:
            raise ValueError(f"Evidence {item['id']}: recorded quote/page/offset/provenance differs")
        ids.append(item["id"])
    return {"name": "evidence_provenance", "outcome": "passed", "reason_code": "evidence_verified",
            "reason": "Referenced quotes, pages, normalized offsets and PDF digest match fresh extraction and the recorded preflight tool response.",
            "evidence_ids": ids, "semantic_fidelity": "requires_human_review"}


def _supported(expression):
    canonical = _canonical(expression)
    for name, formula in FORMULAS.items():
        if canonical == _canonical(formula):
            return name
    return None


def oracle_support(expression: str) -> dict:
    """Declare coverage without running the production expression interpreter."""
    formula = _supported(expression)
    return {"supported": formula is not None, "formula": formula, "oracle_version": ORACLE_VERSION,
            "reason_code": "supported_formula" if formula else "oracle_unsupported_formula",
            "operator_semantics": ORACLE_OPERATOR_SEMANTICS,
            "max_market_cells": MAX_ORACLE_CELLS,
            "absolute_tolerance": ABS_TOLERANCE, "relative_tolerance": REL_TOLERANCE}


def _market(root, metadata):
    dates, assets = metadata["calendar_dates"], metadata["universe"]
    if (not isinstance(dates, list) or not isinstance(assets, list) or
            len(dates) * len(assets) > MAX_ORACLE_CELLS):
        raise OracleUnsupported("oracle_resource_bound", f"Independent oracle is bounded to {MAX_ORACLE_CELLS} market cells")
    if len(set(dates)) != len(dates) or dates != sorted(dates) or len(set(assets)) != len(assets):
        raise ValueError("Independent oracle requires a unique ordered calendar and unique universe")
    path = _file(root, "inputs/market.csv")
    if hashlib.sha256(path.read_bytes()).hexdigest() != metadata["market_sha256"]:
        raise ValueError("Recorded market CSV digest differs from metadata")
    rows = {}
    with path.open(newline="") as stream:
        reader = csv.DictReader(stream)
        if reader.fieldnames != ["date", "asset", "open", "high", "low", "close", "volume"]:
            raise ValueError("Unexpected recorded market CSV columns")
        for item in reader:
            key = (item.pop("date"), item.pop("asset"))
            if key in rows:
                raise ValueError("Duplicate market observation")
            values = {key: float(value) for key, value in item.items()}
            if not all(math.isfinite(value) and value > 0 for value in values.values()):
                raise ValueError("Market observations must be finite and positive")
            rows[key] = values
            if len(rows) > MAX_ORACLE_CELLS:
                raise OracleUnsupported("oracle_resource_bound", "Independent oracle market cell bound exceeded")
    if set(rows) != {(date, asset) for date in dates for asset in assets}:
        raise ValueError("Recorded CSV does not match the independently declared grid")
    return dates, assets, rows


def _factors(formula, dates, assets, rows, last):
    if formula in TEMPORAL_FORMULAS:
        return temporal_factors(formula, dates, assets, rows, last)
    if formula == "alpha006" and (last + 1) * len(assets) * 10 > MAX_CORRELATION_OPERATIONS:
        raise OracleUnsupported("oracle_resource_bound", "Independent correlation oracle operation bound exceeded")
    result = {}
    for i, date in enumerate(dates[:last + 1]):
        if formula == "alpha033_modified":
            raw = [-1 * (1 - rows[date, asset]["open"] / rows[date, asset]["close"]) for asset in assets]
            for asset, rank in zip(assets, _ranks(raw)):
                result[date, asset] = rank / len(assets)
            continue
        for asset in assets:
            row = rows[date, asset]
            value = None
            if formula == "alpha101":
                value = (row["close"] - row["open"]) / (row["high"] - row["low"] + .001)
            elif formula == "alpha012" and i >= 1:
                previous = rows[dates[i - 1], asset]
                change = row["volume"] - previous["volume"]
                value = float((change > 0) - (change < 0)) * -(row["close"] - previous["close"])
            elif formula == "alpha006" and i >= 9:
                window = [rows[day, asset] for day in dates[i - 9:i + 1]]
                left, right = ([item[field] for item in window] for field in ("open", "volume"))
                correlation = _reference_correlation(left, right)
                value = None if correlation is None else -correlation
            if value is not None and not math.isfinite(value):
                raise ValueError("Independent factor arithmetic produced a nonfinite value")
            result[date, asset] = value
    return result


def _reference_correlation(left, right):
    """Independent centered-rational Pearson, without production helpers.

    Fraction.from_float retains the exact represented binary64 observations.
    Unlike production's integer raw moments, this reference subtracts rational
    means and forms rational centered products. Decimal precision is 100.
    """
    if len(left) != len(right) or len(left) < 2:
        return None
    if len(left) > MAX_CORRELATION_WINDOW:
        raise OracleUnsupported("oracle_resource_bound", "Independent correlation window exceeds 252 observations")
    if not all(math.isfinite(value) for value in (*left, *right)):
        return None
    x, y = ([Fraction.from_float(float(value)) for value in axis] for axis in (left, right))
    mx, my = sum(x) / len(x), sum(y) / len(y)
    x, y = [value - mx for value in x], [value - my for value in y]
    vx, vy = sum(value * value for value in x), sum(value * value for value in y)
    if not vx or not vy:
        return None
    covariance = sum(a * b for a, b in zip(x, y))
    product = vx * vy
    if covariance * covariance == product:
        return 1.0 if covariance > 0 else -1.0
    with localcontext(Context(prec=100, rounding=ROUND_HALF_EVEN)):
        numerator = Decimal(covariance.numerator) / Decimal(covariance.denominator)
        squared_denominator = Decimal(product.numerator) / Decimal(product.denominator)
        return float(numerator / squared_denominator.sqrt())


def _ranks(values):
    ordered = sorted(range(len(values)), key=lambda index: values[index])
    ranked = [0.0] * len(values)
    start = 0
    while start < len(values):
        stop = start + 1
        while stop < len(values) and values[ordered[stop]] == values[ordered[start]]:
            stop += 1
        average = (start + 1 + stop) / 2
        for position in ordered[start:stop]:
            ranked[position] = average
        start = stop
    return ranked


def _reference_evaluation(dates, assets, market, factor, config):
    bounds = config["splits"]["validation"]
    first, last = dates.index(bounds["start"]), dates.index(bounds["end"])
    if last - first < 2:
        raise ValueError("Validation requires at least three recorded sessions")
    min_assets = config.get("min_assets", 3)
    daily, ics, returns = [], [], []
    finite_count = 0
    for index in range(first, last + 1):
        date = dates[index]
        record = {"signal_date": date, "entry_date": None, "exit_date": None,
                  "status": "purged", "reason": "label_would_cross_split_boundary",
                  "available_assets": 0, "missing_assets": [], "rank_ic": None,
                  "rank_ic_state": "not_evaluated", "gross_return": None, "assets": []}
        daily.append(record)
        if index + 2 > last:
            continue
        selected = [asset for asset in assets if factor[date, asset] is not None]
        finite_count += len(selected)
        record.update(entry_date=dates[index + 1], exit_date=dates[index + 2],
                      available_assets=len(selected),
                      missing_assets=[asset for asset in assets if asset not in selected])
        if len(selected) < min_assets:
            record.update(status="skipped", reason="insufficient_finite_assets")
            continue
        ranks = _ranks([factor[date, asset] for asset in selected])
        center = (len(selected) + 1) / 2
        centered = [rank - center for rank in ranks]
        gross = math.fsum(abs(value) for value in centered)
        if gross == 0:
            record.update(status="skipped", reason="constant_factor")
            continue
        weights = [value / gross for value in centered]
        labels = [market[dates[index + 2], asset]["open"] / market[dates[index + 1], asset]["open"] - 1
                  for asset in selected]
        return_ranks = _ranks(labels)
        if len(set(return_ranks)) > 1:
            ic = statistics.correlation(ranks, return_ranks)
            record.update(rank_ic=ic, rank_ic_state="defined")
            ics.append(ic)
        else:
            record["rank_ic_state"] = "constant_forward_returns"
        contributions = [weight * label for weight, label in zip(weights, labels)]
        gross_return = math.fsum(contributions)
        returns.append(gross_return)
        record.update(status="evaluated", reason=None, gross_return=gross_return,
                      assets=[{"asset": asset, "signal": factor[date, asset], "weight": weight,
                               "entry_open": market[dates[index + 1], asset]["open"],
                               "exit_open": market[dates[index + 2], asset]["open"],
                               "forward_return": label, "gross_contribution": contribution}
                              for asset, weight, label, contribution in
                              zip(selected, weights, labels, contributions)])
    eligible = last - first - 1
    metrics = {"mean_rank_ic": statistics.fmean(ics) if ics else None,
               "rank_ic_days": len(ics),
               "mean_gross_return": statistics.fmean(returns) if returns else None,
               "sum_gross_return": math.fsum(returns) if returns else None,
               "evaluated_days": len(returns), "eligible_days": eligible,
               "skipped_days": eligible - len(returns), "purged_days": 2,
               "finite_factor_observations": finite_count,
               "possible_factor_observations": eligible * len(assets),
               "factor_coverage": finite_count / (eligible * len(assets))}
    return {"status": "evaluated" if returns else "not_evaluable", "split": "validation",
            "config": {**deepcopy(config), "min_assets": min_assets},
            "daily": daily, "metrics": metrics}


def _differences(expected, actual, path="", limit=20):
    mismatches = []

    def visit(left, right, location):
        if len(mismatches) >= limit:
            return
        if isinstance(left, bool) or left is None or isinstance(left, str):
            equal = type(left) is type(right) and left == right
        elif type(left) is int:
            # Session/asset counts and metadata identifiers are exact, not
            # approximate measurements that should inherit float tolerances.
            equal = type(right) is int and left == right
        elif isinstance(left, float):
            equal = (type(right) in (int, float) and math.isfinite(right) and
                     math.isclose(left, right, rel_tol=REL_TOLERANCE, abs_tol=ABS_TOLERANCE))
        elif isinstance(left, dict):
            if not isinstance(right, dict) or set(left) != set(right):
                mismatches.append(location + ": object keys differ")
                return
            for key in left:
                visit(left[key], right[key], f"{location}.{key}" if location else key)
            return
        elif isinstance(left, list):
            if not isinstance(right, list) or len(left) != len(right):
                mismatches.append(location + ": list length differs")
                return
            for index, (a, b) in enumerate(zip(left, right)):
                visit(a, b, f"{location}[{index}]")
            return
        else:
            equal = left == right
        if not equal:
            mismatches.append(location + ": observed value differs from independent reference")

    visit(expected, actual, path)
    return mismatches


def _numeric_checks(root, task, candidate_id, state):
    candidate, _, _ = _subject(task, candidate_id)
    saved = _one(state.get("candidates", []), candidate_id, "saved candidate")
    result = saved.get("result")
    if not isinstance(result, dict):
        raise OracleUnsupported("no_numerical_result", "Candidate has no numerical result; status checks do not establish numerical correctness")
    if _canonical(result.get("expression")) != _canonical(candidate["expression"]):
        raise ValueError("Executed expression differs from the frozen candidate beyond documented aliases")
    formula = _supported(result["expression"])
    if formula is None:
        raise OracleUnsupported("oracle_unsupported_formula", "Independent numerical oracle supports only its seven explicitly registered formulas")
    semantics = recorded_semantics(root)
    if semantics["operator_semantics"] != ORACLE_OPERATOR_SEMANTICS:
        raise OracleUnsupported("oracle_unsupported_semantics", "Independent oracle has not been reviewed for these historical operator semantics")
    for key in ("expression_diagnostics", "factor_diagnostics"):
        if result.get(key, {}).get("operator_semantics_version") != semantics["operator_semantics"]:
            raise ValueError("Reported operator semantics differ from the recorded source declaration")
    if result.get("execution") != semantics["execution_semantics"]:
        raise ValueError("Reported execution semantics differ from the recorded source declaration")
    metadata = _json(root, "inputs/metadata.json")
    if metadata.get("data_kind") != "synthetic":
        raise OracleUnsupported("oracle_unsupported_data", "Independent oracle currently supports synthetic data only")
    dates, assets, market = _market(root, metadata)
    config = task["evaluation"]
    last = dates.index(config["splits"]["validation"]["end"])
    factors = _factors(formula, dates, assets, market, last)
    paths = [path for path in saved.get("artifacts", {}) if path.endswith("/factor.csv")]
    result_paths = [path for path in saved.get("artifacts", {}) if path.endswith("/result.json")]
    if len(paths) != 1 or len(result_paths) != 1 or Path(paths[0]).parent != Path(result_paths[0]).parent:
        raise ValueError("Expected one bound factor/result artifact pair")
    if _json(root, result_paths[0]) != result:
        raise ValueError("Saved candidate result differs from its result.json artifact")
    observed = {}
    with _file(root, paths[0]).open(newline="") as stream:
        reader = csv.DictReader(stream)
        if reader.fieldnames != ["date", *assets]:
            raise ValueError("Factor CSV columns differ from the declared universe")
        seen_dates = []
        for row in reader:
            date = row.pop("date")
            seen_dates.append(date)
            if len(seen_dates) > len(dates):
                raise ValueError("Factor CSV exceeds the recorded calendar")
            for asset in assets:
                raw = row[asset]
                observed[date, asset] = None if raw == "" else float(raw)
    if seen_dates != dates[:last + 1]:
        raise ValueError("Factor CSV must contain exactly the history through validation end and no test signals")
    factor_errors = []
    for key in factors:
        factor_errors.extend(_differences(factors[key], observed.get(key), f"factor[{key[0]},{key[1]}]"))
        if len(factor_errors) >= 20:
            break
    reference = _reference_evaluation(dates, assets, market, factors, config)
    daily_errors = _differences(reference["daily"], result.get("daily"), "daily")
    metric_errors = _differences(reference["metrics"], result.get("metrics"), "metrics")
    for key in ("status", "split", "config"):
        metric_errors.extend(_differences(reference[key], result.get(key), key))
    metric_errors.extend(_differences(metadata, result.get("market_metadata"), "market_metadata"))
    details = {"oracle_version": ORACLE_VERSION, "formula": formula,
               "absolute_tolerance": ABS_TOLERANCE, "relative_tolerance": REL_TOLERANCE}
    return [{"name": name, "outcome": "failed" if errors else "passed",
             "reason": ("Independent reference mismatch" if errors else reason),
             "reason_code": "numeric_mismatch" if errors else "numeric_verified",
             "mismatches": errors[:20], **details}
            for name, errors, reason in (
                ("factor_values", factor_errors, f"All {len(factors)} recorded factor cells agree, including warm-up gaps; no held-out test signals exist."),
                ("daily_numerics", daily_errors, "All validation dates, purge/skip states, signal values, forward returns, ranks, portfolio weights and contributions agree."),
                ("aggregate_metrics", metric_errors, "Validation metrics, valid-day denominators, configuration and data metadata agree."))]


def verify_candidate(output_dir: Path, candidate_id: str, task: dict) -> dict:
    """Check a verified run's recorded artifacts; never edit or trust its metrics.

    The caller must first perform the normal run-integrity/manifest check. This
    oracle adds independent correctness evidence rather than replacing hashing.
    Errors are JSON diagnostics; unsupported scope is distinct from failure.
    """
    checks = []
    root = Path(output_dir).resolve()
    try:
        state = _json(root, "state.json")
        recorded_task = _json(root, "inputs/task.json")
        if _subject(recorded_task, candidate_id) != _subject(task, candidate_id):
            raise ValueError("Requested candidate/hypothesis/evidence differ from recorded input")
    except (ValueError, KeyError, TypeError, OSError, AttributeError, IndexError) as exc:
        return {"outcome": "failed", "scope": SCOPE, "oracle_version": ORACLE_VERSION,
                "checks": [{"name": "recorded_inputs", "outcome": "failed", "reason_code": "recorded_inputs_invalid", "reason": str(exc)}]}
    try:
        checks.append(_evidence_check(root, task, candidate_id, state))
    except Exception as exc:
        checks.append({"name": "evidence_provenance", "outcome": "failed", "reason_code": "evidence_invalid", "reason": str(exc),
                       "semantic_fidelity": "requires_human_review"})
    try:
        checks.extend(_numeric_checks(root, task, candidate_id, state))
    except NotImplementedError as exc:
        checks.append({"name": "numerical_oracle", "outcome": "not_comparable",
                       "reason_code": getattr(exc, "code", "oracle_unsupported"), "reason": str(exc)})
    except (ValueError, KeyError, TypeError, OSError, ArithmeticError, AttributeError, IndexError, csv.Error) as exc:
        checks.append({"name": "numerical_oracle", "outcome": "failed", "reason_code": "numeric_check_error", "reason": str(exc)})
    outcome = ("failed" if any(item["outcome"] == "failed" for item in checks) else
               "not_comparable" if any(item["outcome"] == "not_comparable" for item in checks) else "passed")
    return {"outcome": outcome, "checks": checks, "scope": SCOPE, "oracle_version": ORACLE_VERSION}
