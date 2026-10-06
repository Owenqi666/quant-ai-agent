"""Measure bounded local synthetic workloads; preserve every sample and receipt.

Run after source freeze. These are service-call measurements in an isolated
workspace, not browser/network latency or evidence of performance improvement.
"""
from __future__ import annotations

import argparse
from datetime import datetime, timezone
import hashlib
from importlib.metadata import version
import json
import math
import os
from pathlib import Path
import platform
import statistics
import sys
import time

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from paper_alpha.server.dataset_imports import DatasetImports
from paper_alpha.server.db import transaction
from paper_alpha.server.feedback import Feedback
from paper_alpha.server.maintenance import workspace_lease
from paper_alpha.server.runner import Worker, worker_lock
from paper_alpha.server.service import Store
from paper_alpha.storage import atomic_json, digest
from paper_alpha.workflow import code_files

REPEATS = 3
EVENT_COUNT = 2501
CATALOG_COUNT = 500


def sample_summary(samples):
    """Keep denominators and missing measurements explicit; never drop outliers."""
    values = list(samples)
    if any(type(value) not in (int, float) or not math.isfinite(value) or value < 0 for value in values):
        raise ValueError("Timing samples must be finite nonnegative numbers")
    return {"unit": "seconds", "sample_count": len(values), "samples": values,
            "median": statistics.median(values) if values else None,
            "minimum": min(values) if values else None, "maximum": max(values) if values else None}


def code_snapshot():
    return {name: hashlib.sha256(path.read_bytes()).hexdigest() for name, path in code_files().items()}


def elapsed_call(function):
    start = time.perf_counter()
    result = function()
    return result, time.perf_counter() - start


def page_round(fetch, *, expected_count, id_field, page_size):
    """Measure a full bounded catch-up while checking its frozen append boundary."""
    cursor, watermark, identities, page_samples = 0, None, [], []
    start = time.perf_counter()
    for _ in range((expected_count + page_size - 1) // page_size + 2):
        page, seconds = elapsed_call(lambda: fetch(cursor, page_size, watermark))
        page_samples.append(seconds)
        if watermark is None:
            watermark = page["high_watermark"]
        if page["high_watermark"] != watermark or page["total_records"] != expected_count:
            raise ValueError("Page count or append watermark changed during a measured catch-up")
        if len(page["items"]) > page_size:
            raise ValueError("Server returned more than the requested page limit")
        identities.extend(item[id_field] for item in page["items"])
        if page["has_more"] and page["next_cursor"] <= cursor:
            raise ValueError("Page cursor did not advance")
        cursor = page["next_cursor"]
        if not page["has_more"]:
            break
    else:
        raise ValueError("Measured pagination exceeded its bounded page count")
    wall = time.perf_counter() - start
    if len(identities) != expected_count or len(set(identities)) != expected_count:
        raise ValueError("Pagination lost or duplicated records")
    return {"elapsed_seconds": wall, "page_count": len(page_samples), "page_samples_seconds": page_samples,
            "record_count": len(identities), "unique_record_count": len(set(identities)),
            "record_ids_digest": digest(identities), "high_watermark": watermark, "next_cursor": cursor}


def measure_pages(fetch, *, expected_count, id_field, page_size):
    rounds = [page_round(fetch, expected_count=expected_count, id_field=id_field, page_size=page_size)
              for _ in range(REPEATS)]
    return {"repetitions": len(rounds), "expected_records": expected_count, "page_size": page_size,
            "rounds": rounds,
            "complete_catch_up": sample_summary(item["elapsed_seconds"] for item in rounds),
            "individual_page_calls": sample_summary(value for item in rounds for value in item["page_samples_seconds"]),
            "scope": "Direct service calls; includes SQLite reads and DTO construction, excludes HTTP/browser and result-file serialization. Repeated warm workspace; no cache reset or outlier removal."}


def measure(out):
    source = code_snapshot()
    report = {"schema_version": 1, "created_at": datetime.now(timezone.utc).isoformat(), "passed": False,
              "environment": {"python": platform.python_version(), "platform": platform.platform(),
                              "machine": platform.machine(), "processor": platform.processor(), "logical_cpus": os.cpu_count(),
                              "packages": {name: version(name) for name in ("paper-to-alpha", "numpy", "pandas", "pypdf", "fastapi")}},
              "source_sha256": source, "source_digest": digest(source), "measurements": {},
              "plan": {"repetitions": REPEATS, "event_records": EVENT_COUNT, "queued_catalog_runs": CATALOG_COUNT,
                       "event_page_size": 200, "catalog_page_size": 50,
                       "execution_order": ["one_real_normalized_fixed_run", "seed_catalog_and_events", "event_pages", "catalog_pages",
                                           "three_regression_checks", "three_feedback_summaries", "three_dataset_validations"]},
              "scope": "One local machine, synthetic fixtures, serial operations and a newly created private workspace. Timings measure this revision only.",
              "limitations": ["No before/after comparison or performance-improvement claim.",
                              "Three repeats measure the same workload, not three independent research tasks or datasets.",
                              "SQLite/OS caches are not reset; observations include process startup where stated.",
                              "Service timing fields retain their declared scope and may exclude final publication/commit.",
                              "Write-transaction timing is not a measurement of concurrent-writer waiting time.",
                              "No AI calls, real-market performance, human effort, monetary cost, production throughput or remote-CI claim."]}
    try:
        workspace = out / "workspace"
        with workspace_lease(workspace):
            store = Store(workspace)
            feedback = Feedback(store)
            example = store.seed_example()
            submitted = store.submit_run(example["revision_id"], "normalized_fixed", "measurement-real-run")
            with worker_lock(workspace) as descriptor:
                worker = Worker(store, descriptor)
                did_work, execution_seconds = elapsed_call(worker.run_once)
            actual = store.get_run(submitted["id"])
            if not did_work or actual["status"] != "completed" or not actual.get("verification", {}).get("verified"):
                raise RuntimeError("The real measurement fixture did not complete with verified results")
            review = store.create_review(actual["id"], "alpha006", "accepted", "implementation",
                "Automated performance fixture: approve the exact normalized Alpha006 implementation for repeated software verification; no human research or economic-validity claim.", source="automation")
            case = store.approve_case(review["id"], "evaluated", "Automated performance fixture: repeat the same supported evidence and numerical verification")
            atomic_json(out / "real-run.json", actual)
            atomic_json(out / "review-and-case.json", {"review": review, "case": case})
            report["real_execution"] = {"run_id": actual["id"], "attempt_id": actual["attempts"][-1]["id"],
                                        "mode": "normalized_fixed", "verified": True,
                                        "worker_run_once_seconds": sample_summary([execution_seconds]),
                                        "scope": "One real worker subprocess execution; this sample is setup evidence, not a three-run execution benchmark.",
                                        "receipt": "real-run.json", "review_source": "automation", "case_id": case["id"]}

            before = time.perf_counter()
            original = store.get_research(example["research_id"])
            catalog_research = store.create_research("Measurement-only queued catalog fixtures", original["paper_id"],
                                                     original["dataset_id"], original["revisions"][0]["task"])
            catalog_runs = [store.submit_run(catalog_research["latest_revision_id"], "fixed", f"measurement-queue-{index}")
                            for index in range(CATALOG_COUNT)]
            event_run_id = catalog_runs[0]["id"]
            with transaction(store.db_path) as connection:
                existing = connection.execute("SELECT COUNT(*) FROM events WHERE run_id=?", (event_run_id,)).fetchone()[0]
                if existing != 1:
                    raise ValueError("Expected exactly one queued event before seeding pagination fixtures")
                for number in range(1, EVENT_COUNT):
                    store._event(connection, event_run_id, "performance_fixture", {"ordinal": number, "automated_fixture": True})
            report["workload_setup"] = {"elapsed_seconds": time.perf_counter() - before,
                                        "catalog_research_id": catalog_research["id"], "queued_run_count": len(catalog_runs),
                                        "event_run_id": event_run_id, "event_record_count": EVENT_COUNT,
                                        "scope": "Queued records only: none of these 500 runs is executed. Events are explicitly labelled generated pagination fixtures."}
            report["measurements"]["event_pagination"] = measure_pages(
                lambda after, limit, through: store.event_page(event_run_id, after, limit, through),
                expected_count=EVENT_COUNT, id_field="sequence", page_size=200)
            report["measurements"]["catalog_pagination"] = measure_pages(
                lambda after, limit, through: feedback.page("runs", after=after, limit=limit, through=through,
                                                          research_id=catalog_research["id"], status="queued"),
                expected_count=CATALOG_COUNT, id_field="id", page_size=50)

            regressions = []
            for index in range(REPEATS):
                check, elapsed = elapsed_call(lambda: store.run_regression_check(actual["id"], [case["id"]]))
                if check.get("outcome") != "passed" or check.get("passed") is not True:
                    raise RuntimeError("Measured regression check did not pass; no timing-only acceptance")
                name = f"regression-{index + 1}.json"
                atomic_json(out / name, check)
                regressions.append({"check_id": check["id"], "elapsed_seconds": elapsed, "receipt": name,
                                    "service_timing": check.get("timing", {})})
            timing_fields = ("total_before_publish_seconds", "write_transaction_before_publish_seconds")
            report["measurements"]["regression"] = {
                "repetitions": len(regressions), "candidate_id": "alpha006", "case_id": case["id"],
                "samples": regressions, "whole_service_call": sample_summary(sample["elapsed_seconds"] for sample in regressions),
                "service_timing_fields": {name: sample_summary(sample["service_timing"][name] for sample in regressions
                                                              if name in sample["service_timing"])
                                          for name in timing_fields},
                "scope": "Same original-fixture run and approved Alpha006 case checked three times; includes real evidence/numerical verification. Missing timing fields have sample_count=0 and null statistics."}

            summaries = []
            for index in range(REPEATS):
                summary, elapsed = elapsed_call(lambda: feedback.summary(original["id"]))
                name = f"feedback-summary-{index + 1}.json"
                atomic_json(out / name, summary)
                summaries.append({"elapsed_seconds": elapsed, "input_digest": summary["input_digest"], "receipt": name})
            report["measurements"]["feedback_summary"] = {
                "repetitions": len(summaries), "research_id": original["id"], "samples": summaries,
                "whole_service_call": sample_summary(sample["elapsed_seconds"] for sample in summaries),
                "same_input_digest": len({sample["input_digest"] for sample in summaries}) == 1,
                "scope": "One real research run, its review and three regression records; excludes the separate queued-only catalog research."}

            fixture = ROOT / "tests/fixtures/datasets"
            imports = DatasetImports(store.root, store.db_path)
            uploaded = imports.create("Automated dataset-validation timing fixture", (fixture / "market.csv").read_bytes(),
                                      (fixture / "metadata.json").read_bytes(), "measurement-upload")
            validations = []
            for index in range(REPEATS):
                result, elapsed = elapsed_call(lambda: imports.validate(uploaded["id"], f"measurement-validation-{index}"))
                if result["status"] != "valid":
                    raise RuntimeError("Measured dataset validation did not pass")
                latest = result["latest_validation"]
                name = f"dataset-validation-{index + 1}.json"
                atomic_json(out / name, result)
                validations.append({"attempt_id": latest["id"], "elapsed_seconds": elapsed,
                                    "child_report_seconds": latest["report"]["duration_seconds"], "receipt": name,
                                    "report_digest": latest["report_digest"], "validator_digest": latest["validator_digest"]})
            report["measurements"]["dataset_validation"] = {
                "repetitions": len(validations), "import_id": uploaded["id"], "input_digest": uploaded["input_digest"],
                "samples": validations, "whole_service_call": sample_summary(sample["elapsed_seconds"] for sample in validations),
                "child_report_scope": sample_summary(sample["child_report_seconds"] for sample in validations),
                "rows": result["latest_validation"]["report"]["summary"]["rows"],
                "scope": "Same 672-row synthetic upload, three real child processes and separately retained attempts. Outer duration includes startup and persistence; child duration follows its report scope. Not registered as a new research version here."}
            if any(run["status"] != "queued" for run in store.list_runs() if run["research_id"] == catalog_research["id"]):
                raise ValueError("Measurement unexpectedly executed a queued-only catalog fixture")
        report["source_unchanged"] = code_snapshot() == source
        if not report["source_unchanged"]:
            raise RuntimeError("Source changed during measurements; preserve this directory and rerun after source freeze")
        report["passed"] = True
    except Exception as exc:
        report["error"] = f"{type(exc).__name__}: {exc}"
    finally:
        report["finished_at"] = datetime.now(timezone.utc).isoformat()
        atomic_json(out / "performance.json", report)
        (out / "README.md").write_text(render_report(report), encoding="utf-8")
    return report


def render_report(report):
    lines = ["# 本机合成工作负载测量", "", f"测量完成：{report['passed']}", "",
             "原始样本、环境、输入摘要和收据见 `performance.json`；隔离工作区保留在 `workspace/`。",
             "这些是当前版本的本地服务调用耗时；包含 Python/SQLite DTO 工作，不包含 HTTP 传输、浏览器渲染或文件报告序列化。",
             "固定连续重复 3 次，未清缓存或删除异常值。单个真实引擎运行仅作为准备和真实性凭据。", "",
             "| 工作负载 | 计时样本数 | 中位数（秒） | 最小—最大（秒） |", "|---|---:|---:|---:|"]
    for name, value in report["measurements"].items():
        timing = value.get("complete_catch_up", value.get("whole_service_call"))
        if timing:
            lines.append(f"| {name} | {timing['sample_count']} | {timing['median']:.6f} | {timing['minimum']:.6f}–{timing['maximum']:.6f} |")
    regression = report["measurements"].get("regression", {}).get("service_timing_fields", {})
    for name, value in regression.items():
        if value["sample_count"]:
            lines.append(f"| regression / {name} | {value['sample_count']} | {value['median']:.6f} | {value['minimum']:.6f}–{value['maximum']:.6f} |")
    lines += ["", "事务计时字段保留服务端的计时边界，可能不覆盖最终 INSERT/COMMIT；不能当作并发写者等待时间。",
              "500 个历史记录只是未执行的队列记录，2501 条分页事件包含人工生成的工程 fixture；并非完成了 500 次研究。",
              "结果不能推导性能提升、生产吞吐、人工耗时改善或投资表现。重复测量没有增加独立研究样本数。"]
    if report.get("error"):
        lines += ["", "本次未完成：" + report["error"]]
    return "\n".join(lines) + "\n"


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--out", type=Path, required=True, help="A new directory; measurement creates only its private workspace")
    args = parser.parse_args(argv)
    out = args.out.resolve()
    if out.exists() or out.is_relative_to(ROOT / "var"):
        parser.error("Choose a new output directory outside the normal var workspace")
    out.mkdir(parents=True, exist_ok=False)
    report = measure(out)
    print(json.dumps({"passed": report["passed"], "evidence": str(out / "performance.json"),
                      "scope": "Local synthetic measurements; no performance improvement claim"}, ensure_ascii=False))
    return 0 if report["passed"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
