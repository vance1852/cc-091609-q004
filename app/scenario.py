"""从巡护 fixture 构建注册表、台账并按时间顺序执行后续动作。

fixture 同时承载“断网巡护包”和“恢复在线后的处置动作（勘误/暂停/申诉）”，
加载器只做反序列化，所有判定规则仍在 :mod:`domain` 内完成。
"""

import json
from datetime import date, datetime
from decimal import Decimal
from pathlib import Path

from domain.contracts import (
    EventDisposition,
    GeoPoint,
    HarvestEvent,
    HarvestPermit,
    Parcel,
    ProtectionLevel,
    SeasonalLimits,
    Species,
)
from domain.ledger import HarvestLedger
from domain.registry import PermitRecord, VersionedRegistry
from domain.audit import Journal


def _point(pair: list) -> GeoPoint:
    return GeoPoint(Decimal(str(pair[0])), Decimal(str(pair[1])))


def _dt(value: str) -> datetime:
    return datetime.fromisoformat(value)


class Scenario:
    def __init__(self, data: dict) -> None:
        self.data = data
        self.registry = VersionedRegistry()
        self.journal = Journal()
        self.ledger = HarvestLedger(self.registry, self.journal)
        self._load()

    @classmethod
    def from_file(cls, path: str | Path) -> "Scenario":
        with open(path, encoding="utf-8") as fh:
            return cls(json.load(fh))

    def _load(self) -> None:
        d = self.data
        # 物种在两个调查版本下均登记，后续版本不影响已冻结的许可。
        self.registry.register_species(
            Species(d["speciesCode"], d["species"], ProtectionLevel.LICENSED,
                    d["surveyVersion"])
        )
        later = d.get("laterSurveyVersion")
        if later:
            self.registry.register_species(
                Species(d["speciesCode"], d["species"], ProtectionLevel.LICENSED, later)
            )

        for parcel in d["parcels"]:
            self.registry.register_parcel(
                Parcel(
                    parcel_id=parcel["id"],
                    parcel_version=parcel["version"],
                    polygon=tuple(_point(p) for p in parcel["polygon"]),
                    core_zones=tuple(
                        tuple(_point(p) for p in zone) for zone in parcel.get("coreZones", [])
                    ),
                )
            )

        limits = d.get("limits", {})
        self.registry.register_season(
            SeasonalLimits(
                season=d["season"],
                team_kg={k: Decimal(v) for k, v in limits.get("teamKg", {}).items()},
                parcel_kg={k: Decimal(v) for k, v in limits.get("parcelKg", {}).items()},
                species_kg={k: Decimal(v) for k, v in limits.get("speciesKg", {}).items()},
            )
        )

        for p in d["permits"]:
            permit = HarvestPermit(
                permit_id=p["id"],
                team_id=p["team"],
                species_code=d["speciesCode"],
                survey_version=p["surveyVersion"],
                parcel_id=p["parcel"],
                parcel_version=p["parcelVersion"],
                valid_from=date.fromisoformat(p["validFrom"]),
                valid_until=date.fromisoformat(p["validUntil"]),
                quota_kg=Decimal(p["quotaKg"]),
            )
            self.registry.add_permit(
                PermitRecord(
                    permit=permit,
                    approved_by=p["approvedBy"],
                    approved_at=date.fromisoformat(p["approvedAt"]),
                )
            )

    # ------------------------------------------------------------ 运行

    def run(self) -> dict:
        d = self.data
        events = [
            HarvestEvent(
                device_id=e["device"],
                sequence=e["sequence"],
                permit_id=e["permitId"],
                captured_at=_dt(e["capturedAt"]),
                received_at=_dt(e["receivedAt"]),
                latitude=Decimal(str(e["lat"])),
                longitude=Decimal(str(e["lon"])),
                accuracy_meters=Decimal(e["accuracyMeters"]),
                mass_kg=Decimal(e["massKg"]),
                disposition=EventDisposition.PENDING,
            )
            for e in d["events"]
        ]
        self.ledger.ingest_patrol(d["patrolId"], d["officer"], events)

        # 恢复在线后的处置动作按记录时间顺序执行。
        for action in sorted(d.get("actions", []), key=lambda a: a["at"]):
            self._apply_action(action)

        return self.ledger.reconstruct_patrol(d["patrolId"])

    def _apply_action(self, a: dict) -> None:
        kind = a["type"]
        patrol_id = self.data["patrolId"]
        if kind == "correction":
            self.ledger.append_correction(
                a["device"], a["sequence"], a["actor"],
                Decimal(a["newMassKg"]), a["note"], at=_dt(a["at"]),
            )
        elif kind == "suspend":
            self.ledger.suspend_permit(
                a["permitId"], a["actor"], date.fromisoformat(a["at"]), a["reason"],
                patrol_id=patrol_id,
            )
        elif kind == "appealOpen":
            self.ledger.open_appeal(
                a["device"], a["sequence"], a["openedBy"], at=_dt(a["at"])
            )
        elif kind == "appealDecide":
            self.ledger.decide_appeal(
                a["device"], a["sequence"], a["reviewer"],
                a["release"], a["rationale"], at=_dt(a["at"]),
            )
        else:
            raise ValueError(f"未知动作类型：{kind}")
