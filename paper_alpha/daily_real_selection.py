"""Pinned metadata-only daily selections; never admission or execution authority."""
from __future__ import annotations

from copy import deepcopy
import hashlib
import os
from pathlib import Path
import tempfile

from . import alpha_vantage as collector
from . import alpha_vantage_set as sets
from . import evaluation
from .daily_real_preparation import EXPRESSION_101

VERSION = "daily-real-selection-plan-v1"
MAX_METADATA_BYTES = sets.MAX_METADATA_BYTES
MIN_ASSETS = 3
EVENT_POLICY = "reject_unsupported_development_events"


class DailyRealSelectionError(ValueError):
    def __init__(self, code):
        self.code = code
        super().__init__(code)


def _need(condition, code):
    if not condition:
        raise DailyRealSelectionError(code)


def _metadata(path, pin, role):
    sets._sha(pin)
    _need((type(path) is str or isinstance(path, Path)) and Path(path).is_absolute(), "absolute_metadata_path_required")
    full = collector._absolute(path)
    # Narrow filename roles reject known originals before opening any file.
    _need(full.suffix == ".json" and "raw" not in full.stem.casefold() and
          ("set" in full.stem.casefold() if role == "set" else "event" in full.stem.casefold()) and
          not any(p.casefold() in ("secrets", "credentials", "alpha-vantage-claims") or
                  p.casefold().endswith(".key") for p in full.parts), "metadata_role_required")
    payload = collector._read(full, MAX_METADATA_BYTES)
    _need(hashlib.sha256(payload).hexdigest() == pin, "metadata_pin_mismatch")
    return collector._json(payload), {"path": str(full), "sha256": pin, "size_bytes": len(payload)}


def _reload_set(set_path, expected_set_sha256):
    value, identity = _metadata(set_path, expected_set_sha256, "set")
    _need(type(value) is dict and value.get("set_version") == sets.VERSION and
          type(value.get("schema_version")) is int and value["schema_version"] == 1,
          "invalid_metadata_set")
    # Recreate every derived field from actual pinned upstream metadata. A
    # caller resealing fabricated selected rows cannot pass equality below.
    _need(type(value.get("requested_range")) is dict and
          set(value["requested_range"]) == {"start_date", "end_date"} and
          type(value.get("calendar")) is dict and type(value.get("manifests")) is list,
          "invalid_metadata_set")
    refs = [{"path": ref["path"], "sha256": ref["sha256"]} for ref in value["manifests"]]
    with tempfile.TemporaryDirectory(prefix="daily-selection-metadata-", dir="/private/tmp") as directory:
        report = sets.build_acquisition_set(
            refs, value["assets"], value["requested_range"]["start_date"],
            value["requested_range"]["end_date"], value["calendar"]["path"],
            value["calendar"]["sha256"], Path(directory).resolve() / "set-revalidated.json")
    _need(collector._canonical(value) == collector._canonical(report), "set_derivation_mismatch")
    _need(report["status"] == "metadata_complete_pending_admission" and not report["missing_inputs"],
          "metadata_set_incomplete")
    _need(len(report["assets"]) >= MIN_ASSETS, "insufficient_assets")
    times, original_slots = [], []
    for ref in refs:
        manifest, _, _ = sets._metadata(ref["path"], ref["sha256"], "manifest")
        # build_acquisition_set already checked shape and temporal binding.
        times.append({"path": ref["path"], "sha256": ref["sha256"],
                      "acquisition_version": manifest["acquisition_version"],
                      "started_at_utc": manifest["started_at_utc"],
                      "finished_at_utc": manifest["finished_at_utc"]})
        original_slots.extend(str(Path(ref["path"]).parent / ("%02d-%s.raw.json" % (a["attempt"], a["symbol"])))
                              for a in manifest["attempts"])
    # A second read guards input replacement during construction.
    _metadata(set_path, expected_set_sha256, "set")
    sets._metadata(report["calendar"]["path"], report["calendar"]["sha256"], "calendar")
    return report, identity, times, sorted(set(original_slots))


def _freeze_selection(set_path, expected_set_sha256, *, train_sessions,
                      validation_sessions, test_sessions, selected_as_of_utc):
    counts = {"train": train_sessions, "validation": validation_sessions, "test": test_sessions}
    _need(all(type(n) is int and n >= 3 for n in counts.values()), "invalid_split_counts")
    selected_time = sets._utc(selected_as_of_utc)
    source, identity, acquisitions, slots = _reload_set(set_path, expected_set_sha256)
    sessions = source["expected_sessions"]
    _need(sum(counts.values()) == len(sessions), "split_must_cover_entire_selection")
    _need(all(selected_time >= sets._utc(a["finished_at_utc"]) for a in acquisitions),
          "selection_precedes_acquisition")
    cursor, partitions = 0, {}
    for name, length in counts.items():
        partitions[name] = sessions[cursor:cursor + length]
        cursor += length
    config = evaluation.validate_config({"min_assets": MIN_ASSETS,
        "splits": {name: {"start": days[0], "end": days[-1]} for name, days in partitions.items()}}, sessions)
    return {
        "schema_version": 1, "selection_version": VERSION,
        "status": "metadata_frozen_pending_admission", "set_identity": identity,
        "assets": list(source["assets"]), "exchange": source["calendar"]["exchange"],
        "timezone": source["calendar"]["timezone"], "selected_as_of_utc": selected_as_of_utc,
        "selection_timestamp_meaning": "project selection of current-vintage downloaded data; not point-in-time availability",
        "requested_range": deepcopy(source["requested_range"]), "sessions": list(sessions),
        "window_unit": "exchange_sessions", "counts": counts, "partitions": partitions,
        "missing_policy": "reject_no_fill_no_calendar_compression",
        "configuration_origin": "project_configuration", "paper_specifies_split": False,
        "config": config, "execution_semantics": deepcopy(evaluation.EXECUTION),
        "development_sessions": partitions["train"] + partitions["validation"],
        "validation_evaluation_signal_sessions": partitions["validation"][:-2],
        "validation_purged_signal_sessions": partitions["validation"][-2:],
        "purge_reason": "entry t+1 and exit t+2 must remain in validation",
        "final_partition": {"status": "reserved_by_metadata_plan", "sessions": list(partitions["test"]),
                            "host_seal_created": False, "test_freshness": "unknown",
                            "numeric_read_allowed": False},
        "acquisitions": acquisitions, "calendar_identity": deepcopy(source["calendar"]),
        "selected_raw_descriptors": [deepcopy(row["raw"]) for row in source["selected"]],
        "reserved_original_slots": slots, "universe_kind": "static_current_vintage",
        "pit": "not_certified", "raw_bytes_authenticated": False,
        "pending_source_facts": list(source["pending_source_facts"]),
        "source_admission": "pending", "execution_allowed": False,
        "market_originals_opened": 0, "market_originals_hashed": 0,
        "numeric_market_values_converted": False, "evaluation_performed": False,
        "experiment_test_partition_created": False, "network_requests": 0,
        "model_calls": 0, "db_mutations": 0,
    }


def _translate(call, *args, **kwargs):
    try:
        return call(*args, **kwargs)
    except DailyRealSelectionError:
        raise
    except (sets.AlphaVantageSetError, collector.AlphaVantageError) as exc:
        raise DailyRealSelectionError(exc.code) from None
    except ValueError as exc:
        # Event contract errors expose a bounded code; never echo evidence text.
        from .daily_event_review import DailyEventReviewError
        if isinstance(exc, DailyEventReviewError):
            raise DailyRealSelectionError(exc.code) from None
        raise DailyRealSelectionError("invalid_selection_input") from None
    except (OSError, TypeError, KeyError, AttributeError, RecursionError):
        raise DailyRealSelectionError("invalid_selection_input") from None


def freeze_selection(set_path, expected_set_sha256, *, train_sessions,
                     validation_sessions, test_sessions, selected_as_of_utc):
    """Rebuild source metadata and freeze explicit contiguous session counts."""
    return _translate(_freeze_selection, set_path, expected_set_sha256,
                      train_sessions=train_sessions, validation_sessions=validation_sessions,
                      test_sessions=test_sessions, selected_as_of_utc=selected_as_of_utc)


def _code_identity():
    from . import daily_event_review
    from . import daily_real_preparation
    paths = {"paper_alpha/daily_real_selection.py": Path(__file__),
             "scripts/plan_real_daily_development.py": Path(__file__).resolve().parents[1] / "scripts/plan_real_daily_development.py",
             "paper_alpha/daily_event_review.py": Path(daily_event_review.__file__),
             "paper_alpha/alpha_vantage_set.py": Path(sets.__file__),
             "paper_alpha/alpha_vantage.py": Path(collector.__file__),
             "paper_alpha/evaluation.py": Path(evaluation.__file__),
             "paper_alpha/daily_real_preparation.py": Path(daily_real_preparation.__file__)}
    files = {name: hashlib.sha256(path.read_bytes()).hexdigest() for name, path in paths.items()}
    return {"files": files, "sha256": collector._digest(files)}


def _plan(set_path, expected_set_sha256, event_ledger_path, expected_event_ledger_sha256,
          *, train_sessions, validation_sessions, test_sessions, selected_as_of_utc):
    from .daily_event_review import assess_event_policy
    selection = _freeze_selection(set_path, expected_set_sha256,
        train_sessions=train_sessions, validation_sessions=validation_sessions,
        test_sessions=test_sessions, selected_as_of_utc=selected_as_of_utc)
    ledger, identity = _metadata(event_ledger_path, expected_event_ledger_sha256, "event")
    review = assess_event_policy(ledger, assets=selection["assets"], sessions=selection["sessions"],
                                 development_sessions=selection["development_sessions"], policy=EVENT_POLICY)
    _need(review["execution_allowed"] is False and review["source_admission"] == "pending" and
          review["status"] in ("blocked", "policy_clear_pending_authority"), "event_boundary_violation")
    _need(sets._utc(ledger["created_at_utc"]) <= sets._utc(selected_as_of_utc), "selection_precedes_event_ledger")
    report = {
        "schema_version": 1, "plan_version": VERSION,
        "status": "blocked" if review["blockers"] else "metadata_planned_pending_authority",
        "selection": selection, "event_ledger_identity": identity, "event_policy": EVENT_POLICY,
        "event_review": review,
        "event_digest_meaning": "event_review.canonical_ledger_digest is canonical-object digest; event_ledger_identity.sha256 is actual retained-file bytes",
        "candidate": {"id": "alpha101-101", "expression": EXPRESSION_101,
                      "expression_origin": "existing_project_alpha101_101", "configuration_origin": "project_configuration"},
        "return_convention": {"formula": "open[t+2] / open[t+1] - 1", "kind": "gross_price_return",
                              "cash_dividends": "excluded", "costs": "excluded", "slippage": "excluded",
                              "borrow": "excluded", "total_return": False},
        "blockers": deepcopy(review["blockers"]),
        "next_gate": "resolve_event_coverage_and_supported_method" if review["blockers"] else
                     "versioned_independent_host_local_development_authority",
        "code_identity": _code_identity(), "source_admission": "pending", "execution_allowed": False,
        "market_originals_opened": 0, "market_originals_hashed": 0,
        "numeric_market_values_converted": False, "evaluation_performed": False,
        "experiment_test_partition_created": False, "final_numeric_rows_parsed": 0,
        "network_requests": 0, "model_calls": 0, "db_mutations": 0,
    }
    _metadata(event_ledger_path, expected_event_ledger_sha256, "event")
    return report


def plan_real_development(set_path, expected_set_sha256, event_ledger_path,
                          expected_event_ledger_sha256, *, train_sessions,
                          validation_sessions, test_sessions, selected_as_of_utc):
    """Bind a frozen selection to event evidence, without producing market rows."""
    return _translate(_plan, set_path, expected_set_sha256, event_ledger_path,
        expected_event_ledger_sha256, train_sessions=train_sessions,
        validation_sessions=validation_sessions, test_sessions=test_sessions,
        selected_as_of_utc=selected_as_of_utc)


def _write_plan(report, out_path):
    """CLI-only publication. Revalidate inputs immediately before O_EXCL write."""
    try:
        out = _precheck_output(out_path)
        selection = report["selection"]
        protected = {selection["set_identity"]["path"], selection["calendar_identity"]["path"],
                     report["event_ledger_identity"]["path"], *selection["reserved_original_slots"],
                     *(ref["path"] for ref in selection["acquisitions"])}
        _need(str(out) not in protected and all(not out.is_relative_to(Path(ref["path"]).parent)
                                               for ref in selection["acquisitions"]), "output_role_conflict")
        refreshed = plan_real_development(selection["set_identity"]["path"], selection["set_identity"]["sha256"],
            report["event_ledger_identity"]["path"], report["event_ledger_identity"]["sha256"],
            train_sessions=selection["counts"]["train"], validation_sessions=selection["counts"]["validation"],
            test_sessions=selection["counts"]["test"], selected_as_of_utc=selection["selected_as_of_utc"])
        _need(collector._canonical(refreshed) == collector._canonical(report), "plan_changed_before_publication")
        collector._write_new(out, collector._canonical(report) + b"\n")
        return report
    except DailyRealSelectionError:
        raise
    except (sets.AlphaVantageSetError, collector.AlphaVantageError) as exc:
        raise DailyRealSelectionError(exc.code) from None
    except (OSError, ValueError, TypeError, KeyError):
        raise DailyRealSelectionError("output_unavailable") from None


def _precheck_output(out_path):
    """Reject obvious output conflicts before reading any task input."""
    try:
        out = sets._output_path(out_path)
        _need("raw" not in out.stem.casefold(), "output_role_conflict")
        try:
            with collector._parent(out) as (parent, name, _):
                try:
                    os.stat(name, dir_fd=parent, follow_symlinks=False)
                except FileNotFoundError:
                    pass
                else:
                    raise DailyRealSelectionError("output_already_exists")
        except collector.AlphaVantageError as exc:
            if exc.code != "path_missing":
                raise
        return out
    except DailyRealSelectionError:
        raise
    except (sets.AlphaVantageSetError, collector.AlphaVantageError) as exc:
        raise DailyRealSelectionError(exc.code) from None
    except (OSError, ValueError, TypeError):
        raise DailyRealSelectionError("output_unavailable") from None
