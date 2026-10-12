from __future__ import annotations

from contextlib import contextmanager
import fcntl
import hashlib
import json
import math
import os
from pathlib import Path


def json_text(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True, indent=2, allow_nan=False) + "\n"


def digest(value):
    return hashlib.sha256(json_text(value).encode()).hexdigest()


def read_json(path):
    def reject(value):
        raise ValueError(f"Invalid JSON numeric constant: {value}")
    def unique_pairs(pairs):
        value = {}
        for key, item in pairs:
            if key in value:
                raise ValueError(f"Duplicate JSON key: {key}")
            value[key] = item
        return value
    def finite_float(value):
        parsed = float(value)
        if not math.isfinite(parsed):
            raise ValueError(f"Nonfinite JSON number: {value}")
        return parsed
    return json.loads(Path(path).read_text(encoding="utf-8"), parse_constant=reject,
                      object_pairs_hook=unique_pairs, parse_float=finite_float)


def atomic_json(path, value):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    with tmp.open("w", encoding="utf-8") as stream:
        stream.write(json_text(value))
        stream.flush()
        os.fsync(stream.fileno())
    os.replace(tmp, path)


@contextmanager
def run_lock(path):
    path.mkdir(parents=True, exist_ok=True)
    with (path / ".lock").open("a") as stream:
        try:
            fcntl.flock(stream, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as e:
            raise ValueError("Run is already locked by another process") from e
        try:
            yield
        finally:
            fcntl.flock(stream, fcntl.LOCK_UN)
