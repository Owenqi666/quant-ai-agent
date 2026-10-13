"""Compare the retained ts_corr reference with production on seeded panels.

Timings, complete-process peak RSS, exact-fallback counts and optional profiles
are separate runs. A successful run means only that the measured cases passed
the same-environment bitwise comparison; speed is never an acceptance gate.
"""
from __future__ import annotations

import argparse
import contextlib
import hashlib
import importlib
import inspect
import io
import json
import os
from pathlib import Path
import platform
import re
import statistics
import subprocess
import sys
import time


ROOT = Path(__file__).resolve().parents[1]
THREAD_ENV = ("OMP_NUM_THREADS", "OPENBLAS_NUM_THREADS", "MKL_NUM_THREADS",
              "VECLIB_MAXIMUM_THREADS", "NUMEXPR_NUM_THREADS", "BLIS_NUM_THREADS")


def arguments():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--suite", choices=("smoke", "local"), default="smoke")
    parser.add_argument("--large", action="store_true", help="Add the 2500-date x 500-asset operator case to local")
    parser.add_argument("--warmups", type=int, default=5)
    parser.add_argument("--repeats", type=int, default=5)
    parser.add_argument("--seed", type=int, default=20261013)
    parser.add_argument("--chunk", type=int, default=1024, help="Production chunk_size; reference has no chunk parameter")
    parser.add_argument("--threads", type=int, default=1, help="Set recorded thread environment before importing NumPy")
    parser.add_argument("--output-dir", type=Path, help="New directory for benchmark.json and report.md")
    parser.add_argument("--profile", action="store_true", help="Run separate cProfile calls after measurements")
    parser.add_argument("--_child", choices=("memory", "fallback", "profile"), help=argparse.SUPPRESS)
    parser.add_argument("--_implementation", choices=("reference", "production"), help=argparse.SUPPRESS)
    parser.add_argument("--_case", help=argparse.SUPPRESS)
    args = parser.parse_args()
    if args.warmups < 5 or args.repeats < 5:
        parser.error("warmups and repeats must both be at least 5")
    if args.chunk < 1 or args.threads < 1 or args.seed < 0:
        parser.error("chunk/threads must be positive and seed must be nonnegative")
    if args.large and args.suite != "local":
        parser.error("--large requires --suite local")
    if args._child:
        if not args._implementation or not args._case:
            parser.error("internal child requires implementation and case")
    elif args.output_dir is None:
        parser.error("--output-dir is required")
    return args


def modules():
    # main() sets the environment before any import that can load NumPy/BLAS.
    sys.path.insert(0, str(ROOT))
    return (importlib.import_module("numpy"), importlib.import_module("pandas"),
            importlib.import_module("tests.corr_reference"),
            importlib.import_module("paper_alpha.expressions"))


def cases(args):
    if args.suite == "smoke":
        settings = [(96, 8, window, scenario) for window in (5, 20)
                    for scenario in ("normal", "missing", "constant_affine", "high_fallback")]
    else:
        settings = [(500, 50, window, "normal") for window in (5, 20, 60)]
        settings += [(1000, 200, window, "normal") for window in (20, 60)]
        settings += [(500, 50, 20, scenario) for scenario in
                     ("missing", "constant_affine", "high_fallback")]
        if args.large:
            settings.append((2500, 500, 20, "normal"))
    return [{"id": f"{scenario}-{rows}x{columns}-w{window}", "dates": rows,
             "assets": columns, "window": window, "scenario": scenario,
             "seed": args.seed, "layout": "C", "dtype": "float64"}
            for rows, columns, window, scenario in settings]


def panels(case, np, pd):
    rng = np.random.default_rng(case["seed"])
    shape = (case["dates"], case["assets"])
    x, y = rng.normal(size=shape), rng.normal(size=shape)
    if case["scenario"] == "missing":
        x[rng.random(shape) < 0.025] = np.nan
        y[rng.random(shape) < 0.025] = np.nan
    elif case["scenario"] == "constant_affine":
        x = rng.integers(-20, 21, size=shape).astype(float)
        y = 3 * x + 7
        x[:, 0], y[:, 0] = 2.0, 5.0
        y[:, 1] = -2 * x[:, 1] + 9
        y[:, 2] = x[:, 2]
        y[:, 3] = -x[:, 3]
    elif case["scenario"] == "high_fallback":
        z = rng.integers(0, 17, size=shape).astype(float)
        x, y = 1e12 + z, 3e12 + 2 * z
        x[:, 0] = np.where(np.arange(shape[0]) % 2, 1e308, -1e308)
        y[:, 0] = 0.5 * x[:, 0]
    columns = [f"asset_{number:04d}" for number in range(shape[1])]
    return (pd.DataFrame(x, columns=columns, copy=False),
            pd.DataFrame(y, columns=columns, copy=False))


def input_hashes(left, right):
    # A contiguous memoryview avoids making an extra full input copy for RSS.
    return {name: hashlib.sha256(memoryview(panel.to_numpy())).hexdigest()
            for name, panel in (("left", left), ("right", right))}


def invoke(implementation, loaded, left, right, window, chunk):
    module = loaded[2] if implementation == "reference" else loaded[3]
    kwargs = {} if implementation == "reference" else {"chunk_size": chunk}
    return module._stable_correlation(left, right, window, **kwargs)


def comparison(left, right, np):
    a, b = left.to_numpy(), right.to_numpy()
    axes_equal = left.index.equals(right.index) and left.columns.equals(right.columns)
    shape_equal = a.shape == b.shape
    dtype_equal = left.dtypes.equals(right.dtypes)
    if not shape_equal:
        return {"passed": False, "axes_equal": axes_equal, "shape_equal": False,
                "dtype_equal": dtype_equal, "nan_mask_differences": None,
                "non_nan_bit_differences": None}
    mask_a, mask_b = np.isnan(a), np.isnan(b)
    nan_differences = int(np.count_nonzero(mask_a != mask_b))
    present = ~mask_a & ~mask_b
    # float64 uint64 patterns distinguish signed zero; no tolerance is used.
    bit_differences = int(np.count_nonzero(a.view(np.uint64)[present] != b.view(np.uint64)[present]))
    return {"passed": axes_equal and dtype_equal and nan_differences == 0 and bit_differences == 0,
            "axes_equal": axes_equal, "shape_equal": shape_equal, "dtype_equal": dtype_equal,
            "nan_mask_differences": nan_differences, "non_nan_bit_differences": bit_differences}


def timing(case, args, loaded, left, right):
    np = loaded[0]
    with np.errstate(over="ignore", invalid="ignore", divide="ignore"):
        reference = invoke("reference", loaded, left, right, case["window"], args.chunk)
        production = invoke("production", loaded, left, right, case["window"], args.chunk)
        checked = comparison(reference, production, np)
        del reference, production
        if not checked["passed"]:
            return checked, None
        for implementation in ("reference", "production"):
            for _ in range(args.warmups):
                result = invoke(implementation, loaded, left, right, case["window"], args.chunk)
                del result
        raw = []
        for repetition in range(args.repeats):
            order = ("reference", "production") if repetition % 2 == 0 else ("production", "reference")
            for implementation in order:
                start = time.perf_counter_ns()
                result = invoke(implementation, loaded, left, right, case["window"], args.chunk)
                elapsed = time.perf_counter_ns() - start
                del result
                raw.append({"repetition": repetition + 1, "implementation": implementation,
                            "elapsed_ns": elapsed})
    summary = {}
    for implementation in ("reference", "production"):
        values = [item["elapsed_ns"] for item in raw if item["implementation"] == implementation]
        summary[implementation] = {"count": len(values), "median_ns": statistics.median(values),
                                   "min_ns": min(values), "max_ns": max(values)}
    return checked, {"clock": "time.perf_counter_ns", "unit": "nanoseconds", "raw": raw,
                     "summary": summary, "statistic": "median of all samples; no samples discarded",
                     "reference_over_production_median":
                     summary["reference"]["median_ns"] / summary["production"]["median_ns"]}


def eligible_windows(left, right, window, np):
    count = 0
    for column in range(left.shape[1]):
        x = np.lib.stride_tricks.sliding_window_view(left.iloc[:, column].to_numpy(), window)
        y = np.lib.stride_tricks.sliding_window_view(right.iloc[:, column].to_numpy(), window)
        count += int(np.count_nonzero(np.isfinite(x).all(axis=1) & np.isfinite(y).all(axis=1)
                                     & (x != x[:, :1]).any(axis=1) & (y != y[:, :1]).any(axis=1)))
    return count


def child(args, loaded):
    np, pd = loaded[:2]
    case = json.loads(args._case)
    left, right = panels(case, np, pd)
    record = {"implementation": args._implementation, "input_sha256": input_hashes(left, right)}
    call = lambda: invoke(args._implementation, loaded, left, right, case["window"], args.chunk)
    with np.errstate(over="ignore", invalid="ignore", divide="ignore"):
        if args._child == "memory":
            import resource
            for _ in range(args.warmups):
                result = call()
                del result
            result = call()
            peak = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss
            unit = "bytes" if sys.platform == "darwin" else "KiB" if sys.platform.startswith("linux") else None
            if unit is None:
                raise RuntimeError("RSS unit conversion supports macOS and Linux only")
            record.update(method="resource.getrusage(RUSAGE_SELF).ru_maxrss", raw_peak_rss=peak,
                          raw_unit=unit, peak_rss_mib=peak / (1024 ** 2 if unit == "bytes" else 1024),
                          warmup_calls=args.warmups, measured_calls=1,
                          scope="Complete fresh process peak, including interpreter, imports, inputs and warmups")
            del result
        elif args._child == "fallback":
            module = loaded[2] if args._implementation == "reference" else loaded[3]
            original = module._pearson_binary64
            calls = 0
            def counted(*operands):
                nonlocal calls
                calls += 1
                return original(*operands)
            module._pearson_binary64 = counted
            try:
                result = call()
                del result
            finally:
                module._pearson_binary64 = original
            complete = max(0, case["dates"] - case["window"] + 1) * case["assets"]
            eligible = eligible_windows(left, right, case["window"], np)
            record.update(measured_calls=1, warmup_calls=0, exact_helper_calls=calls,
                          complete_windows=complete, finite_nonconstant_windows=eligible,
                          calls_per_complete_window=calls / complete if complete else None,
                          calls_per_finite_nonconstant_window=calls / eligible if eligible else None,
                          method="Wrap only this implementation's _pearson_binary64 in a separate process")
        else:
            import cProfile
            import pstats
            profile = cProfile.Profile()
            result = profile.runcall(call)
            del result
            stream = io.StringIO()
            pstats.Stats(profile, stream=stream).strip_dirs().sort_stats("cumulative").print_stats(25)
            record.update(measured_calls=1, warmup_calls=0, method="cProfile, cumulative top 25",
                          text=stream.getvalue())
    return record


def separate_run(mode, implementation, case, args):
    command = [sys.executable, str(Path(__file__).resolve()), "--_child", mode,
               "--_implementation", implementation, "--_case", json.dumps(case),
               "--chunk", str(args.chunk), "--threads", str(args.threads),
               "--warmups", str(args.warmups), "--repeats", str(args.repeats)]
    observed = subprocess.run(command, cwd=ROOT, capture_output=True, text=True)
    if observed.returncode:
        raise RuntimeError(f"{mode}/{implementation} child failed ({observed.returncode}): {observed.stderr.strip()}")
    return json.loads(observed.stdout)


def identities(loaded):
    files = ("scripts/benchmark_ts_corr.py", "tests/corr_reference.py", "paper_alpha/expressions.py",
             "requirements-lock.txt")
    commit = subprocess.run(["git", "rev-parse", "HEAD"], cwd=ROOT, capture_output=True, text=True)
    dirty = subprocess.run(["git", "status", "--porcelain", "--untracked-files=no"],
                           cwd=ROOT, capture_output=True, text=True)
    return {"checkout_commit": commit.stdout.strip() if commit.returncode == 0 else None,
            "tracked_dirty": bool(dirty.stdout.strip()) if dirty.returncode == 0 else None,
            "tracked_status": dirty.stdout.splitlines() if dirty.returncode == 0 else None,
            "files_sha256": {name: hashlib.sha256((ROOT / name).read_bytes()).hexdigest() for name in files},
            "functions_sha256": {name: hashlib.sha256(inspect.getsource(module._stable_correlation).encode()).hexdigest()
                                 for name, module in (("reference", loaded[2]), ("production", loaded[3]))},
            "reference_origin": {name: getattr(loaded[2], name) for name in
                                 ("SOURCE_PUBLIC_COMMIT", "SOURCE_FILE_SHA256", "SOURCE_FUNCTION_SHA256",
                                  "FROZEN_FUNCTIONS_SHA256")}}


def environment(loaded, inherited):
    np, pd = loaded[:2]
    backend = io.StringIO()
    with contextlib.redirect_stdout(backend):
        np.show_config()
        np.show_runtime()
    cpu = platform.processor()
    if sys.platform == "darwin":
        observed = subprocess.run(["sysctl", "-n", "machdep.cpu.brand_string"], capture_output=True, text=True)
        if observed.returncode == 0:
            cpu = observed.stdout.strip()
    elif Path("/proc/cpuinfo").is_file():
        cpu = next((line.split(":", 1)[1].strip() for line in Path("/proc/cpuinfo").read_text().splitlines()
                    if line.startswith("model name")), platform.machine())
    return {"os": platform.platform(), "machine": platform.machine(), "cpu": cpu,
            "python": platform.python_version(), "numpy": np.__version__, "pandas": pd.__version__,
            "thread_environment_inherited": inherited,
            "thread_environment_effective": {key: os.environ[key] for key in THREAD_ENV},
            "numpy_backend_and_runtime": backend.getvalue()}


def readable(report):
    lines = ["# ts_corr benchmark", "", f"Accepted: {report['passed']}",
             "Same-environment comparison on recorded seeded cases; no speed threshold.",
             "Peak RSS is the entire fresh process, not operator net allocation.", "",
             "| Case (dates x assets) | Reference median ms | Production median ms | Median ratio | RSS MiB ref/prod | Exact calls ref/prod |",
             "| --- | ---: | ---: | ---: | --- | --- |"]
    for case in report["cases"]:
        if case.get("timing") is None:
            lines.append(f"| {case['id']} | numeric comparison failed | | | | |")
            continue
        summary = case["timing"]["summary"]
        rss, fallback = case.get("memory", {}), case.get("fallback", {})
        memory_text = "/".join(f"{rss[name]['peak_rss_mib']:.2f}" if name in rss else "pending"
                               for name in ("reference", "production"))
        fallback_text = "/".join(str(fallback[name]["exact_helper_calls"]) if name in fallback else "pending"
                                 for name in ("reference", "production"))
        lines.append(f"| {case['id']} | {summary['reference']['median_ns'] / 1e6:.3f} | "
                     f"{summary['production']['median_ns'] / 1e6:.3f} | "
                     f"{case['timing']['reference_over_production_median']:.3f} | "
                     f"{memory_text} | {fallback_text} |")
    lines += ["", "Raw timings, numerical differences, RSS units, fallback denominators, environment and source hashes are in benchmark.json."]
    if report.get("error"):
        lines += ["", "Error: " + report["error"]]
    return "\n".join(lines) + "\n"


def public_text(text):
    # Backend/profile diagnostics may contain build or checkout directories.
    text = text.replace(str(ROOT), "<checkout>").replace(str(Path.home()), "<home>")
    user_prefix = re.escape(str(Path("/", "Users")))
    return re.sub(user_prefix + r"/[^/\s\"']+", "<home>", text)


def main():
    args = arguments()
    inherited = {key: os.environ.get(key) for key in THREAD_ENV}
    for key in THREAD_ENV:
        os.environ[key] = str(args.threads)
    os.environ["PYTHONDONTWRITEBYTECODE"] = "1"
    sys.dont_write_bytecode = True
    if args._child:
        print(json.dumps(child(args, modules()), allow_nan=False))
        return 0
    args.output_dir.mkdir(parents=True, exist_ok=False)
    report = {"schema_version": 1, "passed": False, "suite": args.suite, "large": args.large,
              "warmups_per_implementation": args.warmups, "repeats_per_implementation": args.repeats,
              "seed": args.seed, "production_chunk_size": args.chunk, "cases": [],
              "command": [Path(sys.executable).name, "scripts/benchmark_ts_corr.py", *sys.argv[1:]],
              "measurement_policy": "Timing alternates in one process; memory/fallback/profile each use separate fresh processes; construction/imports excluded from operator timings"}
    try:
        loaded = modules()
        report["environment"] = environment(loaded, inherited)
        report["source"] = identities(loaded)
        for case in cases(args):
            print(f"Measuring {case['id']}", file=sys.stderr, flush=True)
            left, right = panels(case, *loaded[:2])
            row = dict(case, input_sha256=input_hashes(left, right))
            report["cases"].append(row)
            row["numeric"], row["timing"] = timing(case, args, loaded, left, right)
            del left, right
            if not row["numeric"]["passed"]:
                raise RuntimeError("Bitwise numerical comparison failed: " + case["id"])
            for mode in ("memory", "fallback", *( ("profile",) if args.profile else () )):
                row[mode] = {}
                for implementation in ("reference", "production"):
                    measured = separate_run(mode, implementation, case, args)
                    row[mode][implementation] = measured
                    if measured["input_sha256"] != row["input_sha256"]:
                        raise RuntimeError("Child input differs: " + case["id"])
        report["source_unchanged"] = identities(loaded) == report["source"]
        report["passed"] = bool(report["cases"]) and report["source_unchanged"]
    except Exception as exc:
        report["error"] = str(exc)
    safe = public_text(json.dumps(report, indent=2, allow_nan=False))
    (args.output_dir / "benchmark.json").write_text(safe + "\n")
    rendered = public_text(readable(report))
    (args.output_dir / "report.md").write_text(rendered)
    print(rendered, end="")
    return 0 if report["passed"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
