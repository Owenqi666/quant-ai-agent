import copy
import json
import unittest

import pandas as pd

from paper_alpha.expressions import compute_expression, validate_expression
from paper_alpha.formula_presentation import expression_to_latex


F = r"\operatorname{close}"
O = r"\operatorname{open}"
H = r"\operatorname{high}"
L = r"\operatorname{low}"
FORMULA = "((close - open) / ((high - low) + .001))"


class FormulaPresentationTests(unittest.TestCase):
    def test_alpha101_readable_fraction_and_rank(self):
        fraction = r"\frac{" + F + " - " + O + "}{" + H + " - " + L + " + .001}"
        self.assertEqual(expression_to_latex(FORMULA), fraction)
        self.assertEqual(expression_to_latex("rank(" + FORMULA + ")"), r"\operatorname{rank}\left(" + fraction + r"\right)")

    def test_supported_alias_spelling_and_argument_order(self):
        value = expression_to_latex("-correlation(rank(close), rank(volume), 6)")
        self.assertEqual(value, r"-\operatorname{correlation}\left(\operatorname{rank}\left(\operatorname{close}\right), \operatorname{rank}\left(\operatorname{volume}\right), 6\right)")
        for name in ("delta", "stddev", "ts_delta", "ts_std", "ts_mean", "ts_min", "ts_max", "ts_sum"):
            with self.subTest(name=name):
                self.assertIn(r"\operatorname{" + name.replace("_", r"\_") + "}", expression_to_latex(f"{name}(close, 3)"))
        self.assertNotEqual(expression_to_latex("ts_corr(close, volume, 6)"), expression_to_latex("ts_corr(volume, close, 6)"))

    def test_binary_grouping_keeps_order_and_nested_structure(self):
        self.assertEqual(expression_to_latex("close - (open - high)"), F + r" - \left(" + O + " - " + H + r"\right)")
        self.assertEqual(expression_to_latex("close + (open + high)"), F + r" + \left(" + O + " + " + H + r"\right)")
        self.assertEqual(expression_to_latex("close * (open * high)"), F + r" \cdot \left(" + O + r" \cdot " + H + r"\right)")
        self.assertEqual(expression_to_latex("(close + open) * high"), r"\left(" + F + " + " + O + r"\right) \cdot " + H)
        self.assertNotEqual(expression_to_latex("close / (open / high)"), expression_to_latex("(close / open) / high"))
        self.assertEqual(expression_to_latex("close / (open / high)"), r"\frac{" + F + r"}{\frac{" + O + "}{" + H + "}}")

    def test_unary_and_comparison_structure(self):
        self.assertEqual(expression_to_latex("-(close + open)"), r"-\left(" + F + " + " + O + r"\right)")
        self.assertEqual(expression_to_latex("--close"), r"-\left(-" + F + r"\right)")
        self.assertNotEqual(expression_to_latex("-close / open"), expression_to_latex("-(close / open)"))
        for op, latex in (("<", "<"), ("<=", r"\le"), (">", ">"), (">=", r"\ge"), ("==", "="), ("!=", r"\ne")):
            with self.subTest(op=op):
                self.assertEqual(expression_to_latex("close " + op + " open"), F + " " + latex + " " + O)
        self.assertEqual(expression_to_latex("(close < open) < high"), r"\left(" + F + " < " + O + r"\right) < " + H)

    def test_decimal_tokens_are_exact_without_float_rounding(self):
        for token in (".001", "0.0010", "0.1000000000000000001", "1.00000000000000000000000000000001", "1_000.00"):
            with self.subTest(token=token):
                self.assertEqual(expression_to_latex("close + " + token), F + " + " + token.replace("_", r"\_"))
        self.assertNotEqual(expression_to_latex("close + 0.1"), expression_to_latex("close + 0.1000000000000000001"))
        for token in ("1e-3", "1.2300E+04", "1e-3000", "0xff", "0o17", "0b101", "0x_FF"):
            with self.subTest(token=token):
                self.assertEqual(expression_to_latex("close + " + token), F + r" + \mathtt{" + token.replace("_", r"\_") + "}")

    def test_literal_window_boundary_and_panel_rules(self):
        for expr in ("delay(close, 0)", "ts_mean(close, 252)", "log(close)", "abs(close)", "sign(close)", " (close\n + open) "):
            with self.subTest(expr=expr):
                self.assertIsNotNone(expression_to_latex(expr))
        for expr in ("ts_mean(close, 0)", "ts_mean(close, 253)", "delay(close, -1)", "ts_corr(close, volume, 6.0)", "ts_mean(close, 1 + 1)", "rank(2)", "rank(close, 2)", "rank(close, axis=1)", "1+2"):
            with self.subTest(expr=expr):
                self.assertIsNone(expression_to_latex(expr))

    def test_invalid_unsupported_and_injection_input_falls_back(self):
        for expr in (None, 1, "", "   ", "ts_rank(close, 3)", "close**2", "sum(close, 3)", "close[0]", "close.__class__", "close and open", "close + True", "close + 1e309", "close + 1000000001", "<img src=x>", r"close + \href{https://example.com}{x}", "close # \\input{secrets}", "ｃｌｏｓｅ", "café", "close\x00", "close; open", "'close'", "__import__('os')"):
            with self.subTest(expr=expr):
                self.assertIsNone(expression_to_latex(expr))

    def test_bounded_complexity_and_safe_identifier_escaping(self):
        self.assertIsNone(expression_to_latex("close + " + "1+" * 1100 + "1"))
        self.assertIsNone(expression_to_latex("-" * 40 + "close"))
        self.assertIsNone(expression_to_latex("rank(" * 40 + "close" + ")" * 40))
        self.assertIsNone(expression_to_latex("x" * 65))
        self.assertEqual(expression_to_latex("close_px_1"), r"\operatorname{close\_px\_1}")

    def test_presentation_does_not_modify_json_execution_or_metadata(self):
        payload = {"expression": FORMULA, "configuration": {"window": 10}}
        expected = copy.deepcopy(payload)
        encoded = json.dumps(payload, sort_keys=True)
        fields = {"close", "open", "high", "low"}
        metadata_before = validate_expression(FORMULA, fields)
        panels = {"close": pd.DataFrame([[3.0, 4.0]]), "open": pd.DataFrame([[2.0, 2.5]]), "high": pd.DataFrame([[4.0, 5.0]]), "low": pd.DataFrame([[1.0, 2.0]])}
        values_before = compute_expression(FORMULA, panels)
        first = expression_to_latex(payload["expression"])
        self.assertIsNotNone(first)
        self.assertEqual(first, expression_to_latex(payload["expression"]))
        self.assertEqual(payload, expected)
        self.assertEqual(json.dumps(payload, sort_keys=True), encoded)
        self.assertEqual(validate_expression(FORMULA, fields), metadata_before)
        pd.testing.assert_frame_equal(compute_expression(FORMULA, panels), values_before)


if __name__ == "__main__":
    unittest.main()
