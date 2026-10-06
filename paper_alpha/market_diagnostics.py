"""Stable, bounded locations for the strict market loader and dataset imports.

CSV rows count logical records, including the header and ignored blank records.
A quoted field spanning physical lines remains one logical record. Missing
observations and metadata errors deliberately have no invented CSV row number.
"""
from __future__ import annotations

import csv
import io
from itertools import islice
from threading import RLock


SAMPLE_LIMIT = 5
TEXT_LIMIT = 160
ROW_NUMBERING = "csv_logical_record_1_based_header_included"
_CSV_FIELD_LOCK = RLock()


def _text(value):
    return None if value is None else str(value)[:TEXT_LIMIT]


def location(*, row=None, column=None, date=None, asset=None, related_rows=(), value=None, reason=""):
    return {"row": row, "column": _text(column), "date": _text(date), "asset": _text(asset),
            "related_rows": list(islice(related_rows, SAMPLE_LIMIT)), "value": _text(value),
            "reason": _text(reason)}


class DataValidationError(ValueError):
    """One failed rule with bounded illustrative samples, never an error census."""
    def __init__(self, code, message, *, samples=(), at=None):
        super().__init__(message)
        self.code = code
        self.samples = list(islice(samples, SAMPLE_LIMIT))
        self.location = at if at is not None else (self.samples[0] if self.samples else None)


def require_data(condition, code, message, *, samples=(), at=None):
    if not condition:
        raise DataValidationError(code, message, samples=samples, at=at)


def csv_record_rows(payload, expected_columns, expected_rows):
    """Validate CSV structure and map dataframe offsets to logical records.

The file byte bound is imposed before this function. Parsing stops after one
    extra data record, matching the loader's bounded nrows read. The parser's
    process-global field guard is temporarily raised only to the already-bounded
    input length and restored under a lock, avoiding an accidental new 128 KiB
    restriction on identifiers accepted by the original loader. No raw parser
    exception text (which can echo arbitrary input) is returned.
"""
    try:
        text = payload.decode("utf-8-sig")
    except UnicodeDecodeError as exc:
        raise DataValidationError("invalid_csv_encoding", "CSV must be valid UTF-8") from exc
    if "\x00" in text:
        raise DataValidationError("invalid_csv_structure", "CSV cannot contain NUL characters")
    with _CSV_FIELD_LOCK:
        previous_limit = csv.field_size_limit()
        try:
            csv.field_size_limit(max(previous_limit, len(text)))
            return _record_rows(text, expected_columns, expected_rows)
        finally:
            csv.field_size_limit(previous_limit)


def _record_rows(text, expected_columns, expected_rows):
    # This pass maps records and checks widths, not competing quote semantics:
    # pandas accepts whitespace following a closing quote. Keep that established
    # input behavior and let its parser reject incomplete/malformed quoting.
    reader = csv.reader(io.StringIO(text, newline=""), strict=False)
    rows = []
    header_seen = False
    record = 0
    try:
        for record, fields in enumerate(reader, start=1):
            # pandas' default skip_blank_lines=True ignores these records too.
            if not fields or (len(fields) == 1 and not fields[0].strip()):
                continue
            if not header_seen:
                require_data(fields == expected_columns, "columns_mismatch", "CSV columns mismatch",
                             samples=[location(row=record, reason="header_columns")])
                header_seen = True
                continue
            require_data(len(fields) == len(expected_columns), "invalid_csv_structure",
                         "CSV records must contain exactly the declared number of columns",
                         samples=[location(row=record, reason="column_count", value=len(fields))])
            rows.append(record)
            if len(rows) > expected_rows:
                break
    except csv.Error as exc:
        # The iterator failed before yielding the next logical record.
        raise DataValidationError("invalid_csv_structure", "CSV contains malformed quoting or an oversized field",
                                  samples=[location(row=record + 1, reason="csv_parse_error")]) from exc
    require_data(header_seen, "columns_mismatch", "CSV columns mismatch")
    return rows
