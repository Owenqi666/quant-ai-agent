# Rolling correlation: compatibility and measured performance

The bounded batch implementation passed **243 same-environment comparisons** on both macOS and Ubuntu, including frozen-reference compatibility, downstream behavior and future/prefix causality checks, plus nine local benchmark cases and eight Ubuntu smoke cases. The reference comparisons matched NaN masks and every non-NaN float64 bit pattern, including signed zero. This finite acceptance matrix is not a proof for every legal input or a requirement for identical bits across platforms.

The reference is copied verbatim from [`1c44df6`](https://github.com/Owenqi666/quant-ai-agent/commit/1c44df62985049af0c19dea2148666e089354f05) into [tests/corr_reference.py](../tests/corr_reference.py), including its own integer-moment and 80-digit Decimal helpers. Independent Fraction cases cover selected analytically explainable values, endpoints and signed-zero underflow. The regression matrix also covers legal dtypes, C/F/strided layouts, windows 1/2/252 and equal to or greater than the input length, missing values, extreme scales, chunk seams, causality, rank ties, combined expressions and full evaluation results.

## Recorded Mac measurements

Measured clean checkout: [`3fcaadb`](https://github.com/Owenqi666/quant-ai-agent/commit/3fcaadbd2fc5aa4c6abe13f164e3b8eda2953e73), Apple M5 Pro, macOS 26.6.2 arm64, Python 3.14.8, NumPy 2.4.6, pandas 3.0.3, Accelerate. BLAS/OpenMP thread environment variables were set to 1 before imports; these are recorded settings, not a measurement of live thread counts. Seeded synthetic panels (`20261013`), C-layout float64 inputs, chunk size `1024`.

Both implementations received five warmups and five alternating timed repetitions per case. The table uses all-sample medians; imports and input construction are outside the operator timer. Exact fallback counts and profiles come from separate runs.

| Input | Assets × dates | Window | Reference ms | Batch ms | Median speedup | Peak RSS MiB, ref / batch | Exact calls, ref / batch |
| --- | --- | ---: | ---: | ---: | ---: | --- | --- |
| normal | 50 × 500 | 5 | 307.60 | 7.54 | 40.80× | 75.53 / 75.55 | 0 / 0 |
| normal | 50 × 500 | 20 | 315.17 | 9.40 | 33.54× | 75.84 / 75.73 | 0 / 0 |
| normal | 50 × 500 | 60 | 279.70 | 14.56 | 19.21× | 75.77 / 77.41 | 0 / 0 |
| normal | 200 × 1000 | 20 | 2503.57 | 63.42 | 39.47× | 80.58 / 82.91 | 0 / 0 |
| normal | 200 × 1000 | 60 | 2516.07 | 113.29 | 22.21× | 82.73 / 87.39 | 0 / 0 |
| missing | 50 × 500 | 20 | 125.73 | 6.60 | 19.06× | 75.44 / 76.14 | 0 / 0 |
| constant affine | 50 × 500 | 20 | 485.60 | 181.52 | 2.68× | 75.91 / 75.98 | 22607 / 22607 |
| high fallback | 50 × 500 | 20 | 589.74 | 273.61 | 2.16× | 75.92 / 76.39 | 24050 / 24050 |
| normal | 500 × 2500 | 20 | 16222.70 | 397.78 | 40.78× | 112.97 / 115.91 | 0 / 0 |

Each RSS value is the peak of a **new process for that implementation**, including Python, dependencies, inputs, warmups and output. macOS byte values are converted to MiB; the Linux path converts KiB. It is not operator-only allocation. All raw repetitions, min/max, RSS source units, fallback denominators, separate profiles, environment and source hashes are in the [measurement artifact](assets/ts-corr-mac-2026-10-13.json); the final public CI also archives this recorded Mac artifact without treating it as an Ubuntu timing result.

The earlier external 32–35× figure was a lead, not a prior result for this project. These measurements establish their own workload-specific ratios. About 5× was an exploration target, never a stopping rule or CI threshold.

## What changed and what remains expensive

Finite/constant masks, first-value translation, scaling and mean centering now operate on bounded groups of trailing windows within one column. Three scalar-output stacked matrix products compute the original inner products. The exact fallback threshold and integer/Decimal code remain unchanged. Temporary storage scales with `chunk × window`, without materializing the entire dates × assets × window tensor.

An initial `ascontiguousarray` candidate failed bit comparison for odd windows on this Mac. Identical centered values could enter different dot accumulation paths when a row started at a different alignment. Padding each row's storage stride to a multiple of 64 bytes preserved allocation alignment and passed the recorded matrices. This is an observed compatibility result; it does not assert that `matmul` and `dot` always select the same kernel on every backend.

Separate profiling continued after the initial speed target was exceeded. In the 200 × 1000, window-20 ordinary case, recorded function calls fell from about 15.2 million to 104 thousand, and reductions from 2,354,800 to 2,800: the main gain comes from removing per-window Python/NumPy calls. In that case, the batch profile took 0.082 s: NumPy reductions accounted for 0.014 s cumulatively and pandas column indexing for 0.013 s; 0.045 s was attributed to the operator body, which includes batch arithmetic, indexing, allocation and matrix products. The profile does not isolate that body time as memory traffic. No exact fallbacks occurred in ordinary cases. In the high-fallback case, 24,050 exact calls took 0.557 s of a 0.586 s profiled run, explaining the much smaller speedup. These high-fallback fixtures are exactly affine and return at the exact endpoint check; they measure integer-moment cost, not the later Decimal square-root branch. These instrumented times explain costs; the speedup table uses the separate uninstrumented timings.

The chosen implementation keeps column conversion and bounded arrays, accepting the measured RSS cost for the recorded speedup. Further cross-column batching would add larger temporary storage and layout complexity; replacing integer/Decimal work would change the part protecting boundary behavior. Neither is needed to deliver the measured ordinary-window benefit. Evaluator budgets and limits are unchanged; the large case calls the operator directly. Computational speed does not establish investment returns or research-quality gains.

## Ubuntu and reproduction

The [numerical-stage CI](https://github.com/Owenqi666/quant-ai-agent/actions/runs/38456656929) used PR head `3fcaadb` and actual checkout `5fdae00655794915a7668e68618579ce5da66b4e`: Ubuntu 24.04, Python 3.14.8, NumPy 2.4.6, pandas 3.0.3, OpenBLAS. It passed all 165 Python tests, the 243-comparison matrix and eight small benchmark cases. Its artifact contains its own reference/new outputs comparison and raw smoke measurements; it does not consume Mac floating-point arrays as expected outputs. Each later PR revision requires its own CI run; see [PR #3](https://github.com/Owenqi666/quant-ai-agent/pull/3) for staged and final acceptance.

After the locked installation in the [README](../README.md):

```sh
OPENBLAS_NUM_THREADS=1 VECLIB_MAXIMUM_THREADS=1 OMP_NUM_THREADS=1 \
  CORR_COMPATIBILITY_REPORT=.ci/corr-compatibility.json \
  .venv/bin/python scripts/run_public_tests.py --output .ci/python-tests.json
.venv/bin/python scripts/benchmark_ts_corr.py --suite smoke \
  --warmups 5 --repeats 5 --output-dir .ci/ts-corr-smoke
.venv/bin/python scripts/benchmark_ts_corr.py --suite local --large \
  --warmups 5 --repeats 5 --profile --output-dir .benchmarks/ts-corr-local
```

Use a new output directory for each run. `--large` is optional locally; CI uses only the smoke suite. `--chunk` changes temporary storage, and `--threads` sets the recorded thread environment before NumPy loads. Numerical differences and execution failures exit nonzero; shared-runner speed and RSS fluctuations are reported without a speed gate. The saved benchmark records exact source hashes and whether the checkout was dirty.
