"""A small, non-executable expression language for dates-by-instruments panels.

Operator semantics v3: trailing windows include the current row; ``rank`` is
cross-sectional percentile rank with average ties; ``ts_std`` uses ddof=1.
``delay`` shifts backwards by nonnegative rows. ``ts_corr`` uses complete paired
windows and centered, scale-normalized binary64 observations. Near zero or an
endpoint, or when centering overflows, exact integer moments and 80-digit
Decimal arithmetic replace the floating path. Zero variance yields NaN;
exact affine relationships yield exactly +1/-1, without endpoint snapping.
No tolerance or rounding is applied to ranks.
Infinite intermediate values become NaN before the parent operator runs, and
comparisons propagate missing inputs instead of silently turning them false.
"""

from __future__ import annotations

import ast
from decimal import Context, Decimal, ROUND_HALF_EVEN, localcontext
import math
import operator
from collections.abc import Mapping

import numpy as np
import pandas as pd
from pandas.api.types import is_bool_dtype, is_numeric_dtype

from .vendor import operators as vendor


OPERATOR_SEMANTICS_VERSION = "local-panel-v3-scaled-corr-exact-boundaries"
MAX_EXPRESSION_LENGTH = 2048
MAX_AST_NODES = 256
MAX_AST_DEPTH = 32
MAX_WINDOW = 252
MAX_CONSTANT_MAGNITUDE = 1_000_000_000

_FUNCTION_ARITY = {
    "ts_corr": 3,
    "rank": 1,
    "delay": 2,
    "ts_delta": 2,
    "ts_mean": 2,
    "ts_std": 2,
    "ts_min": 2,
    "ts_max": 2,
    "ts_sum": 2,
    "log": 1,
    "abs": 1,
    "sign": 1,
}
_WINDOW_FUNCTIONS = {name for name, arity in _FUNCTION_ARITY.items() if arity > 1}
_BINARY = {
    ast.Add: operator.add,
    ast.Sub: operator.sub,
    ast.Mult: operator.mul,
    ast.Div: operator.truediv,
}
_COMPARE = {
    ast.Lt: operator.lt,
    ast.LtE: operator.le,
    ast.Gt: operator.gt,
    ast.GtE: operator.ge,
    ast.Eq: operator.eq,
    ast.NotEq: operator.ne,
}
_ALIASES = {"correlation": "ts_corr", "delta": "ts_delta", "stddev": "ts_std"}


class ExpressionError(ValueError):
    """Machine-readable validation or execution failure."""

    def __init__(self, message: str, code: str = "unsafe_expression") -> None:
        super().__init__(message)
        self.code = code


def _parse(expression: str) -> ast.Expression:
    if not isinstance(expression, str) or not expression.strip():
        raise ExpressionError("Expression must be a nonempty string.")
    if len(expression) > MAX_EXPRESSION_LENGTH:
        raise ExpressionError(f"Expression exceeds {MAX_EXPRESSION_LENGTH} characters.")
    try:
        tree = ast.parse(expression.strip(), mode="eval")
    except (SyntaxError, ValueError, RecursionError) as exc:
        raise ExpressionError("Invalid expression syntax.") from exc
    stack = [(tree, 0)]
    count = 0
    while stack:
        node, depth = stack.pop()
        count += 1
        if count > MAX_AST_NODES or depth > MAX_AST_DEPTH:
            raise ExpressionError("Expression exceeds the syntax complexity limit.")
        stack.extend((child, depth + 1) for child in ast.iter_child_nodes(node))
    return tree


def validate_expression(expression: str, available_fields: set[str]) -> dict:
    """Validate every AST node and return deterministic, JSON-ready metadata.

    Windows must be integer literals, rather than expressions or coercible
    floats. Panel operators require panel operands; constants can be used in
    arithmetic and comparisons. No Python objects or user functions are run.
    """
    tree = _parse(expression)
    used_fields: set[str] = set()
    used_operators: set[str] = set()

    def visit(node: ast.AST) -> str:
        if isinstance(node, ast.Name):
            if node.id not in available_fields:
                raise ExpressionError(f"Unavailable field: {node.id}", "unknown_field")
            used_fields.add(node.id)
            return "panel"
        if isinstance(node, ast.Constant):
            if type(node.value) not in (int, float):
                raise ExpressionError("Only finite numeric constants are allowed.")
            if not math.isfinite(node.value) or abs(node.value) > MAX_CONSTANT_MAGNITUDE:
                raise ExpressionError("Numeric constant is nonfinite or too large.")
            return "scalar"
        if isinstance(node, ast.UnaryOp) and isinstance(node.op, (ast.UAdd, ast.USub)):
            return visit(node.operand)
        if isinstance(node, ast.BinOp) and type(node.op) in _BINARY:
            kinds = [visit(node.left), visit(node.right)]
            return "panel" if "panel" in kinds else "scalar"
        if isinstance(node, ast.Compare):
            if len(node.ops) != 1 or type(node.ops[0]) not in _COMPARE:
                raise ExpressionError("Only a single numeric comparison is allowed.")
            kinds = [visit(node.left), visit(node.comparators[0])]
            return "panel" if "panel" in kinds else "scalar"
        if isinstance(node, ast.Call):
            if not isinstance(node.func, ast.Name) or node.keywords:
                raise ExpressionError("Calls must use a named operator and positional arguments.")
            name = node.func.id
            if name not in _FUNCTION_ARITY:
                raise ExpressionError(f"Unknown operator: {name}", "unknown_operator")
            if len(node.args) != _FUNCTION_ARITY[name]:
                raise ExpressionError(
                    f"{name} requires {_FUNCTION_ARITY[name]} positional arguments.",
                    "invalid_arguments",
                )
            used_operators.add(name)
            panel_args = node.args
            if name in _WINDOW_FUNCTIONS:
                window = node.args[-1]
                minimum = 0 if name == "delay" else 1
                if (
                    not isinstance(window, ast.Constant)
                    or type(window.value) is not int
                    or not minimum <= window.value <= MAX_WINDOW
                ):
                    raise ExpressionError(
                        f"{name} window must be an integer literal in [{minimum}, {MAX_WINDOW}].",
                        "invalid_window",
                    )
                panel_args = node.args[:-1]
            for arg in panel_args:
                if visit(arg) != "panel":
                    raise ExpressionError(f"{name} requires panel operands.", "invalid_arguments")
            return "panel"
        raise ExpressionError(f"Unsupported expression syntax: {type(node).__name__}")

    if visit(tree.body) != "panel":
        raise ExpressionError("Expression must produce a panel using an available field.")
    return {
        "used_fields": sorted(used_fields),
        "operators": sorted(used_operators),
        "canonical_expression": ast.unparse(tree.body),
    }


def _integer_observations(values):
    """Express binary64 inputs as integers sharing a power-of-two denominator.

    Each axis may use its own denominator: positive scale cancels from Pearson.
    Python integer arithmetic cannot overflow or lose low-order input bits.
    """
    ratios = [float(value).as_integer_ratio() for value in values]
    exponents = [denominator.bit_length() - 1 for _, denominator in ratios]
    shared = max(exponents)
    return [numerator << (shared - exponent)
            for (numerator, _), exponent in zip(ratios, exponents)]


def _pearson_binary64(left, right):
    """Compute exact moments, then a high-precision approximation of Pearson.

    The exactness claim covers represented observations and integer moments,
    not arbitrary real inputs or correctly rounded irrational square roots.
    Endpoint equality is algebraic; near correlation is never epsilon-snapped.
    """
    x, y = _integer_observations(left), _integer_observations(right)
    count = len(x)
    sx, sy = sum(x), sum(y)
    vx = count * sum(value * value for value in x) - sx * sx
    vy = count * sum(value * value for value in y) - sy * sy
    if vx == 0 or vy == 0:
        return np.nan
    covariance = count * sum(a * b for a, b in zip(x, y)) - sx * sy
    if covariance == 0:
        return 0.0
    product = vx * vy
    if covariance * covariance == product:
        return 1.0 if covariance > 0 else -1.0
    with localcontext(Context(prec=80, rounding=ROUND_HALF_EVEN)):
        return float(Decimal(covariance) / Decimal(product).sqrt())


def _stable_correlation(left: pd.DataFrame, right: pd.DataFrame, window: int) -> pd.DataFrame:
    """Complete-window Pearson with explicit binary64 numerical semantics."""
    output = np.full(left.shape, np.nan, dtype=float)
    if window > len(left):
        return pd.DataFrame(output, index=left.index, columns=left.columns)
    for column in range(left.shape[1]):
        x = left.iloc[:, column].to_numpy(dtype=float, na_value=np.nan)
        y = right.iloc[:, column].to_numpy(dtype=float, na_value=np.nan)
        x_windows = np.lib.stride_tricks.sliding_window_view(x, window)
        y_windows = np.lib.stride_tricks.sliding_window_view(y, window)
        for row, (xw, yw) in enumerate(zip(x_windows, y_windows), start=window - 1):
            if not np.isfinite(xw).all() or not np.isfinite(yw).all():
                continue
            if np.all(xw == xw[0]) or np.all(yw == yw[0]):
                continue
            # Equality here is exact equality of the represented inputs, not
            # an approximate factor threshold. Missing/constant windows have
            # already been excluded.
            if np.array_equal(xw, yw):
                output[row, column] = 1.0
                continue
            if np.array_equal(xw, -yw):
                output[row, column] = -1.0
                continue
            xc, yc = xw - xw[0], yw - yw[0]
            if not np.isfinite(xc).all() or not np.isfinite(yc).all():
                output[row, column] = _pearson_binary64(xw, yw)
                continue
            # Translation removes a large common offset; division before
            # squaring prevents both large- and small-scale norm failures.
            xc, yc = xc / np.max(np.abs(xc)), yc / np.max(np.abs(yc))
            xc, yc = xc - xc.mean(), yc - yc.mean()
            value = float(np.dot(xc, yc) / np.sqrt(np.dot(xc, xc) * np.dot(yc, yc)))
            # This threshold ONLY selects the more exact algorithm. It never
            # rounds, ties or snaps a computed coefficient to 0 or +/-1.
            boundary = 32 * np.finfo(float).eps
            if not math.isfinite(value) or abs(value) <= boundary or abs(value) >= 1 - boundary:
                value = _pearson_binary64(xw, yw)
            output[row, column] = value
    return pd.DataFrame(output, index=left.index, columns=left.columns)


def _check_panels(panels: Mapping[str, pd.DataFrame]) -> pd.DataFrame:
    if not isinstance(panels, Mapping) or not panels:
        raise ExpressionError("At least one data panel is required.", "invalid_data")
    reference = None
    for name, panel in panels.items():
        if not isinstance(name, str) or not isinstance(panel, pd.DataFrame) or panel.empty:
            raise ExpressionError("Fields must map to nonempty DataFrames.", "invalid_data")
        if (
            not panel.index.is_unique
            or not panel.columns.is_unique
            or not panel.index.is_monotonic_increasing
        ):
            raise ExpressionError("Panel dates must be sorted and both axes unique.", "panel_alignment")
        if reference is not None and (
            not panel.index.equals(reference.index) or not panel.columns.equals(reference.columns)
        ):
            raise ExpressionError("Every panel must have exactly the same ordered axes.", "panel_alignment")
        if any(not is_numeric_dtype(dtype) or is_bool_dtype(dtype) for dtype in panel.dtypes):
            raise ExpressionError(f"Field {name} contains nonnumeric data.", "invalid_data")
        if np.isinf(panel.to_numpy(dtype=float, na_value=np.nan)).any():
            raise ExpressionError(f"Field {name} contains infinite input values.", "invalid_data")
        reference = panel if reference is None else reference
    return reference


def compute_expression(expression: str, panels: dict[str, pd.DataFrame]) -> pd.DataFrame:
    """Interpret the validated expression; never evaluate generated Python code.

    Inputs are checked before arithmetic to forbid implicit pandas alignment.
    Infinities at every AST operation become NaN before subsequent operations.
    Final pre-conversion counts and cumulative intermediate-operation counts
    are retained in ``result.attrs['expression_diagnostics']``. Intermediate
    counts can count a cell more than once and are not unique observations.
    """
    reference = _check_panels(panels)
    validation = validate_expression(expression, set(panels))
    tree = _parse(validation["canonical_expression"])
    root_counts = None
    intermediate_infinite_count = 0
    intermediate_nan_count = 0

    def sanitize(value, node):
        nonlocal root_counts, intermediate_infinite_count, intermediate_nan_count
        if isinstance(value, pd.DataFrame):
            array = value.to_numpy(dtype=float, na_value=np.nan)
            infinite = int(np.isinf(array).sum())
            missing = int(np.isnan(array).sum())
            if infinite:
                array = array.copy()
                array[np.isinf(array)] = np.nan
                value = pd.DataFrame(array, index=value.index, columns=value.columns)
        else:
            infinite = int(math.isinf(value))
            missing = int(math.isnan(value))
            if infinite:
                value = np.nan
        if node is tree.body:
            root_counts = (infinite, missing)
        else:
            intermediate_infinite_count += infinite
            intermediate_nan_count += missing
        return value

    def calculate(node: ast.AST):
        if isinstance(node, ast.Name):
            return panels[node.id]
        if isinstance(node, ast.Constant):
            return node.value
        if isinstance(node, ast.UnaryOp):
            value = calculate(node.operand)
            return sanitize(-value if isinstance(node.op, ast.USub) else +value, node)
        if isinstance(node, ast.BinOp):
            left, right = calculate(node.left), calculate(node.right)
            # NumPy scalar division gives the same Inf/NaN semantics as panel
            # division, instead of a Python-only ZeroDivisionError.
            if isinstance(node.op, ast.Div) and not isinstance(left, pd.DataFrame) and not isinstance(right, pd.DataFrame):
                return sanitize(np.divide(left, right), node)
            return sanitize(_BINARY[type(node.op)](left, right), node)
        if isinstance(node, ast.Compare):
            left, right = calculate(node.left), calculate(node.comparators[0])
            result = _COMPARE[type(node.ops[0])](left, right)
            left_missing = left.isna() if isinstance(left, pd.DataFrame) else bool(pd.isna(left))
            right_missing = right.isna() if isinstance(right, pd.DataFrame) else bool(pd.isna(right))
            missing = left_missing | right_missing
            if isinstance(result, pd.DataFrame):
                if isinstance(missing, pd.DataFrame):
                    result = result.astype(float).mask(missing)
                elif missing:
                    result = result.astype(float) * np.nan
            elif missing:
                result = np.nan
            return sanitize(result, node)
        if isinstance(node, ast.Call):
            args = [calculate(arg) for arg in node.args]
            if node.func.id == "ts_corr":
                return sanitize(_stable_correlation(*args), node)
            return sanitize(getattr(vendor, node.func.id)(*args), node)
        raise AssertionError("Validated AST contains an unhandled node.")

    try:
        with np.errstate(all="ignore"):
            result = calculate(tree.body)
    except (ValueError, TypeError, ArithmeticError) as exc:
        raise ExpressionError(f"Expression computation failed: {exc}", "computation_error") from exc
    if not isinstance(result, pd.DataFrame) or (
        not result.index.equals(reference.index) or not result.columns.equals(reference.columns)
    ):
        raise ExpressionError("Expression output changed panel axes.", "panel_alignment")
    values = result.to_numpy(dtype=float, na_value=np.nan)
    infinite_count, nan_count = root_counts or (int(np.isinf(values).sum()), int(np.isnan(values).sum()))
    values = values.copy()
    values[np.isinf(values)] = np.nan
    result = pd.DataFrame(values, index=reference.index, columns=reference.columns)
    result.attrs["expression_diagnostics"] = {
        "infinite_count": infinite_count,
        "nan_count": nan_count,
        "nonfinite_count": infinite_count + nan_count,
        "intermediate_infinite_count": intermediate_infinite_count,
        "intermediate_nan_count": intermediate_nan_count,
        "intermediate_count_semantics": "Cumulative operation-output cells, not unique observations",
        "operator_semantics_version": OPERATOR_SEMANTICS_VERSION,
    }
    return result


def repair_expression(expression: str, error: Exception | dict) -> str | None:
    """Repair only documented operator aliases; never substitute data or windows.

    The caller must revalidate and record the proposed expression as a separate
    attempt. Unknown fields and invalid windows are deliberately not repairable.
    """
    code = error.get("code") if isinstance(error, dict) else getattr(error, "code", None)
    if code != "unknown_operator":
        return None
    try:
        tree = _parse(expression)
    except ExpressionError:
        return None
    changed = False

    class AliasRepair(ast.NodeTransformer):
        def visit_Call(self, node):
            nonlocal changed
            self.generic_visit(node)
            if isinstance(node.func, ast.Name) and node.func.id in _ALIASES:
                node.func.id = _ALIASES[node.func.id]
                changed = True
            return node

    AliasRepair().visit(tree)
    if not changed:
        return None
    proposed = ast.unparse(tree.body)
    # Recheck the complete grammar without pretending any data field is
    # available. Actual availability is checked by the workflow on retry.
    fields = {node.id for node in ast.walk(tree) if isinstance(node, ast.Name)}
    try:
        validate_expression(proposed, fields)
    except ExpressionError:
        return None
    return proposed
