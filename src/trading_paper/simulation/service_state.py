"""JSON checkpoints and hourly, compressed public event logs for a durable run."""
from __future__ import annotations

import gzip
import json
import os
import shutil
from dataclasses import fields, is_dataclass
from datetime import date, datetime, timezone
from decimal import Decimal
from pathlib import Path
from typing import Any

from trading_paper.simulation.live_engine import LiveAccount, LivePaperEngine, LivePosition
from trading_paper.simulation.models import Bar, Signal

_CLASSES = {cls.__name__: cls for cls in (LiveAccount, LivePosition, Bar, Signal)}


def _encode(value: Any) -> Any:
    if isinstance(value, Decimal):
        return {"$type": "Decimal", "value": str(value)}
    if isinstance(value, datetime):
        return {"$type": "datetime", "value": value.isoformat()}
    if isinstance(value, date):
        return {"$type": "date", "value": value.isoformat()}
    if is_dataclass(value):
        return {"$type": type(value).__name__, "fields": {f.name: _encode(getattr(value, f.name)) for f in fields(value)}}
    if isinstance(value, tuple):
        return {"$type": "tuple", "items": [_encode(item) for item in value]}
    if isinstance(value, list):
        return [_encode(item) for item in value]
    if isinstance(value, dict):
        return {key: _encode(item) for key, item in value.items()}
    return value


def _decode(value: Any) -> Any:
    if isinstance(value, list):
        return [_decode(item) for item in value]
    if not isinstance(value, dict):
        return value
    kind = value.get("$type")
    if kind == "Decimal":
        return Decimal(value["value"])
    if kind == "datetime":
        return datetime.fromisoformat(value["value"])
    if kind == "date":
        return date.fromisoformat(value["value"])
    if kind == "tuple":
        return tuple(_decode(item) for item in value["items"])
    if kind in _CLASSES:
        return _CLASSES[kind](**{key: _decode(item) for key, item in value["fields"].items()})
    if kind is not None:
        raise ValueError("Unsupported checkpoint value type")
    return {key: _decode(item) for key, item in value.items()}


def atomic_json(path: Path, value: Any) -> None:
    temporary = path.with_suffix(path.suffix + ".tmp")
    with temporary.open("w", encoding="utf-8") as stream:
        json.dump(value, stream, ensure_ascii=False, separators=(",", ":"))
        stream.flush()
        os.fsync(stream.fileno())
    temporary.replace(path)


def engine_state(engine: LivePaperEngine) -> dict[str, Any]:
    return _encode(vars(engine))


def restore_engine(value: dict[str, Any], config: dict[str, Any], markets: list[str], run_id: str) -> LivePaperEngine:
    decoded = _decode(value)
    if decoded.get("config") != config or decoded.get("markets") != markets or decoded.get("run_id") != run_id:
        raise ValueError("Checkpoint does not match this run/config; refusing to reset a portfolio")
    engine = LivePaperEngine(config, markets, run_id=run_id)
    vars(engine).update(decoded)
    return engine


class EventLog:
    def __init__(self, directory: Path):
        self.directory = directory
        directory.mkdir(parents=True, exist_ok=True)
        self.stream = None
        self.path: Path | None = None
        self.last_cursor: dict[str, Any] | None = None

    def record(self, event: dict[str, Any]) -> None:
        path = self.directory / (datetime.now(timezone.utc).strftime("%Y%m%dT%H") + ".jsonl")
        if path != self.path:
            self.close()
            self.path = path
            compressed = path.with_suffix(".jsonl.gz")
            if compressed.exists():
                if not path.exists():
                    temporary = path.with_suffix(".restore.tmp")
                    with gzip.open(compressed, "rb") as source, temporary.open("wb") as output:
                        shutil.copyfileobj(source, output)
                    temporary.replace(path)
                compressed.unlink()
            if path.exists():
                # A killed process may leave only the final JSON line incomplete.
                with path.open("rb+") as stream:
                    stream.seek(0, os.SEEK_END)
                    size = stream.tell()
                    stream.seek(max(0, size - 4 * 1024 * 1024))
                    tail = stream.read()
                    if tail and not tail.endswith(b"\n"):
                        newline = tail.rfind(b"\n")
                        if newline < 0 and size > len(tail):
                            raise ValueError("Cannot safely repair event log tail")
                        stream.truncate(size - len(tail) + newline + 1)
            self.stream = path.open("ab")
        self.stream.write((json.dumps(event, ensure_ascii=False, separators=(",", ":")) + "\n").encode("utf-8"))
        self.stream.flush()
        self.last_cursor = {"file": self.path.name, "offset": self.stream.tell()}

    def cursor(self) -> dict[str, Any] | None:
        if self.stream is not None:
            self.stream.flush()
            os.fsync(self.stream.fileno())
        return self.last_cursor

    def close(self) -> None:
        if self.stream is not None:
            self.stream.close()
            self.stream = None

    def compress_sealed(self, *, all_files: bool = False) -> None:
        active = datetime.now(timezone.utc).strftime("%Y%m%dT%H") + ".jsonl"
        for path in sorted(self.directory.glob("*.jsonl")):
            if not all_files and path.name == active:
                continue
            target = path.with_suffix(".jsonl.gz")
            temporary = target.with_suffix(".gz.tmp")
            with path.open("rb") as source, gzip.open(temporary, "wb", compresslevel=3) as output:
                while chunk := source.read(1024 * 1024):
                    output.write(chunk)
            temporary.replace(target)
            path.unlink()

    def after(self, cursor: dict[str, Any] | None):
        paths = {path.name.removesuffix(".gz"): path for path in self.directory.glob("*.jsonl.gz")}
        paths.update({path.name: path for path in self.directory.glob("*.jsonl")})
        if cursor and cursor["file"] not in paths:
            raise ValueError("Checkpoint event log is missing; refusing to lose recorded events")
        for name, path in sorted(paths.items()):
            if cursor and name < cursor["file"]:
                continue
            opener = gzip.open if path.suffix == ".gz" else open
            with opener(path, "rb") as stream:
                if cursor and name == cursor["file"]:
                    stream.seek(cursor["offset"])
                while line := stream.readline():
                    if not line.endswith(b"\n"):
                        break
                    self.last_cursor = {"file": name, "offset": stream.tell()}
                    yield json.loads(line)
