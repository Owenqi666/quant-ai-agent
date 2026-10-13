"""Discover the curated public checkout's tests and save their actual outcomes.

Run from a freshly exported public repository, where every tests/test_*.py is
part of the public core. Skipped or expected-failure core tests fail this gate.
"""
import argparse
import json
from pathlib import Path
import sys
import unittest


class PublicResult(unittest.TextTestResult):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.outcomes = {}

    def startTest(self, test):
        self.outcomes[test.id()] = "running"
        super().startTest(test)

    def addSuccess(self, test):
        self.outcomes[test.id()] = "passed"
        super().addSuccess(test)

    def addError(self, test, err):
        self.outcomes[test.id()] = "error"
        super().addError(test, err)

    def addFailure(self, test, err):
        self.outcomes[test.id()] = "failed"
        super().addFailure(test, err)

    def addSubTest(self, test, subtest, err):
        if err is not None:
            self.outcomes[test.id()] = "failed"
        super().addSubTest(test, subtest, err)

    def addSkip(self, test, reason):
        self.outcomes[test.id()] = "skipped"
        super().addSkip(test, reason)


def test_ids(suite):
    for item in suite:
        if isinstance(item, unittest.TestSuite):
            yield from test_ids(item)
        else:
            yield item.id()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, default=Path(".ci/python-tests.json"))
    args = parser.parse_args()
    root = Path(__file__).resolve().parents[1]
    sys.path.insert(0, str(root))
    suite = unittest.defaultTestLoader.discover(str(root / "tests"), pattern="test_*.py", top_level_dir=str(root))
    collected = list(test_ids(suite))
    result = unittest.TextTestRunner(verbosity=2, resultclass=PublicResult).run(suite)
    accepted = bool(collected) and result.wasSuccessful() and not result.skipped and not result.expectedFailures
    report = {
        "collected": len(collected), "run": result.testsRun,
        "passed": sum(value == "passed" for value in result.outcomes.values()),
        "failed": sum(value in {"failed", "error"} for value in result.outcomes.values()),
        "skipped": len(result.skipped), "expected_failures": len(result.expectedFailures),
        "unexpected_successes": len(result.unexpectedSuccesses), "accepted": accepted,
        "tests": [{"id": name, "outcome": result.outcomes.get(name, "not_run")} for name in collected],
        "skip_reasons": [{"id": test.id(), "reason": reason} for test, reason in result.skipped],
        "failures": [{"id": test.id(), "traceback": trace} for test, trace in result.failures + result.errors],
    }
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(report, indent=2) + "\n")
    return 0 if accepted else 1


if __name__ == "__main__":
    raise SystemExit(main())
