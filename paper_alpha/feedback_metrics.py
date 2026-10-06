"""Pure, reproducible review/issue aggregates; no database or human inference.

Sources are declarations, not authenticated identities. Output units are exact
(run, attempt, candidate, digest) tuples; repeated reviews never increase output
coverage. A resolved issue is not evidence of saved human working time.
"""
from __future__ import annotations

from collections import Counter
from datetime import datetime

from .storage import digest


METRICS_VERSION = "feedback-v2-structured-assessments"
SOURCES = ("human", "automation", "imported", "legacy_unknown")
OUTPUT_KEYS = ("run_id", "attempt_id", "candidate_id", "result_digest")


def _identity(record):
    values = [record.get(key) for key in OUTPUT_KEYS]
    if not all(isinstance(value, str) and value for value in values):
        raise ValueError("Output/review requires run, attempt, candidate and result digest")
    return digest(dict(zip(OUTPUT_KEYS, values)))


def _indexed(records, label):
    result = {}
    for record in records:
        key = record.get("id")
        if not isinstance(key, str) or not key:
            raise ValueError(f"{label} requires a nonempty id")
        if key in result and result[key] != record:
            raise ValueError(f"Conflicting duplicate {label}: {key}")
        result[key] = record
    return result


def _source(record):
    source = record.get("source", "legacy_unknown")
    if source not in SOURCES:
        raise ValueError("Unknown declared review source")
    return source


def _ratio(passed, eligible):
    eligible, passed = sorted(set(eligible)), sorted(set(passed) & set(eligible))
    return {"numerator": len(passed), "denominator": len(eligible),
            "rate": len(passed) / len(eligible) if eligible else None,
            "passed_ids": passed, "eligible_ids": eligible}


def _time(value):
    parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if parsed.tzinfo is None:
        raise ValueError("Review and issue event timestamps must include a timezone")
    return parsed


def _assessment_metrics(reviews, eligible):
    """Declared judgments and observed review effort, recomputed from raw records.

    Multiple records never increase output coverage. Overlapping intervals from
    the same declared reviewer are unioned, not double-counted. This does not
    authenticate identity or estimate full research time / time savings.
    """
    dimensions = {'evidence_accuracy', 'hypothesis_fidelity', 'mechanism_attribution',
                  'field_semantics', 'implementation_alignment'}
    covered, records, intervals_by_reviewer = set(), [], {}
    for review in sorted(reviews, key=lambda row: row['id']):
        assessment = review.get('assessment')
        if assessment is None or _identity(review) not in eligible:
            continue
        if not isinstance(assessment, dict) or set(assessment) != {
                'reviewer', 'expected_attempt_id', 'expected_result_digest', 'dimensions', 'active_intervals'}:
            raise ValueError('Invalid structured assessment')
        if (assessment['expected_attempt_id'] != review['attempt_id'] or
                assessment['expected_result_digest'] != review['result_digest']):
            raise ValueError('Assessment target differs from its frozen review')
        reviewer = assessment['reviewer']
        if not isinstance(reviewer, str) or not reviewer.strip():
            raise ValueError('Assessment reviewer must be nonblank')
        judgments = assessment['dimensions']
        if not isinstance(judgments, dict) or set(judgments) != dimensions:
            raise ValueError('Assessment dimensions are incomplete')
        outcomes = []
        for judgment in judgments.values():
            if (not isinstance(judgment, dict) or set(judgment) != {'outcome', 'reason'} or
                    judgment['outcome'] not in {'passed', 'failed', 'not_assessed', 'not_applicable'} or
                    not isinstance(judgment['reason'], str) or not judgment['reason'].strip()):
                raise ValueError('Invalid assessment judgment')
            outcomes.append(judgment['outcome'])
        source = _source(review)
        status = ('not_human' if source != 'human' else 'failed' if 'failed' in outcomes else
                  'passed' if all(value == 'passed' for value in outcomes) else 'incomplete')
        raw_intervals = assessment['active_intervals']
        if not isinstance(raw_intervals, list) or len(raw_intervals) > 100:
            raise ValueError('Assessment intervals must be a bounded list')
        spans = []
        for interval in raw_intervals:
            if not isinstance(interval, dict) or set(interval) != {'started_at', 'ended_at'}:
                raise ValueError('Invalid active interval')
            start, end = _time(interval['started_at']), _time(interval['ended_at'])
            if end <= start or end > _time(review['created_at']):
                raise ValueError('Active interval must end after start and before its saved review')
            spans.append((start, end))
        spans.sort()
        if any(spans[i][0] < spans[i - 1][1] for i in range(1, len(spans))):
            raise ValueError('Active intervals within one assessment overlap')
        seconds = sum((end - start).total_seconds() for start, end in spans)
        if seconds > 86400:
            raise ValueError('Assessment active time exceeds 24 hours')
        if source == 'human':
            covered.add(_identity(review))
            if spans:
                intervals_by_reviewer.setdefault(reviewer.strip(), []).extend(spans)
        records.append({'review_id': review['id'], 'output_id': _identity(review), 'source': source,
                        'semantic_status': status, 'timing_recorded': bool(spans),
                        'recorded_active_seconds': seconds if spans else None})
    observed = []
    for reviewer, intervals in sorted(intervals_by_reviewer.items()):
        merged = []
        for start, end in sorted(intervals):
            if merged and start <= merged[-1][1]:
                merged[-1] = (merged[-1][0], max(merged[-1][1], end))
            else:
                merged.append((start, end))
        observed.append({'declared_reviewer': reviewer,
                         'union_active_seconds': sum((end - start).total_seconds() for start, end in merged),
                         'recorded_intervals': len(intervals), 'merged_intervals': len(merged)})
    return {'human_output_coverage': _ratio(covered, eligible), 'records': records,
            'human_active_time': {'observed_person_seconds': sum(row['union_active_seconds'] for row in observed) if observed else None,
                                  'by_declared_reviewer': observed},
            'scope': 'Declared per-review judgments only. Human output coverage is deduplicated. Active intervals are unioned per declared reviewer; different reviewers contribute person-time. No full research duration, authenticated identity or saved-time claim.'}


def aggregate_feedback(outputs: list[dict], reviews: list[dict], issues: list[dict],
                       events: list[dict], cases=(), checks=()) -> dict:
    """Aggregate a caller-selected cohort, without reading or changing storage.

    outputs need OUTPUT_KEYS and explicit ``eligible: bool``. The caller only
    labels frozen, verified attempts eligible. issues need id/review_id and may
    have source/created_at. events need id/issue_id/created_at/state/disposition;
    closing evidence is revision_id/target_run_id/target_attempt_id/
    target_result_digest/check_id. Outputs also carry revision_id/state_digest
    for matching that frozen target. checks are decoded
    regression-check payloads. Filtering belongs to the caller and is retained
    alongside this result by the API/export layer.
    """
    output_index = {}
    for record in outputs:
        key = _identity(record)
        if type(record.get("eligible")) is not bool:
            raise ValueError("Every output requires an explicit boolean eligibility")
        if key in output_index and output_index[key] != record:
            raise ValueError("Conflicting records for the same frozen output")
        output_index[key] = record
    review_index = _indexed(reviews, "review")
    issue_index = _indexed(issues, "issue")
    event_index = _indexed(events, "issue event")
    case_index = _indexed(cases, "case")
    check_index = _indexed(checks, "check")
    eligible = {key for key, value in output_index.items() if value["eligible"]}
    covered = {source: set() for source in SOURCES}
    review_counts = Counter({source: 0 for source in SOURCES})
    out_of_cohort_reviews = []
    for review in review_index.values():
        key, source = _identity(review), _source(review)
        review_counts[source] += 1
        if key in eligible:
            covered[source].add(key)
        else:
            out_of_cohort_reviews.append(review["id"])
    events_by_issue = {identity: [] for identity in issue_index}
    for event in event_index.values():
        if event.get("issue_id") not in events_by_issue:
            raise ValueError("Issue event refers to an unknown issue")
        _source(event)
        events_by_issue[event["issue_id"]].append(event)
    states = Counter()
    dispositions = Counter()
    closed, fixed, fix_attempts, human_issues, converted, elapsed, incomplete = [], [], [], [], [], [], []
    issue_rows = []
    scoped_issues = []
    for identity, issue in sorted(issue_index.items()):
        if issue.get("review_id") not in review_index:
            raise ValueError("Issue refers to an unknown review")
        review = review_index[issue["review_id"]]
        if _identity(review) not in eligible:
            continue
        scoped_issues.append(identity)
        source = _source(issue) if "source" in issue else _source(review)
        if source == "human":
            human_issues.append(identity)
        history = sorted(events_by_issue[identity], key=lambda item: (_time(item["created_at"]), item["id"]))
        latest = history[-1] if history else {"state": "open", "disposition": None}
        if issue.get("latest_event_id"):
            matches = [event for event in history if event["id"] == issue["latest_event_id"]]
            if len(matches) != 1:
                raise ValueError("Issue latest_event_id is absent from its event snapshot")
            latest = matches[0]  # The store's append order outranks wall-clock/UUID order.
        state, disposition = latest["state"], latest.get("disposition")
        states[state] += 1
        if disposition:
            dispositions[disposition] += 1
        if disposition == "implementation_fix":
            fix_attempts.append(identity)
        target = latest.get("target_run_id")
        target_attempt, target_digest = latest.get("target_attempt_id"), latest.get("target_result_digest")
        target_known = any((value["eligible"] or value.get("linkage_verified") is True) and value["run_id"] == target
                           and value["candidate_id"] == review["candidate_id"]
                           and bool(target_attempt) and bool(target_digest)
                           and value["attempt_id"] == target_attempt
                           and value.get("state_digest") == target_digest
                           and value.get("revision_id") == latest.get("revision_id")
                           for value in output_index.values())
        linked = bool(latest.get("revision_id") and target and target_known)
        proof = check_index.get(latest.get("check_id"), {})
        bound_cases = {case["id"] for case in case_index.values()
                       if case.get("review_id") == review["id"] and case.get("approved") and case.get("contract")}
        matching = [row for row in proof.get("results", []) if row.get("case_id") in bound_cases]
        bound_result = bool(matching and all(row.get("candidate_id") == review["candidate_id"]
                                            and row.get("compatible") is True and row.get("outcome") == "passed"
                                            for row in matching))
        compatible_fix = bool(linked and proof.get("run_id") == target
                              and proof.get("revision_id") == latest.get("revision_id")
                              and proof.get("attempt_id") == target_attempt
                              and proof.get("result_digest") == target_digest
                              and proof.get("results") and bound_result
                              )
        closure = state == "resolved" and linked and (disposition != "implementation_fix" or compatible_fix)
        if closure:
            closed.append(identity)
            if disposition == "implementation_fix" and compatible_fix:
                fixed.append(identity)
            start = issue.get("created_at") or review.get("created_at")
            if start and latest.get("created_at"):
                seconds = (_time(latest["created_at"]) - _time(start)).total_seconds()
                if seconds < 0:
                    raise ValueError("Issue closure precedes issue creation")
                elapsed.append({"issue_id": identity, "seconds": seconds, "source": source})
        elif state == "resolved":
            incomplete.append(identity)
        if any(case.get("review_id") == review["id"] and case.get("approved")
               and case.get("contract") for case in case_index.values()):
            converted.append(identity)
        issue_rows.append({"issue_id": identity, "review_id": review["id"], "source": source,
                           "state": state, "disposition": disposition,
                           "closure_evidence_complete": closure,
                           "same_condition_fix_verified": identity in fixed,
                           "latest_event_id": latest.get("id")})
    records = {"outputs": sorted(output_index.values(), key=_identity),
               "reviews": sorted(review_index.values(), key=lambda item: item["id"]),
               "issues": sorted(issue_index.values(), key=lambda item: item["id"]),
               "events": sorted(event_index.values(), key=lambda item: item["id"]),
               "cases": sorted(case_index.values(), key=lambda item: item["id"]),
               "checks": sorted(check_index.values(), key=lambda item: item["id"])}
    return {"schema_version": 1, "metrics_version": METRICS_VERSION, "input_digest": digest(records),
            "scope": "Caller-selected verified output cohort; review source is declared, not authenticated.",
            "output_count": len(output_index), "eligible_output_count": len(eligible),
            "supporting_output_count": sum(not value["eligible"] and value.get("linkage_verified") is True for value in output_index.values()),
            "run_count": len({value["run_id"] for value in output_index.values() if value["eligible"]}),
            "independent_run_count": len({value["run_id"] for value in output_index.values() if value["eligible"]}),
            "count_scope": "run_count and its compatibility alias independent_run_count deduplicate run IDs; neither counts independent research hypotheses. Attempts and frozen outputs have separate units.",
            "attempt_count": len({(value["run_id"], value["attempt_id"]) for value in output_index.values() if value["eligible"]}),
            "output_units": {key: {field: value[field] for field in OUTPUT_KEYS}
                             for key, value in sorted(output_index.items())},
            "review_rows_by_source": dict(review_counts),
            "review_coverage_by_source": {source: _ratio(ids, eligible) for source, ids in covered.items()},
            "human_review_coverage": _ratio(covered["human"], eligible),
            "any_review_coverage": _ratio(set().union(*covered.values()), eligible),
            "out_of_cohort_review_ids": sorted(out_of_cohort_reviews),
            "issue_states": dict(states), "issue_dispositions": dict(dispositions),
            "issue_closure": _ratio(closed, scoped_issues),
            "human_issue_closure": _ratio(closed, human_issues),
            "development_regression_conversion": _ratio(converted, scoped_issues),
            "same_condition_fix_verification": _ratio(fixed, fix_attempts),
            "incomplete_resolved_issue_ids": incomplete, "issues": issue_rows,
            "closure_elapsed": {"records": elapsed, "scope": "Wall elapsed time including waiting; not active human work or time saved."},
            "structured_assessments": _assessment_metrics(review_index.values(), eligible),
            "unmeasured": ["human_end_to_end_work_time", "human_time_saved", "independently_validated_semantic_fidelity", "economic_value"]}
