"""Bounded LaTeX reading views for the local formula language.

This module does not compute, repair, canonicalize stored expressions, or infer
financial notation. The raw expression remains the execution/provenance source.
Unsupported input returns ``None`` so callers can retain a literal text view.
"""
from __future__ import annotations

import ast
import copy
import re

from .expressions import MAX_EXPRESSION_LENGTH, _ALIASES, _parse, validate_expression

# Restrict source characters as well as the AST: comments, Unicode identifier
# normalization and all untrusted LaTeX/HTML control characters are excluded.
_SOURCE = re.compile(r"[A-Za-z0-9_ \t\r\n.+*/(),<>=!\-]+\Z", re.ASCII)
_IDENTIFIER = re.compile(r"[A-Za-z_][A-Za-z0-9_]{0,63}\Z", re.ASCII)
_DIGITS = r"[0-9](?:_?[0-9])*"
_DECIMAL = re.compile(
    rf"(?:{_DIGITS}(?:\.(?:{_DIGITS})?)?|\.{_DIGITS})(?:[eE][+\-]?{_DIGITS})?\Z",
    re.ASCII,
)
_BASED_INTEGER = re.compile(r"(?:0[xX](?:_?[0-9a-fA-F])+|0[oO](?:_?[0-7])+|0[bB](?:_?[01])+)\Z", re.ASCII)
_COMPARISONS = {
    ast.Lt: "<", ast.LtE: r"\le", ast.Gt: ">", ast.GtE: r"\ge",
    ast.Eq: "=", ast.NotEq: r"\ne",
}


def _parentheses(value: str) -> str:
    return r"\left(" + value + r"\right)"


def expression_to_latex(expression: str) -> str | None:
    """Return trusted LaTeX for supported local DSL structure, or ``None``.

    Names, operator aliases, argument order and original numeric lexemes are
    retained. Only redundant source parentheses are omitted; required grouping
    is preserved. Documented aliases are checked on a separate AST and keep
    their submitted spelling in the reading view. Data availability is outside
    this presentation-only check. Accepting an alias for a reading view does
    not imply it can execute before the existing explicit repair step. No user
    text becomes a LaTeX command.
    """
    if type(expression) is not str or len(expression) > MAX_EXPRESSION_LENGTH or not _SOURCE.fullmatch(expression):
        return None
    text = expression.strip()
    try:
        tree = _parse(text)  # Existing length, node and depth limits.
        fields: set[str] = set()
        for node in ast.walk(tree):
            if isinstance(node, ast.Name):
                token = ast.get_source_segment(text, node)
                if not _IDENTIFIER.fullmatch(node.id) or token != node.id:
                    return None
                fields.add(node.id)
            elif isinstance(node, ast.Constant):
                token = ast.get_source_segment(text, node)
                if (type(node.value) not in (int, float) or token is None
                        or not (_DECIMAL.fullmatch(token) or (type(node.value) is int and _BASED_INTEGER.fullmatch(token)))):
                    return None
        validated = copy.deepcopy(tree)
        for node in ast.walk(validated):
            if isinstance(node, ast.Call) and isinstance(node.func, ast.Name):
                node.func.id = _ALIASES.get(node.func.id, node.func.id)
        # Validate arity, panel operands, integer row windows and exact executable
        # grammar independently; the unmodified tree supplies the reading view.
        validate_expression(ast.unparse(validated.body), fields)

        def render(node: ast.AST) -> tuple[str, int]:
            if isinstance(node, ast.Name):
                return r"\operatorname{" + node.id.replace("_", r"\_") + "}", 100
            if isinstance(node, ast.Constant):
                token = ast.get_source_segment(text, node)
                assert token is not None
                escaped = token.replace("_", r"\_")
                # Preserve the source scientific literal instead of converting
                # a binary64 value or inventing an exponent/rounded mantissa.
                if "e" in token.lower() or _BASED_INTEGER.fullmatch(token):
                    escaped = r"\mathtt{" + escaped + "}"
                return escaped, 100
            if isinstance(node, ast.Call):
                name, _ = render(node.func)
                args = ", ".join(render(arg)[0] for arg in node.args)
                return name + _parentheses(args), 100
            if isinstance(node, ast.UnaryOp):
                value, _ = render(node.operand)
                if isinstance(node.operand, (ast.BinOp, ast.UnaryOp, ast.Compare)):
                    value = _parentheses(value)
                return ("+" if isinstance(node.op, ast.UAdd) else "-") + value, 40
            if isinstance(node, ast.BinOp):
                left, lp = render(node.left)
                right, rp = render(node.right)
                if isinstance(node.op, ast.Div):
                    return r"\frac{" + left + "}{" + right + "}", 100
                precedence = 30 if isinstance(node.op, ast.Mult) else 20
                if lp < precedence:
                    left = _parentheses(left)
                # The right child at equal precedence needs parentheses to keep
                # the submitted AST, even for mathematically associative ops.
                if rp <= precedence:
                    right = _parentheses(right)
                symbol = r" \cdot " if isinstance(node.op, ast.Mult) else " + " if isinstance(node.op, ast.Add) else " - "
                return left + symbol + right, precedence
            if isinstance(node, ast.Compare):
                left, lp = render(node.left)
                right, rp = render(node.comparators[0])
                if lp <= 10:
                    left = _parentheses(left)
                if rp <= 10:
                    right = _parentheses(right)
                return left + " " + _COMPARISONS[type(node.ops[0])] + " " + right, 10
            raise ValueError("Unsupported formula node")

        return render(tree.body)[0]
    except (ValueError, TypeError, SyntaxError, RecursionError, OverflowError):
        return None
