from __future__ import annotations

import json
from datetime import datetime
from pathlib import Path
from typing import Any, Iterable

DATA_DIR = Path("data")
DATA_DIR.mkdir(exist_ok=True)


def _append_jsonl(path: Path, records: Iterable[dict[str, Any]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as f:
        for rec in records:
            f.write(json.dumps(rec, ensure_ascii=False) + "\n")


def log_opportunities(records: Iterable[dict[str, Any]], file_name: str = "opportunities.jsonl") -> None:
    _append_jsonl(DATA_DIR / file_name, records)


def log_trades(records: Iterable[dict[str, Any]], file_name: str = "trades.jsonl") -> None:
    _append_jsonl(DATA_DIR / file_name, records)


def timestamp() -> str:
    return datetime.utcnow().isoformat() + "Z"
