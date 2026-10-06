"""Immutable local MOM-only industry research, separate from fixture services.

One fixed retrospective development contract, one attempt per output directory.
No network, final-test execution, human judgment, model call or workspace writes.
"""
from __future__ import annotations

import argparse
from datetime import datetime, timezone
import hashlib
from importlib.metadata import version
import json
from pathlib import Path, PurePosixPath
import platform
import re
import stat
import subprocess
import sys
import unicodedata

from . import french_industry, mom_only, mom_only_reference
from .storage import atomic_json, digest, read_json

ROOT = Path(__file__).resolve().parents[1]
SEMANTICS_VERSION = "mom-only-local-workflow-v1"
MAX_JSON_BYTES = 1024 * 1024
MAX_ARCHIVE_BYTES = 8 * 1024 * 1024
MAX_OUTPUT_BYTES = 64 * 1024 * 1024
PAPER_SHA256 = "07b8dad425b23328588a9668e8fccb58f18f79a4d1665d8fc49680198b0fbc2e"
SUPPORTED_METHOD_DIGEST = "405a947b3d9f6f2978c386a0dea5a83242837e0a08a19a1af127d394316ca8de"
SOURCE_FILES = (
    "paper_alpha/__init__.py",
    "paper_alpha/mom_only_workflow.py", "paper_alpha/mom_only.py",
    "paper_alpha/mom_only_reference.py", "paper_alpha/french_industry.py",
    "paper_alpha/monthly_evaluation.py", "paper_alpha/research_protocol.py",
    "paper_alpha/storage.py", "pyproject.toml", "requirements-lock.txt",
    "scripts/demo_mom_only.py", "docs/research/mom_only_contract.json",
    "examples/mom_only_industry/config.json",
)


def _now():
    return datetime.now(timezone.utc).isoformat()


def _bytes(path, limit):
    path = Path(path)
    info = path.lstat()
    if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1 or info.st_size > limit:
        raise ValueError("Input must be a bounded independent regular file")
    value = path.read_bytes()
    if len(value) > limit:
        raise ValueError("Input changed beyond its size limit")
    return value


def _sha(path, limit=MAX_OUTPUT_BYTES):
    return hashlib.sha256(_bytes(path, limit)).hexdigest()


def _json(path, limit=MAX_JSON_BYTES):
    _bytes(path, limit)
    return read_json(path)


def _copy(path, target, limit):
    value = _bytes(path, limit)
    target.parent.mkdir(parents=True, exist_ok=True)
    with target.open("xb") as stream:
        stream.write(value)
        stream.flush()
    expected = hashlib.sha256(value).hexdigest()
    if _sha(target, limit) != expected or _sha(path, limit) != expected:
        raise ValueError("Input changed while snapshotting")
    return expected


def _environment():
    # A commit identifies the base checkout; a dirty tree is recorded explicitly.
    def git(*args):
        completed = subprocess.run(["git", "-C", str(ROOT), *args], capture_output=True,
                                   text=True, timeout=5, check=False)
        return completed.stdout.strip() if completed.returncode == 0 else None
    repository = git("rev-parse", "--show-toplevel")
    owned_checkout = repository is not None and Path(repository).resolve() == ROOT.resolve()
    status = git("status", "--porcelain") if owned_checkout else None
    return {"python": platform.python_version(), "platform": platform.platform(),
            "packages": {"pypdf": version("pypdf")},
            "git_commit": git("rev-parse", "HEAD") if owned_checkout else None,
            "git_dirty": bool(status) if status is not None else None,
            "code_identity": "Exact saved file digests identify computation code; commit alone is insufficient."}


def _code_snapshot(out):
    inventory = {}
    for relative in SOURCE_FILES:
        inventory[relative] = _copy(ROOT / relative, out / "source" / relative, MAX_JSON_BYTES)
    return inventory


def _code_fence(inventory):
    if any(_sha(ROOT / name, MAX_JSON_BYTES) != expected for name, expected in inventory.items()):
        raise ValueError("Computation code changed during the attempt")


def _validate_method(method, config, source_hash):
    if (not isinstance(method, dict) or method.get("kind") != "mom_only_method_contract"
            or type(method.get("schema_version")) is not int or method["schema_version"] != 1
            or method.get("contract_version") != "gjs-mom-only-method-evidence-v1"
            or method.get("human_review") is not None or method.get("semantic_quality_score") is not None
            or method.get("paper_returns_reproduced") is not False
            or method.get("ai_interface_enabled") is not False
            or method.get("paper", {}).get("document_sha256") != PAPER_SHA256
            or method.get("contract_digest") != SUPPORTED_METHOD_DIGEST
            or method.get("contract_digest") != digest({k: v for k, v in method.items() if k != "contract_digest"})):
        raise ValueError("Method evidence does not match the declared fixed research boundary")
    signal = method.get("paper_signal", {})
    policy = method.get("input_and_missing_policy", {})
    if (signal.get("formation_offsets") != list(range(-12, -1)) or signal.get("skipped_offsets") != [-1]
            or policy.get("fill_policy") != "none" or policy.get("all_history_finite") is not True):
        raise ValueError("Method evidence changes the fixed MOM formation rules")
    defaults = method.get("illustrative_defaults", {})
    if (defaults.get("development", {}).get("start") != config["development"]["start"]
            or defaults.get("development", {}).get("end") != config["development"]["end"]
            or defaults.get("reserved", {}).get("start") != config["reserved"]["start"]
            or defaults.get("reserved", {}).get("end") != config["reserved"]["end"]
            or defaults.get("minimum_formation_assets") != config["min_formation_assets"]
            or defaults.get("source_candidate", {}).get("raw_sha256") != source_hash):
        raise ValueError("Method evidence is bound to another source or development contract")
    if method.get("legacy_frozen_plan", {}).get("new_industry_example_can_unlock_original_plan") is not False:
        raise ValueError("The industry example cannot unlock the original author-source plan")


def _evidence_snapshot(out, method, evidence_dir=None):
    paper = method["paper"]
    if paper["path"] != "artifacts/research-momentum-01/paper.pdf":
        raise ValueError("Unexpected local evidence path")
    evidence = Path(evidence_dir) if evidence_dir is not None else None
    original_paper = evidence / "paper.pdf" if evidence is not None else ROOT / paper["path"]
    actual = _copy(original_paper, out / "inputs/paper.pdf", 8 * 1024 * 1024)
    if actual != PAPER_SHA256:
        raise ValueError("Original paper bytes differ from the method evidence")
    seen = set()
    for source in method["sources"]:
        relative = source["path"]
        if (not re.fullmatch(r"artifacts/research-momentum-01/author-code/[A-Za-z0-9]+\.m", relative)
                or relative in seen):
            raise ValueError("Unexpected or duplicate author evidence path")
        seen.add(relative)
        original_code = evidence / "author-code" / Path(relative).name if evidence is not None else ROOT / relative
        actual = _copy(original_code, out / "inputs/author-code" / Path(relative).name, MAX_JSON_BYTES)
        if actual != source["sha256"] or source["executed"] is not False:
            raise ValueError("Author source evidence changed or was marked executed")


def _verify_paper_anchors(out, method):
    from pypdf import PdfReader
    def normalize(text):
        return re.sub(r"\s+", " ", unicodedata.normalize("NFKC", text)).strip()
    reader = PdfReader(out / "inputs/paper.pdf")
    pages = {}
    anchors = method.get("evidence")
    if not isinstance(anchors, list) or not 1 <= len(anchors) <= 16:
        raise ValueError("Bounded paper evidence is required")
    for anchor in anchors:
        page = anchor["page_1_based"]
        if type(page) is not int or not 1 <= page <= len(reader.pages):
            raise ValueError("Paper evidence page is invalid")
        if page not in pages:
            pages[page] = normalize(reader.pages[page - 1].extract_text() or "")
        start, end = anchor["normalized_start"], anchor["normalized_end_exclusive"]
        if (type(start) is not int or type(end) is not int or not 0 <= start < end <= len(pages[page])
                or anchor["document_sha256"] != PAPER_SHA256
                or pages[page][start:end] != normalize(anchor["quote"])):
            raise ValueError("Saved paper quote does not match its exact fixed page and span")


def _validate_receipt(receipt, source_hash):
    if receipt is None:
        return
    if not isinstance(receipt, dict) or not isinstance(receipt.get("files"), list):
        raise ValueError("Download receipt must contain the actual recorded file inventory")
    selected = [item for item in receipt["files"] if isinstance(item, dict)
                and item.get("url") == mom_only.SOURCE_URL]
    if len(selected) != 1 or selected[0].get("sha256") != source_hash:
        raise ValueError("Download receipt does not bind the exact archive and source URL")


def _inventory(out):
    files, size = {}, 0
    for path in sorted(out.rglob("*")):
        info = path.lstat()
        if stat.S_ISDIR(info.st_mode):
            continue
        if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1:
            raise ValueError("Unsafe output file")
        size += info.st_size
        if size > MAX_OUTPUT_BYTES or len(files) >= 64:
            raise ValueError("Output inventory exceeds budget")
        if path.name != "manifest.json" or path.parent != out:
            files[str(path.relative_to(out))] = _sha(path)
    return files


def _manifest(out, state):
    request = _json(out / "input.json") if (out / "input.json").exists() else None
    result = _json(out / "result.json") if (out / "result.json").exists() else None
    value = {"schema_version": 1, "semantics_version": SEMANTICS_VERSION,
             "status": state["status"], "input_digest": digest(request) if request else None,
             "invocation_digest": digest(_json(out / "invocation.json")),
             "result_digest": digest(result) if result else None, "files": _inventory(out)}
    atomic_json(out / "manifest.json", value)


def render_report(result, reference, request):
    """Format saved tool outputs without calculating a financial statistic."""
    def number(value):
        return "unavailable" if value is None else format(value, ".17g")
    lines = ["# MOM-only industry development experiment", "",
             "Independent project modification using published industry-portfolio returns.",
             "Not a GJS individual-stock paper reproduction, investable strategy or human/model quality result.", "",
             "Signal: eleven natural monthly returns H-12..H-2; skip H-1; complete history; no fill.",
             "Development: 2010-01..2011-12. Reserved: 2012-01..2013-12; its numeric labels are not parsed or evaluated.",
             "The downloaded archive may contain later bytes. This current-vintage historical example is retrospective, not an as-of or fully blind test.",
             "Industry returns are published value-weighted portfolio returns; the project allocates weights across those portfolios.",
             "Raw percentage-to-decimal conversion is an explicit source contract. The official 49-industry detail page does not separately state units; supporting checks do not certify daily/monthly equivalence.",
             "MOM has gross exposure 1 / net exposure 0. The same-formation-sample long-only baseline has gross 1 / net 1.",
             "Comparisons have different net exposures and do not establish risk-adjusted alpha.",
             "Only gross returns are evaluated; costs, borrowing, financing, implementation and intramonth holdings drift are not modeled.", "",
             f"Source archive SHA256: {request['source_sha256']}",
             f"Input digest: {digest(request)}",
             f"Result digest: {digest(result)}",
             f"Independent numerical reference passed: {reference.get('passed')}", "",
             "## Computed summaries", "",
             "Returns are decimal fractions. Annualized mean/volatility assumes risk-free return zero.", "",
             "| Strategy | Available months | Mean monthly gross | Annualized sample volatility | Annualized mean/volatility, zero Rf | Complete gross return index | Max gross drawdown |",
             "|---|---|---|---|---|---|---|"]
    for item in result["summary"]:
        lines.append("| " + " | ".join([
            item["strategy_id"], f"{item['months_evaluated']}/{item['months_total']}",
            number(item["mean_gross_return"]), number(item["annualized_sample_volatility"]),
            number(item["annualized_mean_over_volatility_zero_rf"]),
            number(item["terminal_gross_return_index"]), number(item["max_gross_drawdown"]),
        ]) + " |")
    lines.extend(["", "## Same-month arithmetic comparison", "", "```json",
                  json.dumps(result["paired_comparison"], ensure_ascii=False, sort_keys=True, indent=2, allow_nan=False),
                  "```", "", "## Monthly tool outputs", "",
                  "| Month | Formation assets | Strategy | Gross return | Gross return index | Reasons |",
                  "|---|---|---|---|---|---|"])
    for month in result["months"]:
        for item in month["strategies"]:
            reasons = "; ".join(item["reasons"]).replace("|", "\\|").replace("\n", " ")
            lines.append("| " + " | ".join([month["month"], str(len(month["eligible_assets"])),
                item["id"], number(item["gross_return"]), number(item["gross_return_index"]), reasons]) + " |")
    lines.extend(["", "## Exact records", "",
             "All signals, formation exclusions, label availability and frozen weights are in result.json.",
             "Source conversion missing markers and omitted months are in diagnostics.json; null is never filled with zero.", "",
             "## Research evidence and review", "",
             "The saved method.json records paper evidence, project choices and remaining scope limitations.",
             "Source receipt fields, when present, are local download records rather than authenticated ownership or a redistribution license.",
             "No human judgments were created. Existing Alpha101 judgments and the blocked author-source plan are unchanged.", ""])
    return "\n".join(lines)


def run(source_path, config_path, method_path, output_dir, expected_sha256, source_receipt_path=None, evidence_dir=None):
    """Exclusive output creation; interrupted or failed attempts are never reused."""
    if not isinstance(expected_sha256, str) or not re.fullmatch(r"[0-9a-f]{64}", expected_sha256):
        raise ValueError("A lowercase SHA256 of the exact source archive is required")
    out = Path(output_dir).absolute()
    if out.resolve() != out:
        raise ValueError("Output directory must not traverse symlinks")
    out.mkdir(parents=True, exist_ok=False)
    state = {"schema_version": 1, "semantics_version": SEMANTICS_VERSION, "status": "running",
             "started_at": _now(), "finished_at": None, "phase": "snapshot",
             "attempts": 1, "human_judgments": None, "semantic_quality_score": None,
             "error": None, "events": []}
    invocation = {"schema_version": 1, "semantics_version": SEMANTICS_VERSION,
                  "source_path": str(Path(source_path).absolute()),
                  "expected_source_sha256": expected_sha256,
                  "config_path": str(Path(config_path).absolute()),
                  "method_path": str(Path(method_path).absolute()),
                  "source_receipt_path": str(Path(source_receipt_path).absolute()) if source_receipt_path is not None else None,
                  "evidence_dir": str(Path(evidence_dir).absolute()) if evidence_dir is not None else None,
                  "scope": "Recorded local invocation declarations; paths are not source authentication."}
    atomic_json(out / "invocation.json", invocation)
    atomic_json(out / "state.json", state)
    try:
        atomic_json(out / "environment.json", _environment())
        code = _code_snapshot(out)
        _copy(Path(config_path), out / "inputs" / "config.json", MAX_JSON_BYTES)
        _copy(Path(method_path), out / "inputs" / "method.json", MAX_JSON_BYTES)
        source_hash = _copy(Path(source_path), out / "inputs" / "source.zip", MAX_ARCHIVE_BYTES)
        if source_hash != expected_sha256:
            raise ValueError("Source archive differs from the expected SHA256")
        config = _json(out / "inputs" / "config.json")
        mom_only.validate_config(config)
        method = _json(out / "inputs" / "method.json")
        _validate_method(method, config, source_hash)
        _evidence_snapshot(out, method, evidence_dir)
        _verify_paper_anchors(out, method)
        receipt = None
        if source_receipt_path is not None:
            _copy(Path(source_receipt_path), out / "inputs" / "source_receipt.json", MAX_JSON_BYTES)
            receipt = _json(out / "inputs" / "source_receipt.json")
        _validate_receipt(receipt, source_hash)
        request = {"schema_version": 1, "config": config, "method": method,
                   "source_sha256": source_hash, "source_receipt": receipt,
                   "source_receipt_scope": "local_record_not_authenticated_license_or_identity",
                   "code_sha256": code}
        atomic_json(out / "input.json", request)
        diagnostics = {}
        panel = french_industry.parse_archive(out / "inputs" / "source.zip", "2009-01", "2011-12",
                                             expected_sha256, diagnostics_out=diagnostics)
        atomic_json(out / "panel.json", panel)
        atomic_json(out / "diagnostics.json", diagnostics)
        state["phase"] = "calculate"
        state["events"].append({"phase": "source_parsed", "at": _now(), "panel_digest": digest(panel)})
        atomic_json(out / "state.json", state)
        result = mom_only.evaluate(panel, config)
        atomic_json(out / "result.json", result)
        reference = mom_only_reference.check(panel, config, result)
        atomic_json(out / "reference.json", reference)
        if reference.get("supported") is not True or reference.get("passed") is not True or reference.get("issues"):
            raise ValueError("Independent numerical reference rejected the calculation")
        (out / "report.md").write_text(render_report(result, reference, request), encoding="utf-8")
        _code_fence(code)
        state.update(status="completed", phase="completed", finished_at=_now())
        state["events"].append({"phase": "independent_reference_passed", "at": _now(), "result_digest": digest(result)})
        atomic_json(out / "state.json", state)
        _manifest(out, state)
        return verify(out)
    except Exception as exc:
        state.update(status="failed", phase="failed", finished_at=_now(),
                     error={"type": type(exc).__name__, "message": str(exc)[:2000]})
        state["events"].append({"phase": "failed", "at": _now()})
        atomic_json(out / "state.json", state)
        _manifest(out, state)
        raise


def verify(output_dir):
    """Check the artifact, reparse bounded raw data and check the saved calculation."""
    out = Path(output_dir).absolute()
    if out.resolve() != out or not out.is_dir():
        raise ValueError("Output directory is missing or unsafe")
    manifest = _json(out / "manifest.json")
    if (not isinstance(manifest, dict) or set(manifest) != {"schema_version", "semantics_version", "status", "input_digest", "invocation_digest", "result_digest", "files"}
            or type(manifest["schema_version"]) is not int or manifest["schema_version"] != 1
            or manifest["semantics_version"] != SEMANTICS_VERSION):
        raise ValueError("Unsupported MOM-only manifest")
    files = manifest["files"]
    if not isinstance(files, dict) or not files or len(files) > 64:
        raise ValueError("Invalid file inventory")
    for name, value in files.items():
        if (not isinstance(name, str) or PurePosixPath(name).is_absolute() or "\\" in name
                or any(part in {"", ".", ".."} for part in name.split("/"))
                or not isinstance(value, str) or not re.fullmatch(r"[0-9a-f]{64}", value)):
            raise ValueError("Unsafe manifest path or SHA256")
    if _inventory(out) != files:
        raise ValueError("Artifact inventory or bytes changed")
    if "invocation.json" not in files or digest(_json(out / "invocation.json")) != manifest["invocation_digest"]:
        raise ValueError("Saved invocation declarations changed")
    state = _json(out / "state.json")
    if state["status"] != manifest["status"] or state["human_judgments"] is not None or state["semantic_quality_score"] is not None:
        raise ValueError("Saved attempt status differs from the workflow boundary")
    if state["status"] == "failed":
        return {"verified": True, "status": "failed", "calculation_verified": False,
                "error": state["error"], "human_review": "pending"}
    if state["status"] != "completed":
        raise ValueError("Attempt is incomplete; retain it and use a new directory")
    required = {"input.json", "result.json", "reference.json", "report.md", "panel.json",
                "diagnostics.json", "environment.json", "state.json", "invocation.json",
                "inputs/source.zip", "inputs/config.json", "inputs/method.json", "inputs/paper.pdf"}
    required.update("source/" + name for name in SOURCE_FILES)
    if not required <= set(files):
        raise ValueError("Completed artifact is missing required records")
    request, result = _json(out / "input.json"), _json(out / "result.json")
    config, method = _json(out / "inputs/config.json"), _json(out / "inputs/method.json")
    mom_only.validate_config(config)
    _validate_method(method, config, request["source_sha256"])
    if _sha(out / "inputs/paper.pdf", 8 * 1024 * 1024) != PAPER_SHA256:
        raise ValueError("Saved original paper differs from the method evidence")
    _verify_paper_anchors(out, method)
    for source in method["sources"]:
        name = "inputs/author-code/" + Path(source["path"]).name
        if name not in files or files[name] != source["sha256"] or source["executed"] is not False:
            raise ValueError("Saved author evidence differs from its method reference")
    if (request["schema_version"] != 1 or request["config"] != config or request["method"] != method
            or digest(request) != manifest["input_digest"] or digest(result) != manifest["result_digest"]
            or request["source_sha256"] != _sha(out / "inputs/source.zip", MAX_ARCHIVE_BYTES)
            or set(request["code_sha256"]) != set(SOURCE_FILES)
            or any(files["source/" + name] != sha for name, sha in request["code_sha256"].items())):
        raise ValueError("Saved request, source or result binding changed")
    receipt_path = out / "inputs/source_receipt.json"
    if receipt_path.exists() != (request["source_receipt"] is not None):
        raise ValueError("Saved source receipt membership changed")
    if receipt_path.exists() and _json(receipt_path) != request["source_receipt"]:
        raise ValueError("Saved source receipt changed")
    _validate_receipt(request["source_receipt"], request["source_sha256"])
    diagnostics = {}
    panel = french_industry.parse_archive(out / "inputs/source.zip", "2009-01", "2011-12",
                                         request["source_sha256"], diagnostics_out=diagnostics)
    if panel != _json(out / "panel.json") or diagnostics != _json(out / "diagnostics.json"):
        raise ValueError("Saved conversion does not match the exact raw source")
    reference = mom_only_reference.check(panel, config, result)
    if (reference != _json(out / "reference.json") or reference.get("supported") is not True
            or reference.get("passed") is not True or reference.get("issues")):
        raise ValueError("Independent numerical verification failed")
    if (out / "report.md").read_text(encoding="utf-8") != render_report(result, reference, request):
        raise ValueError("Report differs from saved calculation outputs")
    return {"verified": True, "status": "completed", "calculation_verified": True,
            "input_digest": digest(request), "result_digest": digest(result),
            "reference_passed": True, "reserved_evaluated": False, "human_review": "pending"}


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)
    execute = commands.add_parser("run", help="One immutable fixed development attempt; no download")
    execute.add_argument("--source", type=Path, required=True)
    execute.add_argument("--source-sha256", required=True)
    execute.add_argument("--source-receipt", type=Path)
    execute.add_argument("--evidence-dir", type=Path, help="Saved inputs directory for a replay using frozen PDF/author files")
    execute.add_argument("--config", type=Path, default=ROOT / "examples/mom_only_industry/config.json")
    execute.add_argument("--method", type=Path, default=ROOT / "docs/research/mom_only_contract.json")
    execute.add_argument("--out", type=Path, required=True)
    inspect = commands.add_parser("verify", help="Read-only raw-source/result/report verification")
    inspect.add_argument("--out", type=Path, required=True)
    args = parser.parse_args(argv)
    try:
        if args.command == "run":
            result = run(args.source, args.config, args.method, args.out, args.source_sha256, args.source_receipt, args.evidence_dir)
        else:
            result = verify(args.out)
        print(json.dumps(result, ensure_ascii=False, sort_keys=True, allow_nan=False))
        return 0
    except (OSError, ValueError, KeyError, TypeError) as exc:
        print(json.dumps({"status": "error", "type": type(exc).__name__, "message": str(exc)[:2000]},
                         ensure_ascii=False, sort_keys=True), file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
