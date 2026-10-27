"""Atomically update persisted rows and JSON artifacts for claim actions."""

from __future__ import annotations

import json
import time
from pathlib import Path
from typing import Any


ARTIFACT_WRITE_POLICY = 'bounded-replace-retry-existing-temp-v1'
_REPLACE_DELAYS = (.025, .05, .1, .2, .4, .8, 1., 1.)


def _publish(temporary: Path, path: Path) -> None:
    """Keep the existing .tmp snapshot; wait briefly for Windows reader locks.

    Do not truncate/delete the destination or regenerate model responses.
    A persistent failure still raises and leaves the complete .tmp for recovery.
    Persistence must also work while recording a stopped job, so cancellation
    does not interrupt these bounded commit attempts.
    """
    for attempt in range(len(_REPLACE_DELAYS) + 1):
        try:
            temporary.replace(path)
            return
        except OSError as exc:
            if getattr(exc, 'winerror', None) not in {5, 32, 33} or attempt == len(_REPLACE_DELAYS):
                raise
            time.sleep(_REPLACE_DELAYS[attempt])

def save_rows(path: Path, rows: list[dict[str, Any]]) -> None:
    """Readers see either the old or the complete new JSONL file."""
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(path.name + '.tmp')
    temporary.write_text(''.join(json.dumps(row, ensure_ascii=False) + '\n' for row in rows), encoding='utf-8')
    _publish(temporary, path)


def save_json(path: Path, value: Any) -> None:
    temporary = path.with_name(path.name + '.tmp')
    temporary.write_text(json.dumps(value, ensure_ascii=False, indent=2), encoding='utf-8')
    _publish(temporary, path)


def replace_row(rows: list[dict[str, Any]], row: dict[str, Any]) -> list[dict[str, Any]]:
    return [row if item['candidate_id'] == row['candidate_id'] else item for item in rows] if any(
        item['candidate_id'] == row['candidate_id'] for item in rows) else [*rows, row]
