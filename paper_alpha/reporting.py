"""Reports are deterministic projections of saved tool outputs, never model prose."""
from __future__ import annotations

import json


def render_report(state):
    task = state["task"]
    lines = [f"# {task.get('title', 'Paper-to-Alpha research task')}", "",
             f"Run status: **{state['status']}**. Policy: `{state['mode']}`.", "",
             "This is an offline engineering research prototype. The bundled market data is synthetic; "
             "its metrics do not establish investment performance. No LLM or BRAIN API was called.", "",
             "## Provenance and review", "",
             "Paper quotes are checked against the exact PDF bytes and re-extracted page text. "
             "Quote matching does not verify economic reasoning or semantic fidelity. "
             "Human review is still required. Personal study of this paper is not assumed.", ""]
    for e in state.get("preflight", {}).get("evidence", []):
        lines += [f"- `{e['id']}` — physical PDF page {e['page']}: `{e['quote']}`"]
    for h in task.get("hypotheses", []):
        lines += ["", f"### Hypothesis `{h['id']}`", "", h["claim"], "",
                  f"Claim attribution: `{h['attribution']}`; mechanism attribution: `{h['mechanism_attribution']}`.", "",
                  h["economic_mechanism"], "", "Assumptions: " + "; ".join(h["assumptions"])]
    lines += ["", "## Experiments", ""]
    for item in state.get("candidates", []):
        lines += [f"### `{item['id']}` — {item['status']}", "",
                  f"Hypothesis: `{item['hypothesis_id']}`. Origin: `{item['origin']}`.", "",
                  f"Original expression: `{item['expression']}`", ""]
        if item.get("changes"):
            lines += ["Declared changes: " + "; ".join(item["changes"]), ""]
        for attempt in item.get("attempts", []):
            lines += [f"- Attempt {attempt['number']}: `{attempt['expression']}` → {attempt['status']}."]
            if "error" in attempt:
                lines += [f"  Tool error: {attempt['error']['code']}: {attempt['error']['message']}"]
            if "repair" in attempt:
                lines += [f"  Decision: normalize a documented operator alias to `{attempt['repair']}`; "
                          "no data-field or window substitution."]
        if item.get("reason"):
            lines += ["", "Decision: " + item["reason"]]
        if item.get("result"):
            lines += ["", "Computed validation metrics (exact values are in result.json):", "",
                      "```json", json.dumps(item["result"]["metrics"], ensure_ascii=False,
                                            sort_keys=True, indent=2, allow_nan=False), "```", "",
                      "Artifacts: " + ", ".join(f"`{p}`" for p in sorted(item.get("artifacts", {})))]
        lines += [""]
    if state.get("error"):
        lines += ["Run error: " + state["error"]["message"], ""]
    lines += ["## Evaluation and limits", "",
              "Signals use information available after session t. Entry is open(t+1), exit is open(t+2); "
              "signal, entry and exit must belong to the validation interval. Trailing history may use "
              "the earlier training interval. No model is fitted in this version. The final test interval is locked.", "",
              "Gross daily portfolio metrics omit fees, slippage, borrow costs and a production execution model. "
              "The local adapter's rank, missing-value, calendar and operator semantics are independent of BRAIN.", "",
              "Means use valid days only. Missing or constant signal days are skipped; IC also skips constant-return days. "
              "Compare coverage and sample counts before comparing candidates.", "",
              f"Tool calls: {state['tool_calls']}; LLM calls: 0; external API cost: 0 (local compute cost not measured).", "",
              "All rejected, failed, interrupted and budget-stopped attempts remain in state.json and events.jsonl.", "",
              "This policy is deterministic and handles only explicit task proposals and supported formula templates. "
              "Automatic literature search, general semantic paper reading, human-time comparison and live BRAIN simulation are not implemented.", ""]
    return "\n".join(lines)
