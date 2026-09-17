"""只追加的状态变化审计链。

每一次许可签发/暂停、事件判定、隔离、勘误与申诉裁决都追加一条
:class:`AuditEntry`；新条目对前一条的哈希做链式哈希，任何事后篡改
都会在 :meth:`Journal.verify` 处断裂。
"""

import hashlib
import json
from dataclasses import dataclass
from datetime import datetime, timezone
from decimal import Decimal
from typing import Any


def _canonical(value: Any) -> str:
    return json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        default=_json_default,
    )


def _json_default(obj: Any) -> Any:
    if isinstance(obj, Decimal):
        return f"decimal:{obj}"
    if isinstance(obj, (datetime,)):
        return obj.isoformat()
    if hasattr(obj, "__dict__"):
        return obj.__dict__
    return str(obj)


GENESIS_HASH = "0" * 64


@dataclass(frozen=True)
class AuditEntry:
    seq: int
    at: datetime
    actor: str
    action: str
    ref: str
    patrol_id: str | None
    payload: dict
    prev_hash: str
    entry_hash: str


class Journal:
    def __init__(self) -> None:
        self._entries: list[AuditEntry] = []

    @property
    def entries(self) -> tuple[AuditEntry, ...]:
        return tuple(self._entries)

    def record(
        self,
        *,
        action: str,
        actor: str,
        ref: str,
        payload: dict | None = None,
        patrol_id: str | None = None,
        at: datetime | None = None,
    ) -> AuditEntry:
        at = at or datetime.now(timezone.utc)
        prev_hash = self._entries[-1].entry_hash if self._entries else GENESIS_HASH
        seq = len(self._entries) + 1
        body = _canonical(
            {
                "seq": seq,
                "at": at,
                "actor": actor,
                "action": action,
                "ref": ref,
                "patrol_id": patrol_id,
                "payload": payload or {},
                "prev_hash": prev_hash,
            }
        )
        entry = AuditEntry(
            seq=seq,
            at=at,
            actor=actor,
            action=action,
            ref=ref,
            patrol_id=patrol_id,
            payload=payload or {},
            prev_hash=prev_hash,
            entry_hash=hashlib.sha256(body.encode("utf-8")).hexdigest(),
        )
        self._entries.append(entry)
        return entry

    def for_patrol(self, patrol_id: str) -> tuple[AuditEntry, ...]:
        return tuple(e for e in self._entries if e.patrol_id == patrol_id)

    def verify(self) -> bool:
        """重放整条哈希链，任何条目被增删改都会返回 False。"""
        prev = GENESIS_HASH
        for entry in self._entries:
            if entry.prev_hash != prev:
                return False
            body = _canonical(
                {
                    "seq": entry.seq,
                    "at": entry.at,
                    "actor": entry.actor,
                    "action": entry.action,
                    "ref": entry.ref,
                    "patrol_id": entry.patrol_id,
                    "payload": entry.payload,
                    "prev_hash": entry.prev_hash,
                }
            )
            if hashlib.sha256(body.encode("utf-8")).hexdigest() != entry.entry_hash:
                return False
            prev = entry.entry_hash
        return True
