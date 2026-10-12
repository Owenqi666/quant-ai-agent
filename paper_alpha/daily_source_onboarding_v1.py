"""Offline retained-byte checks. Completeness never authenticates a source."""
from __future__ import annotations

import hashlib
import json
import math
import re
from pathlib import Path, PurePosixPath
import stat

from .daily_adapter import _open_regular, _signature, _stable
from .daily_contract import MAX_METADATA_BYTES, MAX_RAW_BYTES, parse_contract, parse_source_receipt
from .server.daily_source_qualification_v2 import FACTS, validate_source_review
from .storage import digest

VERSION = "daily-source-onboarding-v1"
MAX_EVIDENCE_BYTES = 2 * 1024 * 1024
FILE_KEYS = {"relative_path", "sha256", "bytes"}
BUNDLE_KEYS = {"schema_version", "bundle_version", "source_id", "contract", "receipt", "review", "raw", "evidence"}


class DailySourceOnboardingError(ValueError):
    def __init__(self, code, role, message):
        super().__init__(message)
        self.code, self.role = code, role


def _require(condition, code, role, message):
    if not condition:
        raise DailySourceOnboardingError(code, role, message)


def _json(raw, role):
    def pairs(items):
        result = {}
        for key, value in items:
            if key in result:
                raise ValueError("Duplicate JSON key")
            result[key] = value
        return result

    def invalid(_):
        raise ValueError("Nonfinite JSON number")

    def number(value):
        parsed = float(value)
        if not math.isfinite(parsed):
            invalid(value)
        return parsed

    try:
        return json.loads(raw.decode("utf-8"), object_pairs_hook=pairs,
                          parse_constant=invalid, parse_float=number)
    except (ValueError, UnicodeError, RecursionError) as exc:
        raise DailySourceOnboardingError("invalid_json", role, "Strict bounded JSON required") from exc


def _path(root, relative, role):
    _require(type(relative) is str and 0 < len(relative) <= 1000 and
             "\x00" not in relative and "\\" not in relative,
             "unsafe_path", role, "Bounded bundle-relative path required")
    parts = relative.split("/")
    _require(not PurePosixPath(relative).is_absolute() and all(p not in ("", ".", "..") for p in parts),
             "unsafe_path", role, "Paths cannot escape the bundle or use aliases")
    path = root.joinpath(*parts)
    _require(path.is_relative_to(root), "unsafe_path", role, "Path must stay within the bundle")
    _require(path.resolve() == path and not any(p.is_symlink() for p in (path, *path.parents)),
             "unsafe_path", role, "Symbolic paths are refused")
    return path


def _descriptor(value, role, limit):
    _require(type(value) is dict and set(value) == FILE_KEYS,
             "invalid_descriptor", role, "Closed retained-file descriptor required")
    _require(type(value["bytes"]) is int and 0 < value["bytes"] <= limit,
             "file_limit", role, "Declared byte size exceeds the role bound")
    _require(type(value["sha256"]) is str and re.fullmatch(r"[0-9a-f]{64}", value["sha256"]) is not None,
             "invalid_digest", role, "Lower-case SHA-256 required")


def _info(path, role, limit):
    try:
        info = path.stat()
        _require(path.resolve() == path and not any(p.is_symlink() for p in (path, *path.parents)),
                 "unsafe_path", role, "Symbolic paths are refused")
        _require(stat.S_ISREG(info.st_mode) and info.st_nlink == 1 and 0 < info.st_size <= limit,
                 "unsafe_file", role, "One regular retained file within the role bound required")
        return info
    except OSError as exc:
        raise DailySourceOnboardingError("missing_or_unsafe_file", role, "Retained file unavailable") from exc


def _read(path, role, limit, expected=None, *, raw_inode=None, capture=True):
    try:
        path, stream, before = _open_regular(path, limit)
        with stream:
            if role != "raw" and raw_inode is not None:
                _require((before.st_dev, before.st_ino) != raw_inode,
                         "raw_alias", role, "Raw bytes cannot be read through another role")
            sha, count, chunks = hashlib.sha256(), 0, []
            while chunk := stream.read(min(64 * 1024, limit + 1 - count)):
                count += len(chunk)
                _require(count <= limit, "file_limit", role, "Retained file grew past its bound")
                sha.update(chunk)
                if capture:
                    chunks.append(chunk)
            _stable(path, stream, before)
        _require(count == before.st_size, "input_changed", role, "File changed while reading")
        actual = {"sha256": sha.hexdigest(), "bytes": count}
        if expected is not None:
            _require(actual == {k: expected[k] for k in ("sha256", "bytes")},
                     "retained_identity_mismatch", role, "Retained SHA or byte size differs")
        return b"".join(chunks) if capture else None, actual, before
    except DailySourceOnboardingError:
        raise
    except (OSError, ValueError) as exc:
        raise DailySourceOnboardingError(getattr(exc, "code", "unsafe_file"), role,
                                         "Retained file could not be checked safely") from exc


def inspect_source_bundle(bundle_path, *, allow_whole_byte_hash=False):
    """Check local descriptors and actual retained bytes; no CSV or database access.

    The raw file is statted for path/alias safety, but never opened without the
    explicit flag. An allowed raw read is a streaming hash, never decoding.
    """
    _require(type(allow_whole_byte_hash) is bool, "invalid_permission", "raw", "Explicit boolean permission required")
    path = Path(bundle_path).absolute()
    _require(path.resolve() == path and not any(p.is_symlink() for p in (path, *path.parents)),
             "unsafe_path", "bundle", "Canonical nonsymbolic bundle path required")
    payload, bundle_identity, bundle_info = _read(path, "bundle", MAX_METADATA_BYTES)
    value = _json(payload, "bundle")
    _require(type(value) is dict and set(value) == BUNDLE_KEYS,
             "invalid_bundle", "bundle", "Closed onboarding bundle required")
    _require(type(value["schema_version"]) is int and value["schema_version"] == 1 and value["bundle_version"] == VERSION,
             "unsupported_version", "bundle", "Unsupported onboarding version")
    _require(type(value["source_id"]) is str and re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._:-]{0,95}", value["source_id"]) is not None,
             "invalid_source_id", "bundle", "Bounded source identity required")
    _require(type(value["evidence"]) is dict and set(value["evidence"]) == set(FACTS),
             "fact_denominator", "evidence", "All eight facts must remain in the denominator")

    refs = {name: value[name] for name in ("contract", "receipt", "review", "raw")}
    refs.update({"evidence:" + name: item for name, item in value["evidence"].items() if item is not None})
    paths, infos, seen = {}, {}, {(bundle_info.st_dev, bundle_info.st_ino): "bundle"}
    for role, descriptor in refs.items():
        limit = MAX_RAW_BYTES if role == "raw" else MAX_EVIDENCE_BYTES
        _descriptor(descriptor, role, limit)
        candidate = _path(path.parent, descriptor["relative_path"], role)
        _require(candidate not in paths.values() and candidate != path,
                 "file_alias", role, "Each retained role must have its own path")
        info = _info(candidate, role, limit)
        inode = (info.st_dev, info.st_ino)
        _require(inode not in seen, "file_alias", role, "Retained roles cannot share a file")
        paths[role], infos[role], seen[inode] = candidate, info, role
    raw_inode = (infos["raw"].st_dev, infos["raw"].st_ino)
    for role, descriptor in refs.items():
        if role != "raw":
            _require((descriptor["sha256"], descriptor["bytes"]) !=
                     (refs["raw"]["sha256"], refs["raw"]["bytes"]),
                     "raw_alias", role, "Raw content cannot be disguised as metadata or evidence")

    actual, documents = {}, {}
    for role in ("contract", "receipt", "review"):
        payload, actual[role], read_info = _read(paths[role], role, MAX_METADATA_BYTES, refs[role], raw_inode=raw_inode)
        _require(_signature(read_info) == _signature(infos[role]), "input_changed", role, "File changed before reading")
        documents[role] = _json(payload, role)
    try:
        contract = parse_contract(documents["contract"])
        receipt = parse_source_receipt(documents["receipt"])
        review = validate_source_review(documents["review"], contract=documents["contract"], receipt=documents["receipt"])
    except (ValueError, TypeError, KeyError, RecursionError) as exc:
        raise DailySourceOnboardingError("invalid_source_declaration", "declarations", "Closed source declarations required") from exc
    c, r, source_review = contract.to_dict(), receipt.to_dict(), review["review"]
    _require(value["source_id"] == c["source"]["source_id"] == r["source_id"] == source_review["source_id"],
             "source_identity_mismatch", "declarations", "Source identities must agree exactly")
    _require(c["origin"] == r["origin"], "origin_mismatch", "declarations", "Contract and receipt origins must agree")
    _require(c["source"]["receipt_sha256"] == actual["receipt"]["sha256"],
             "receipt_binding_mismatch", "receipt", "Contract must bind the retained receipt bytes")
    _require((r["raw_sha256"], r["raw_bytes"]) == (refs["raw"]["sha256"], refs["raw"]["bytes"]),
             "raw_binding_mismatch", "raw", "Receipt must bind the declared raw identity")

    evidence, missing = {}, list(review["missing_inputs"])
    for fact in FACTS:
        ref = value["evidence"][fact]
        declaration = source_review["facts"][fact]
        if ref is None:
            evidence[fact] = {"status": declaration["status"], "retained": False, "identity": None}
            missing.append("retained_evidence:" + fact)
            continue
        role = "evidence:" + fact
        _require(declaration["evidence_sha256"] == ref["sha256"], "fact_binding_mismatch", role,
                 "Fact must bind its retained evidence bytes")
        _, actual[role], read_info = _read(paths[role], role, MAX_EVIDENCE_BYTES, ref, raw_inode=raw_inode, capture=False)
        _require(_signature(read_info) == _signature(infos[role]), "input_changed", role, "Evidence changed before reading")
        evidence[fact] = {"status": declaration["status"], "retained": True, "identity": actual[role]}
    if value["evidence"]["calendar"] is not None:
        _require(c["calendar"]["evidence_sha256"] == value["evidence"]["calendar"]["sha256"],
                 "calendar_binding_mismatch", "evidence:calendar", "Calendar must bind its retained evidence")
    if r["use_scope"]["evidence_sha256"] is not None:
        rights = value["evidence"]["rights"]
        _require(rights is not None and rights["sha256"] == r["use_scope"]["evidence_sha256"],
                 "rights_binding_mismatch", "evidence:rights", "Declared rights must bind retained rights evidence")

    raw_verified = False
    if allow_whole_byte_hash:
        _, actual["raw"], read_info = _read(paths["raw"], "raw", MAX_RAW_BYTES, refs["raw"], capture=False)
        _require(_signature(read_info) == _signature(infos["raw"]), "input_changed", "raw", "Raw file changed before hashing")
        raw_verified = True
    else:
        missing.append("raw_byte_hash_permission")

    for role, candidate in {"bundle": path, **paths}.items():
        before = bundle_info if role == "bundle" else infos[role]
        limit = MAX_RAW_BYTES if role == "raw" else MAX_METADATA_BYTES
        _require(_signature(_info(candidate, role, limit)) == _signature(before),
                 "input_changed", role, "Retained file changed before report publication")
    retained = all(item["retained"] for item in evidence.values())
    complete = review["checks_complete"] and retained and raw_verified
    result = {
        "schema_version": 1, "bundle_version": VERSION, "status": "complete" if complete else "needs_inputs",
        "source_id": value["source_id"], "origin": c["origin"], "bundle_identity": bundle_identity,
        "manifest_digest": digest(value), "contract_digest": contract.contract_digest,
        "receipt_declaration_digest": receipt.declaration_digest, "review_digest": review["review_digest"],
        "file_identities": actual, "fact_evidence": evidence, "checks_complete": review["checks_complete"],
        "retained_evidence_verified": retained, "raw_byte_identity_verified": raw_verified,
        "onboarding_complete": complete, "missing_inputs": sorted(set(missing)),
        "independent_missing_inputs": list(receipt.pending_facts), "independent_authentication": "pending",
        "point_in_time": source_review["pit"], "real_source_ready": False, "execution_allowed": False,
        "numeric_market_rows_parsed": 0, "final_numeric_rows_parsed": 0, "full_dataset_validated": False,
        "scope": "retained_byte_identity_and_declaration_completeness_only",
        "limitations": list(source_review["limitations"]) + [
            "Retained evidence equality does not establish authenticity, applicable rights or semantics.",
            "No source certification, PIT certification, dataset registration or numerical execution is granted.",
            "Files are checked at inspection time; later mutation requires a new inspection.",
        ],
    }
    result["report_digest"] = digest(result)
    return result
