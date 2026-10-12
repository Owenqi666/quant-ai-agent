"""Deterministic bilingual reading view over a verified frozen report only."""
from collections import Counter
import html
import json
import math
import re

from ..contracts import ENGINEERING_ONLY_ASSUMPTION
from ..storage import digest

RENDERER_VERSION = 1
DETAIL_LIMIT = 10
FEEDBACK_LIMIT = 20
METRICS = {
    "mean_rank_ic": ("平均 Rank IC（无量纲）", "Mean Rank IC (dimensionless)"),
    "mean_gross_return": ("平均日毛收益（小数，未计成本）", "Mean daily gross return (decimal, before costs)"),
    "sum_gross_return": ("日毛收益之和（小数，非复利）", "Sum of daily gross returns (decimal, not compounded)"),
    "factor_coverage": ("因子覆盖率（0–1）", "Factor coverage (0–1)"),
    "evaluated_days": ("有效评估日数", "Evaluated days"),
    "eligible_days": ("可评估日数", "Eligible days"),
    "rank_ic_days": ("有效 Rank IC 日数", "Rank IC days"),
    "purged_days": ("边界剔除日数", "Purged boundary days"),
    "skipped_days": ("跳过日数", "Skipped days"),
}
SOURCES = {"human": ("人工（申报来源）", "Human (declared source)"),
    "automation": ("自动化／代理", "Automation / agent"),
    "imported": ("导入记录", "Imported record"), "legacy_unknown": ("历史来源未知", "Legacy source unknown")}
CATEGORIES = {"evidence": ("证据", "Evidence"), "hypothesis": ("假说", "Hypothesis"),
    "implementation": ("实现", "Implementation"), "data": ("数据", "Data"),
    "evaluation": ("评估", "Evaluation"), "other": ("其他", "Other")}
DIMENSIONS = {"evidence_accuracy": ("证据准确性", "Evidence accuracy"),
    "hypothesis_fidelity": ("假说忠实度", "Hypothesis fidelity"),
    "mechanism_attribution": ("机制归因", "Mechanism attribution"),
    "field_semantics": ("字段语义", "Field semantics"),
    "implementation_alignment": ("实现一致性", "Implementation alignment")}
STATUS = {"passed": ("通过", "Passed"), "failed": ("失败", "Failed"),
    "not_assessed": ("未评定", "Not assessed"), "not_applicable": ("不适用", "Not applicable"),
    "accepted": ("接受", "Accepted"), "needs_changes": ("需修改", "Needs changes"),
    "rejected": ("拒绝", "Rejected")}
ATTRIBUTION = {"paper_original": ("论文原文", "Paper original"),
    "model_conjecture": ("模型推测", "Model conjecture"),
    "user_modification": ("用户改动", "User modification"), "unresolved": ("未知", "Unknown")}


def render_readable_report(report, *, locale="zh", renderer_version=1):
    if locale not in {"zh", "en"} or type(renderer_version) is not int or renderer_version not in {RENDERER_VERSION, 2}:
        raise ValueError("Unsupported readable report locale or renderer version")
    payload = report["payload"]
    if digest(payload) != report["payload_digest"]:
        raise ValueError("Frozen report payload digest mismatch")
    index = 0 if locale == "zh" else 1
    def t(zh, en):
        return (zh, en)[index]
    missing = t("未提供", "Not provided")
    def text(value):
        if value is None or value == "":
            return missing
        if isinstance(value, (dict, list)):
            value = json.dumps(value, ensure_ascii=False, sort_keys=True)
        value = " ".join(str(value).splitlines())
        value = "".join(character if ord(character) >= 32 else " " for character in value)
        pattern = r"([\\*_\[\]{}()#!|~" + chr(96) + r"])"
        return re.sub(pattern, r"\\\1", html.escape(value, quote=True))
    def label(mapping, value):
        pair = mapping.get(value)
        return pair[index] + " (" + value + ")" if pair else value
    def number(value):
        if isinstance(value, bool) or not isinstance(value, (float, int)) or not math.isfinite(value):
            return missing
        return json.dumps(value, allow_nan=False)
    lines = []
    def line(value=""):
        lines.append(value)
    def section(zh, en):
        line(); line("## " + t(zh, en)); line()
    def row(name, value):
        line("- " + name + ": " + text(value))
    def formula(name, value):
        """Keep original bytes literal; mathematical notation is derived display only."""
        line("- " + name + ":")
        if not isinstance(value, str) or value == "":
            line(); line(missing); line()
            return
        # A fence longer than every untrusted backtick run cannot be closed by
        # the expression, even when it contains newlines or embedded HTML.
        fence = "`" * max(3, 1 + max((len(part) for part in re.findall(r"`+", value)), default=0))
        line(); line(fence + "text"); line(value); line(fence); line()
        from ..formula_presentation import expression_to_latex
        latex = expression_to_latex(value)
        if latex is None:
            line(t("未生成数学展示；原始表达式完整保留。",
                   "No mathematical display was generated; the full original expression is retained."))
            return
        line(t("派生数学展示（不用于执行；未验证研究语义）：",
               "Derived mathematical display (not executable; research semantics are unverified):"))
        line(); line("$$"); line(latex); line("$$"); line()

    def indexed(items, pointer, limit=DETAIL_LIMIT):
        line(t(f"详情展示 {min(len(items), limit)} / {len(items)} 项；完整索引：",
               f"Details shown: {min(len(items), limit)} / {len(items)}; complete index: ") + text(pointer))
        return enumerate(items[:limit])

    line("# " + t("研究阅读报告", "Research reading report")); line()
    row(t("研究", "Research"), payload.get("research", {}).get("title"))
    row(t("报告标识", "Report ID"), report.get("id"))
    row(t("冻结时间", "Frozen at"), payload.get("snapshot_at"))
    row(t("内容摘要", "Payload SHA-256"), report["payload_digest"])
    row(t("渲染版本／语言", "Renderer version / locale"), str(renderer_version) + " / " + locale)
    line(t("本阅读版仅解释已保存的快照；不调用模型、不重新计算指标，也不构成人工语义认可。",
           "This view reads the saved snapshot only; it makes no model calls, recomputes no metrics and supplies no human semantic approval."))

    section("范围与数据", "Scope and data")
    counts = payload.get("counts", {})
    for key in ("selected_runs", "total_research_runs", "unselected_runs", "revisions", "candidates",
                "candidate_attempts", "run_attempts", "run_events", "reviews", "issues", "unreviewed_current_candidates"):
        row(key, counts.get(key))
    if 'pending_human_candidates' in counts:
        row(t("待人工质量审核的候选数", "Candidates pending human quality review"), counts['pending_human_candidates'])
    dataset = payload.get("dataset", {})
    row(t("数据名称", "Dataset"), dataset.get("title"))
    row(t("数据类型", "Data kind"), dataset.get("metadata", {}).get("data_kind"))
    row(t("数据版本指纹", "Dataset fingerprint"), dataset.get("sha256"))
    row(t("论文", "Paper"), payload.get("paper", {}).get("title"))
    row(t("论文 SHA-256", "Paper SHA-256"), payload.get("paper", {}).get("sha256"))
    line(t("最终测试区间保留；本报告不将合成数据结果解释为投资表现。完整日历和事件请查审计 JSON。",
           "Final test stays held out; synthetic-data results are not investment performance. Full calendars and events remain in the audit JSON."))

    section("研究问题与论文依据", "Research questions and paper evidence")
    stage_report = any('stage_provenance' in item for item in payload.get('runs', []))
    if stage_report:
        line(t("本节的论文引文、主张及原论文数字属于来源材料；本次计算指标仅来自下节绑定的工具结果。",
               "Paper quotations, claims and paper statistics in this section are source material; current calculation metrics come only from the bound tool results below."))
    revisions = payload.get("revisions", [])
    selected_revision_ids = {item.get("summary", {}).get("run", {}).get("revision_id") for item in payload.get("runs", [])}
    selected_revisions = [(i, revision) for i, revision in enumerate(revisions) if revision.get("id") in selected_revision_ids]
    line(t(f"本节展示所选实验对应的 {len(selected_revisions)} 个修订；全部 {len(revisions)} 个修订位于 /payload/revisions。",
           f"This section shows {len(selected_revisions)} revisions used by selected runs; all {len(revisions)} revisions are indexed at /payload/revisions."))
    for revision_index, revision in selected_revisions:
        line(); line("### " + t("修订 ", "Revision ") + text(revision.get("id")))
        task = revision.get("task", {})
        for i, hypothesis in indexed(task.get("hypotheses", []), f"/payload/revisions/{revision_index}/task/hypotheses"):
            row(t("研究主张", "Research claim"), hypothesis.get("claim"))
            row(t("主张来源", "Claim attribution"), label(ATTRIBUTION, hypothesis.get("attribution")))
            row(t("机制与解释", "Mechanism / interpretation"), hypothesis.get("economic_mechanism"))
            row(t("机制来源", "Mechanism attribution"), label(ATTRIBUTION, hypothesis.get("mechanism_attribution")))
            if stage_report and ENGINEERING_ONLY_ASSUMPTION in hypothesis.get("assumptions", []):
                row(t("验证范围", "Verification scope"),
                    t("仅公式工程验证；经济机制未决，研究有效性仍待评定。",
                      "Formula engineering verification only; economic mechanism unresolved and research validity unassessed."))
            row(t("信号方向", "Signal direction"), hypothesis.get("signal_direction"))
            row(t("所需字段", "Required fields"), ", ".join(hypothesis.get("required_fields", [])))
            row(t("证据 ID", "Evidence IDs"), ", ".join(hypothesis.get("evidence_ids", [])))
            row(t("适用条件", "Conditions"), " / ".join(hypothesis.get("assumptions", [])))
        for i, item in indexed(task.get("evidence", []), f"/payload/revisions/{revision_index}/task/evidence"):
            line("- " + text(item.get("id")) + " · " + t("第 ", "page ") + text(item.get("page")) + ": " + text(item.get("quote")))

    section("候选实现与实际结果", "Candidate implementations and recorded results")
    for run_index, item in enumerate(payload.get("runs", [])):
        summary = item.get("summary", {})
        run = summary.get("run", {})
        line(); line("### " + t("实验 ", "Experiment ") + text(run.get("id")))
        row(t("状态", "Status"), run.get("status"))
        row(t("修订", "Revision"), run.get("revision_id"))
        row(t("来源完整性（非语义认可）", "Source integrity (not semantic approval)"), summary.get("source_verified"))
        if summary.get("verification_error"):
            row(t("来源问题", "Source issue"), summary["verification_error"])
        if run.get("error"):
            row(t("运行错误", "Run error"), run["error"])
        stages = item.get('stage_provenance')
        if stages:
            line(); line("#### " + t("模型调用与采用来源", "Model calls and adoption provenance")); line()
            row(t("来源依据", "Provenance basis"), stages.get('basis'))
            line(t("调用记录来自已保存的主机收据；调用成功和采用记录均不等于人工质量认可。未知费用保持未知。",
                   "Calls come from saved host receipts; successful calls and adoption do not imply human quality approval. Unknown costs stay unknown."))
            for key, labels in (("extraction", ("材料／假说提取", "Material / hypothesis extraction")),
                                ("generation", ("候选生成", "Candidate generation"))):
                stage = stages.get(key, {})
                row(labels[index] + t("状态／来源", " status / source"),
                    str(stage.get('status', 'unknown')) + ' / ' + str(stage.get('execution_kind', 'unknown')))
                if stage.get('status') != 'recorded':
                    row(t("未记录原因", "Unrecorded reason"), stage.get('reason_code'))
                    continue
                receipt = stage['receipt']; usage = receipt.get('usage', {})
                row(t("Provider 配置／模式／传输", "Provider configuration / mode / transport"),
                    stage['provider_config_id'] + ' / ' + stage['provider_mode'] + ' / ' + stage['native_transport'])
                line(t("Native 进程调用收据与真实模型验收分开；需结合保留的官方传输身份和验收证据。",
                       "A native invocation receipt and real-model acceptance are separate; consult retained official transport identity and acceptance evidence."))
                row(t("提案／尝试", "Proposal / attempt"), stage['proposal_id'] + ' / ' + stage['attempt_id'])
                row(t("响应／收据摘要", "Response / receipt digest"),
                    stage['accepted_response_digest'] + ' / ' + stage['receipt_digest'])
                row(t("请求模型／实际报告模型", "Requested / reported model"),
                    str(receipt.get('requested_model') or t('未知', 'Unknown')) + ' / ' +
                    str(receipt.get('reported_model') or t('未知', 'Unknown')))
                for name in ('input_tokens', 'output_tokens', 'cached_input_tokens', 'model_calls', 'cost'):
                    row(name, usage.get(name) if usage.get(name) is not None else t('未知', 'Unknown'))
                row(t("调用耗时（秒）", "Invocation seconds"), receipt.get('elapsed_seconds'))
            adoption = stages.get('adoption', {})
            row(t("采用状态／执行者", "Adoption status / actor"),
                str(adoption.get('status', 'unknown')) + ' / ' + str(adoption.get('actor', 'unknown')))
            row(t("计算阶段 LLM 调用数", "Calculation-stage LLM calls"), stages.get('calculation', {}).get('llm_calls'))
            row(t("报告阶段 LLM 调用数", "Report-stage LLM calls"), stages.get('report', {}).get('llm_calls'))
            row(t("完整来源索引", "Complete provenance index"), f"/payload/runs/{run_index}/stage_provenance")
        evaluation = summary.get("evaluation", {})
        for name in ("train", "validation", "test"):
            split = evaluation.get("splits", {}).get(name, {})
            row(name + (t("（保留）", " (held out)") if name == "test" else ""),
                str(split.get("start") or missing) + " — " + str(split.get("end") or missing))
        row(t("最少有效资产数", "Minimum valid assets"), evaluation.get("min_assets"))
        semantics = summary.get("recorded_semantics") or {}
        row(t("算子语义版本", "Operator semantics version"), semantics.get("operator_semantics"))
        execution = semantics.get("execution_semantics") or {}
        for name in ("signal", "entry", "exit", "label", "costs", "tradability"):
            row(t("执行规则 ", "Execution rule ") + name, execution.get(name))
        row(t("完整执行规则索引", "Complete execution-rule index"), f"/payload/runs/{run_index}/summary/recorded_semantics")
        candidates = item.get("candidates", [])
        statuses = Counter(str(candidate.get("status", "unknown")) for candidate in candidates)
        row(t("全部候选状态计数（含失败）", "All candidate status counts (including failures)"), dict(sorted(statuses.items())))
        for candidate_index, candidate in indexed(candidates, f"/payload/runs/{run_index}/candidates"):
            pointer = f"/payload/runs/{run_index}/candidates/{candidate_index}"
            line(); line("#### " + text(candidate.get("id")))
            row(t("状态", "Status"), candidate.get("status"))
            if renderer_version == 1:
                row(t("公式", "Expression"), candidate.get("expression"))
            else:
                formula(t("候选声明的原始表达式", "Original declared candidate expression"), candidate.get("expression"))
                recorded = candidate.get("result")
                verified = summary.get("source_verified") is True and isinstance(candidate.get("metric_source"), dict)
                actual = recorded.get("expression") if verified and isinstance(recorded, dict) else None
                formula(t("已冻结结果中的实际执行表达式", "Executed expression in the frozen result"), actual)
                if actual is None:
                    line(t("快照未提供可验证的执行表达式；不会用候选声明替代。",
                           "The snapshot provides no verified executed expression; the candidate declaration is not substituted."))
            row(t("实现来源", "Implementation attribution"), label(ATTRIBUTION, candidate.get("origin")))
            row(t("失败或停止原因", "Failure or stop reason"), candidate.get("reason") or candidate.get("error"))
            attempts = candidate.get("attempts", [])
            row(t("候选尝试数", "Candidate attempts"), len(attempts))
            for attempt_index, attempt in indexed(attempts, pointer + "/attempts"):
                row(t("尝试状态／错误", "Attempt status / error"),
                    str(attempt.get("status", "")) + " / " + str(attempt.get("error") or attempt.get("reason") or ""))
            metric_source = candidate.get("metric_source")
            trustworthy = summary.get("source_verified") is True and isinstance(metric_source, dict)
            metrics = (candidate.get("result") or {}).get("metrics", {}) if trustworthy else {}
            line("| " + t("指标", "Metric") + " | " + t("记录值", "Recorded value") + " | " + t("快照位置", "Snapshot pointer") + " |")
            line("| --- | --- | --- |")
            for key, labels in METRICS.items():
                line("| " + labels[index] + " | " + number(metrics.get(key)) + " | " + text(pointer + "/result/metrics/" + key) + " |")
            other_metrics = sorted(set(metrics) - set(METRICS))
            if other_metrics:
                row(t("其余记录指标（完整索引见上）", "Other recorded metrics (see complete index above)"), ", ".join(other_metrics))
            row(t("原始指标文件 ID", "Original metric artifact ID"), (metric_source or {}).get("artifact_id"))
            row(t("原始结果摘要", "Original result digest"), (metric_source or {}).get("result_digest"))
            row(t("冻结结果日数", "Frozen daily-result count"), candidate.get("result_daily_count"))
            row(t("当前结果绑定的审核 ID", "Review IDs bound to this result"), ", ".join(candidate.get("current_review_ids", [])))
            if 'human_review' in candidate:
                human = candidate['human_review']
                row(t("人工质量审核状态", "Human quality review status"), human.get('status'))
                row(t("当前结果的人工审核 ID", "Human review IDs for this result"), ", ".join(human.get('human_review_ids', [])))
        line(t(f"运行尝试 {len(item.get('attempts', []))} 项、事件 {len(item.get('events', []))} 项；完整失败与恢复索引：",
               f"{len(item.get('attempts', []))} run attempts and {len(item.get('events', []))} events; complete failure and recovery index: ")
             + text(f"/payload/runs/{run_index}/attempts; /payload/runs/{run_index}/events"))

    section("审核来源与维度", "Review sources and dimensions")
    line(t("下列审核保留所申报的来源与分类；自动化接受候选不等于人工认可论文忠实度。审核可能属于未选实验，须核对目标 ID。",
           "Reviews retain their declared source and category. Automated acceptance is not human approval of paper fidelity. Reviews may concern unselected runs; check target IDs."))
    reviews = payload.get("feedback", {}).get("reviews", [])
    for review_index, review in indexed(reviews, "/payload/feedback/reviews", FEEDBACK_LIMIT):
        row(t("审核 ID", "Review ID"), review.get("id"))
        row(t("来源", "Source"), label(SOURCES, review.get("source")))
        row(t("分类", "Category"), label(CATEGORIES, review.get("category")))
        row(t("结论", "Verdict"), label(STATUS, review.get("verdict")))
        row(t("目标实验／候选／结果", "Target run / candidate / result"),
            " / ".join(str(review.get(key) or missing) for key in ("run_id", "candidate_id", "result_digest")))
        row(t("审核备注", "Review note"), review.get("note"))
        dimensions = (review.get("assessment") or {}).get("dimensions") or {}
        for name, labels in DIMENSIONS.items():
            dimension = dimensions.get(name) or {}
            row(labels[index], label(STATUS, dimension.get("outcome", "not_assessed")) + " — " + str(dimension.get("reason") or missing))
    if not reviews:
        line(t("没有冻结的审核记录；各研究语义维度均未评定。", "No reviews were frozen; research semantic dimensions are not assessed."))

    section("未解决事项与完整记录索引", "Open issues and complete record index")
    issues = payload.get("feedback", {}).get("issues", [])
    for issue_index, issue in indexed(issues, "/payload/feedback/issues", FEEDBACK_LIMIT):
        row(t("问题／状态", "Issue / status"), str(issue.get("id")) + " / " + str(issue.get("status", "unknown")))
        row(t("标题／说明", "Title / note"), issue.get("title") or issue.get("note"))
    if not issues:
        line(t("未记录问题不表示研究已被验证。", "No recorded issue does not mean the research was validated."))
    for pointer in ("/payload/revisions", "/payload/runs", "/payload/feedback/reviews",
                    "/payload/feedback/issues", "/payload/feedback/regression_cases", "/payload/feedback/regression_checks"):
        row(t("审计 JSON 指针", "Audit JSON pointer"), pointer)
    line(t("阅读版的条目限制已逐项标明。完整 Markdown 和审计 JSON 保留全部冻结记录、失败及事件；不会因下载语言不同改写 payload digest。",
           "Reading-view limits are stated for each collection. Full Markdown and audit JSON retain all frozen records, failures and events; download language never changes the payload digest."))

    section("限制", "Limitations")
    line(t("- 合成数据仅验证工程流程；未计成本的毛收益不代表可交易利润。\n- 字面引文匹配、来源完整性和数值核验各有范围，均不能替代人工研究语义判断。\n- 本报告没有补填缺失指标，也没有推断真人研究耗时或 Agent 优势。",
           "- Synthetic data validates engineering only; gross returns without costs are not tradable profit.\n- Literal citation matching, source integrity and numerical checks each have limited scope and do not replace human research judgment.\n- Missing metrics, human research time and agent advantage are not inferred."))
    for limitation in payload.get("limitations", []):
        line("- " + text(limitation))
    line()
    return "\n".join(lines)
