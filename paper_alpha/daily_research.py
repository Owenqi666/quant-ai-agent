"""File-backed, fixed #101 development demonstration; no model or queue side effects."""
from __future__ import annotations

from copy import deepcopy
from datetime import datetime, timezone
import hashlib
from pathlib import Path
import platform
import uuid

import numpy as np
import pandas as pd

from .contracts import require
from .daily_adapter import precheck_development
from .evaluation import evaluate_development
from .expressions import compute_expression, validate_expression, OPERATOR_SEMANTICS_VERSION
from .storage import atomic_json, digest, json_text

RUN_VERSION = "daily-development-run-v1"
EXPRESSION_101 = "((close - open) / ((high - low) + .001))"
SOURCE_REFERENCE = {
    "paper_id": "arxiv:1601.00991v3",
    "document_sha256": "1f9c21afe32dcb3ee77b31548acdaea00451fbfa1c0ee10c907867bcc736fce9",
    "physical_pdf_page": 15,
    "evidence_id": "alpha101-formula",
    "quote": EXPRESSION_101,
    "verification_scope": "maintained formula reference; not new paper extraction or economic-mechanism validation",
}
CODE_FILES = (
    "paper_alpha/daily_contract.py", "paper_alpha/daily_adapter.py",
    "paper_alpha/daily_research.py", "paper_alpha/evaluation.py",
    "paper_alpha/expressions.py", "paper_alpha/vendor/operators.py",
    "paper_alpha/contracts.py", "paper_alpha/market_diagnostics.py",
    "paper_alpha/storage.py", "paper_alpha/server/provider_catalog.py",
    "scripts/evaluate_daily_development.py",
)


def research_code_identity():
    """Actual engine/adapter/catalog/CLI bytes; no mutable VCS or DB dependency."""
    root = Path(__file__).resolve().parents[1]
    value = {"run_version": RUN_VERSION,
             "operator_semantics_version": OPERATOR_SEMANTICS_VERSION,
             "files": {name: hashlib.sha256((root / name).read_bytes()).hexdigest()
                       for name in CODE_FILES},
             "python": platform.python_version(), "numpy": np.__version__, "pandas": pd.__version__}
    return {**value, "digest": digest(value)}


def _now():
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def _factor_record(factor):
    values = factor.to_numpy(dtype=float)
    return {"schema_version": 1,
            "dates": factor.index.strftime("%Y-%m-%d").tolist(),
            "assets": [str(a) for a in factor.columns],
            "values": [[float(v) if np.isfinite(v) else None for v in row] for row in values],
            "diagnostics": deepcopy(factor.attrs.get("expression_diagnostics", {}))}


def _report(result, candidate, identity):
    """All reported statistics are a copy of the actual evaluator's output."""
    report = {"schema_version": 1, "report_version": RUN_VERSION,
              "scope": "engineering_fixture_development_validation",
              "candidate_id": candidate["id"], "source_reference": candidate["source_reference"],
              "expression": candidate["expression"], "result_digest": digest(result),
              "reported_metrics": deepcopy(result["metrics"]),
              "reported_daily": deepcopy(result["daily"]), "identity": deepcopy(identity),
              "test_freshness": "unknown", "real_source_ready": False,
              "real_execution_readiness": "pending",
              "limitations": deepcopy(result["limitations"])}
    text = "# Alpha101 #101：开发期工程演示\n\n"
    text += "本次只验证软件计算链路。工程夹具不代表真实行情、独立论文复现或投资表现。\n\n"
    text += f"论文公式参考：{SOURCE_REFERENCE['paper_id']}，PDF 物理第 15 页。此引用沿用项目维护记录，本次没有重新抽取或审核论文。\n\n"
    text += "```text\n" + candidate["expression"] + "\n```\n\n"
    text += "以下指标直接来自 evaluation.json；undefined 值保持 null。\n\n```json\n"
    text += json_text(result["metrics"]).rstrip() + "\n```\n\n"
    text += "信号在 t 收盘后可用，t+1 开盘进入、t+2 开盘退出。验证区间末两日保留为 purged。\n\n"
    text += "最终期数值读取与评估：0；test_freshness：unknown。数据注册、旧 reuse/final-test guard 和工作台接线尚未完成。\n\n"
    text += f"实际结果 digest：`{report['result_digest']}`。输入、完整配置、代码和目录身份见 manifest.json。\n"
    return report, text


def _manifest(directory, identity, *, status, attempt_id):
    files = {p.name: hashlib.sha256(p.read_bytes()).hexdigest()
             for p in sorted(directory.iterdir()) if p.is_file() and p.name != "manifest.json"}
    value = {"schema_version": 1, "run_version": RUN_VERSION, "status": status,
             "scope": "engineering_fixture_development_validation" if status == "completed" else "failed_development_attempt",
             "attempt_id": attempt_id, "identity": identity,
             "files": files, "test_freshness": "unknown", "real_source_ready": False,
             "real_execution_readiness": "pending", "registered": False,
             "legacy_guard_connected": False, "model_calls": 0,
             "final_numeric_rows_parsed": 0}
    value["manifest_digest"] = digest(value)
    atomic_json(directory / "manifest.json", value)
    return value


def run_daily_development(contract_path, csv_path, receipt_path, out_dir, *, allow_whole_byte_hash=False):
    """Create one new attempt from freshly checked files; retain every failure.

    Only engineering fixtures may run. Raw-file hashing requires an explicit
    boolean permission; rejected inputs are never reopened for diagnostics.
    ``out_dir`` must be absent, so no existing research record is overwritten.
    """
    requested = Path(out_dir)
    require(".." not in requested.parts, "Output path must not contain parent traversal")
    directory = requested.absolute()
    require(not any(p.is_symlink() for p in (directory, *directory.parents)),
            "Output path and parents must not be symbolic links")
    if directory.exists():
        raise FileExistsError("Attempt output directory must not already exist")
    directory.mkdir(parents=True, exist_ok=False)
    attempt = {"schema_version": 1, "attempt_id": str(uuid.uuid4()), "run_version": RUN_VERSION,
               "created_at_utc": _now(), "status": "running", "phase": "precheck",
               "model_calls": 0, "final_numeric_rows_parsed": 0,
               "whole_byte_integrity_permission": allow_whole_byte_hash,
               "test_freshness": "unknown", "real_source_ready": False,
               "real_execution_readiness": "pending"}
    identity = {"contract_file_sha256": "not_read", "receipt_file_sha256": "not_read",
                "raw_sha256": "not_read", "config_digest": "unknown",
                "catalog_digest": "unknown", "code_digest": "unknown"}
    atomic_json(directory / "attempt.json", attempt)
    try:
        # Deliberately no API accepting a DevelopmentView or preflight assertion.
        view = precheck_development(contract_path, csv_path, receipt_path,
                                    allow_whole_byte_hash=allow_whole_byte_hash,
                                    required_fields=("open", "high", "low", "close"))
        preflight = view.report
        identity.update(preflight["identity"])
        identity.update(preflight["inputs"])
        identity["adapter_code_digest"] = preflight["identity"]["code_digest"]
        identity["origin"] = view.contract.to_dict()["origin"]
        atomic_json(directory / "preflight.json", preflight)
        require(view.contract.to_dict()["origin"] == "engineering_fixture",
                "Only engineering_fixture execution is allowed; real-source execution remains pending")
        panels = view.panels
        require(preflight["evaluation_asset_eligibility"], "At least three assets are required for evaluation")
        # The shared full public catalog is never augmented with private paths.
        from .server.provider_catalog import catalog_from_daily_contract
        catalog = catalog_from_daily_contract(view.contract)
        config = view.contract.to_dict()["evaluation"]
        code = research_code_identity()
        candidate = {"schema_version": 1, "id": "fixed-alpha101-101", "expression": EXPRESSION_101,
                     "source_reference": deepcopy(SOURCE_REFERENCE),
                     "origin": "maintained_paper_formula", "project_modifications": [
                         "Static three-asset engineering fixture, development-prefix validation, next-open labels and gross rank portfolio; not original-paper reproduction."],
                     "model_speculation": [],
                     "validation": validate_expression(EXPRESSION_101, set(panels))}
        identity.update(catalog_digest=catalog["catalog_digest"], code_digest=code["digest"],
                        candidate_digest=digest(candidate), config_digest=digest(config))
        atomic_json(directory / "inputs.json", {
            "schema_version": 1, "paths": {"contract": str(Path(contract_path).absolute()),
            "csv": str(Path(csv_path).absolute()), "source_receipt": str(Path(receipt_path).absolute())},
            "input_digests": deepcopy(preflight["inputs"]), "read_scope": deepcopy(preflight["read_scope"]),
            "note": "Original bytes remain external and pinned; development_input.csv preserves only parsed float64 development OHLCV. The opaque final tail is not numerically archived or parsed."})
        records = [{"date": date, "asset": asset, **{field: float(panels[field].loc[date, asset])
                   for field in ("open", "high", "low", "close", "volume")}}
                   for date in panels["open"].index for asset in panels["open"].columns]
        pd.DataFrame(records).to_csv(directory / "development_input.csv", index=False,
                                     date_format="%Y-%m-%d", float_format="%.17g")
        for name, value in (("contract", view.contract.to_dict()), ("config", config),
                            ("catalog", catalog), ("candidate", candidate), ("code", code)):
            atomic_json(directory / f"{name}.json", value)
        attempt.update(phase="compute")
        atomic_json(directory / "attempt.json", attempt)
        factor = compute_expression(EXPRESSION_101, panels)
        factor_record = _factor_record(factor)
        atomic_json(directory / "factor.json", factor_record)
        factor.to_csv(directory / "factor.csv", index_label="date", float_format="%.17g")
        identity["factor_digest"] = digest(factor_record)
        attempt.update(phase="validation")
        atomic_json(directory / "attempt.json", attempt)
        result = evaluate_development(factor, panels, config, view.contract.calendar)
        require(research_code_identity() == code, "Code changed during the attempt; output is not accepted")
        atomic_json(directory / "evaluation.json", result)
        identity["evaluation_digest"] = digest(result)
        identity["evaluation_file_sha256"] = hashlib.sha256((directory / "evaluation.json").read_bytes()).hexdigest()
        report, markdown = _report(result, candidate, identity)
        atomic_json(directory / "report.json", report)
        (directory / "report.md").write_text(markdown, encoding="utf-8")
        attempt.update(status="completed", phase="completed", completed_at_utc=_now(),
                       evaluation_status=result["status"], identity=identity)
        atomic_json(directory / "attempt.json", attempt)
        return _manifest(directory, identity, status="completed", attempt_id=attempt["attempt_id"])
    except (ValueError, TypeError, OSError) as exc:
        attempt.update(status="failed", completed_at_utc=_now(), identity=identity,
                       diagnostics={"code": getattr(exc, "code", "development_run_error"),
                                    "message": str(exc) if not isinstance(exc, OSError) else "Input or output could not be accessed",
                                    "location": getattr(exc, "location", None),
                                    "samples": getattr(exc, "samples", [])})
        # Retain already written records only; never re-open rejected inputs.
        atomic_json(directory / "attempt.json", attempt)
        return _manifest(directory, identity, status="failed", attempt_id=attempt["attempt_id"])
