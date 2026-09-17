"""可持续采集监管台账。

一条断网巡护的处理流程：

1. :meth:`HarvestLedger.ingest_patrol` 接收设备离线签发的事件批次；
2. 按 ``(设备号, 设备序号)`` 去重（重传包只保留首条），其余事件按
   ``captured_at`` 归位，依采集时刻顺序逐条判定——先许可与物种，再空间
   边界（存疑从禁），最后四本限额账（许可/团队/地块/物种）；
3. 任一不通过进入隔离（quarantine），不参与合法收获计量；
4. 全部状态变化写入只追加审计链，勘误只追加、申诉由第三人复核。
"""

from dataclasses import dataclass
from datetime import date, datetime
from decimal import Decimal

from .audit import Journal
from .contracts import (
    AppealStatus,
    EventDisposition,
    HarvestEvent,
    HarvestPermit,
    PermitStatus,
    QuarantineReason,
)
from .geo import BoundaryVerdict, GeoPoint, evaluate_boundary
from .registry import PermitRecord, RegistryError, VersionedRegistry


class LedgerError(ValueError):
    pass


@dataclass(frozen=True)
class Correction:
    correction_id: str
    at: datetime
    actor: str
    old_mass_kg: Decimal
    new_mass_kg: Decimal
    note: str


@dataclass(frozen=True)
class Appeal:
    appeal_id: str
    opened_at: datetime
    opened_by: str
    status: AppealStatus
    decided_at: datetime | None = None
    reviewer: str | None = None
    rationale: str | None = None


@dataclass(frozen=True)
class EventRecord:
    event: HarvestEvent
    patrol_id: str
    officer: str
    reasons: tuple[QuarantineReason, ...]
    verdict: BoundaryVerdict | None
    duplicate_of: str | None = None
    corrections: tuple[Correction, ...] = ()
    appeal: Appeal | None = None

    @property
    def is_duplicate(self) -> bool:
        return self.duplicate_of is not None

    @property
    def effective_mass_kg(self) -> Decimal:
        return self.corrections[-1].new_mass_kg if self.corrections else self.event.mass_kg

    @property
    def is_legal(self) -> bool:
        """计入合法收获：判定通过，或申诉获解除；重复件与隔离件不计。"""
        if self.is_duplicate:
            return False
        if self.appeal is not None and self.appeal.status is AppealStatus.RELEASED:
            return True
        return self.event.disposition in (EventDisposition.COUNTED, EventDisposition.CORRECTED)

    @property
    def effective_disposition(self) -> EventDisposition:
        if self.is_duplicate:
            return EventDisposition.PENDING
        if self.appeal is not None and self.appeal.status is AppealStatus.RELEASED:
            return EventDisposition.CORRECTED if self.corrections else EventDisposition.COUNTED
        if self.corrections and self.event.disposition is EventDisposition.COUNTED:
            return EventDisposition.CORRECTED
        return self.event.disposition


def _season_of(day: date, registry: VersionedRegistry) -> str | None:
    candidate = str(day.year)
    try:
        registry.season(candidate)
    except RegistryError:
        return None
    return candidate


class HarvestLedger:
    def __init__(self, registry: VersionedRegistry, journal: Journal) -> None:
        self.registry = registry
        self.journal = journal
        self._events: dict[tuple[str, int], EventRecord] = {}
        # 重复上报件不覆盖首条，但按巡护留存以还原去重过程。
        self._duplicates: list[EventRecord] = []
        self._correction_seq = 0
        self._appeal_seq = 0

    # ---------------------------------------------------------------- 接收

    def ingest_patrol(
        self,
        patrol_id: str,
        officer: str,
        raw_events: list[HarvestEvent],
    ) -> list[EventRecord]:
        """接收一次巡护的断网事件包：去重、按采集时间归位、逐条判定。"""
        accepted: list[HarvestEvent] = []
        records: list[EventRecord] = []
        seen: set[tuple[str, int]] = set()
        for event in raw_events:
            key = (event.device_id, event.sequence)
            if key in self._events or key in seen:
                kept = self._events.get(key)
                dup = EventRecord(
                    event=event,
                    patrol_id=patrol_id,
                    officer=officer,
                    reasons=(),
                    verdict=None,
                    duplicate_of=f"{key[0]}#{key[1]}",
                )
                records.append(dup)
                self._duplicates.append(dup)
                self.journal.record(
                    action="event_deduplicated",
                    actor=officer,
                    ref=f"{key[0]}#{key[1]}",
                    patrol_id=patrol_id,
                    payload={
                        "kept_captured_at": (kept.event.captured_at if kept else event.captured_at).isoformat(),
                        "kept_received_at": (kept.event.received_at if kept else event.received_at).isoformat(),
                        "dropped_received_at": event.received_at.isoformat(),
                    },
                )
                continue
            seen.add(key)
            accepted.append(event)

        # 按实际采集时间归位（雨后补录、延迟同步在此恢复时间顺序）。
        accepted.sort(key=lambda e: (e.captured_at, e.device_id, e.sequence))
        for event in accepted:
            record = self._adjudicate(event, patrol_id, officer)
            self._events[(event.device_id, event.sequence)] = record
            records.append(record)
        return records

    # ---------------------------------------------------------------- 判定

    def _adjudicate(
        self, event: HarvestEvent, patrol_id: str, officer: str
    ) -> EventRecord:
        reasons: list[QuarantineReason] = []
        verdict: BoundaryVerdict | None = None
        permit_record: PermitRecord | None = None

        try:
            permit_record = self.registry.permit_record(event.permit_id)
        except RegistryError:
            reasons.append(QuarantineReason.NO_VALID_PERMIT)

        permit: HarvestPermit | None = permit_record.permit if permit_record else None
        day = event.captured_at.date()

        if permit_record is not None:
            reasons.extend(self._permit_time_reasons(permit_record, day))

            species = self.registry.species_at(permit.species_code, permit.survey_version)
            if species.protection_level.value == "strictly_protected":
                reasons.append(QuarantineReason.SPECIES_FORBIDDEN)

            # 空间判定永远执行并留存，即使其他项已失败——越界理由必须可还原。
            parcel = self.registry.parcel_at(permit.parcel_id, permit.parcel_version)
            verdict = evaluate_boundary(
                GeoPoint(event.latitude, event.longitude),
                event.accuracy_meters,
                parcel,
            )
            reasons.extend(self._boundary_reasons(verdict))

        # 四本限额账：只对空间与许可前提成立的事件计量。
        if not reasons and permit is not None:
            reasons.extend(self._quota_breaches(permit, event, day))

        disposition = (
            EventDisposition.QUARANTINED if reasons else EventDisposition.COUNTED
        )
        settled = HarvestEvent(
            device_id=event.device_id,
            sequence=event.sequence,
            permit_id=event.permit_id,
            captured_at=event.captured_at,
            received_at=event.received_at,
            latitude=event.latitude,
            longitude=event.longitude,
            accuracy_meters=event.accuracy_meters,
            mass_kg=event.mass_kg,
            disposition=disposition,
        )
        record = EventRecord(
            event=settled,
            patrol_id=patrol_id,
            officer=officer,
            reasons=tuple(reasons),
            verdict=verdict,
        )
        self.journal.record(
            action="event_quarantined" if reasons else "event_counted",
            actor=officer,
            ref=f"{event.device_id}#{event.sequence}",
            patrol_id=patrol_id,
            payload={
                "permit_id": event.permit_id,
                "captured_at": event.captured_at.isoformat(),
                "mass_kg": str(event.mass_kg),
                "reasons": [r.value for r in reasons],
            },
        )
        return record

    def _quota_breaches(
        self, permit: HarvestPermit, event: HarvestEvent, day: date
    ) -> list[QuarantineReason]:
        breaches: list[QuarantineReason] = []
        projected = self._permit_used(permit.permit_id) + event.mass_kg
        if projected > permit.quota_kg:
            breaches.append(QuarantineReason.PERMIT_QUOTA_EXCEEDED)

        season_id = _season_of(day, self.registry)
        if season_id is not None:
            limits = self.registry.season(season_id)
            checks = (
                (limits.team(permit.team_id), self._team_used, permit.team_id,
                 QuarantineReason.TEAM_QUOTA_EXCEEDED),
                (limits.parcel(permit.parcel_id), self._parcel_used, permit.parcel_id,
                 QuarantineReason.PARCEL_QUOTA_EXCEEDED),
                (limits.species(permit.species_code), self._species_used, permit.species_code,
                 QuarantineReason.SPECIES_QUOTA_EXCEEDED),
            )
            for cap, used_fn, key, reason in checks:
                if cap is not None and used_fn(season_id, key) + event.mass_kg > cap:
                    breaches.append(reason)
        return breaches

    # ---------------------------------------------------------------- 余额

    def _legal_records(self) -> list[EventRecord]:
        return [r for r in self._events.values() if r.is_legal]

    def _permit_used(self, permit_id: str) -> Decimal:
        return sum(
            (r.effective_mass_kg for r in self._legal_records()
             if r.event.permit_id == permit_id),
            Decimal(0),
        )

    def _season_keys(self, record: EventRecord) -> tuple[str, str, str, str] | None:
        try:
            permit = self.registry.permit_record(record.event.permit_id).permit
        except RegistryError:
            return None
        season_id = _season_of(record.event.captured_at.date(), self.registry)
        if season_id is None:
            return None
        return season_id, permit.team_id, permit.parcel_id, permit.species_code

    def _team_used(self, season_id: str, team_id: str) -> Decimal:
        total = Decimal(0)
        for r in self._legal_records():
            keys = self._season_keys(r)
            if keys and keys[0] == season_id and keys[1] == team_id:
                total += r.effective_mass_kg
        return total

    def _parcel_used(self, season_id: str, parcel_id: str) -> Decimal:
        total = Decimal(0)
        for r in self._legal_records():
            keys = self._season_keys(r)
            if keys and keys[0] == season_id and keys[2] == parcel_id:
                total += r.effective_mass_kg
        return total

    def _species_used(self, season_id: str, species_code: str) -> Decimal:
        total = Decimal(0)
        for r in self._legal_records():
            keys = self._season_keys(r)
            if keys and keys[0] == season_id and keys[3] == species_code:
                total += r.effective_mass_kg
        return total

    def permit_balance(self, permit_id: str) -> dict:
        record = self.registry.permit_record(permit_id)
        used = self._permit_used(permit_id)
        return {
            "permit_id": permit_id,
            "status": record.status.value,
            "quota_kg": record.permit.quota_kg,
            "used_kg": used,
            "remaining_kg": record.permit.quota_kg - used,
        }

    # ---------------------------------------------------------------- 暂停

    def suspend_permit(
        self, permit_id: str, actor: str, day: date, reason: str,
        patrol_id: str | None = None,
    ) -> PermitRecord:
        """仅尚未使用（无任何合法收获）的许可可以暂停。"""
        record = self.registry.permit_record(permit_id)
        if record.status is PermitStatus.SUSPENDED:
            raise LedgerError(f"许可 {permit_id} 已处于暂停状态")
        if self._permit_used(permit_id) > 0:
            raise LedgerError(f"许可 {permit_id} 已产生合法采集记录，不能暂停，只能通过勘误/申诉处理")
        suspended = PermitRecord(
            permit=record.permit,
            approved_by=record.approved_by,
            approved_at=record.approved_at,
            status=PermitStatus.SUSPENDED,
            suspended_by=actor,
            suspended_at=day,
            suspend_reason=reason,
        )
        self.registry.replace_permit_record(suspended)
        self.journal.record(
            action="permit_suspended",
            actor=actor,
            ref=permit_id,
            payload={"at": day.isoformat(), "reason": reason},
            patrol_id=patrol_id,
        )
        return suspended

    # ---------------------------------------------------------------- 勘误

    def append_correction(
        self,
        device_id: str,
        sequence: int,
        actor: str,
        new_mass_kg: Decimal,
        note: str,
        at: datetime | None = None,
    ) -> Correction:
        record = self._require_record(device_id, sequence)
        if record.is_duplicate:
            raise LedgerError("重复上报件不接受勘误，请勘误首条记录")
        if new_mass_kg < 0:
            raise LedgerError("勘误质量不能为负数")
        # 已计入合法收获的记录，勘误上调不得把四本账顶破；
        # 需要按更大实际量计量时应先走申诉/限额调剂，勘误本身只据实留痕。
        if record.is_legal and new_mass_kg > record.effective_mass_kg:
            breaches = self._projected_quota_breaches(record, new_mass_kg)
            if breaches:
                raise LedgerError(
                    "勘误会使限额超限，不能直接计入：" + ", ".join(r.value for r in breaches)
                )
        at = at or datetime.now().astimezone()
        self._correction_seq += 1
        correction = Correction(
            correction_id=f"corr-{self._correction_seq}",
            at=at,
            actor=actor,
            old_mass_kg=record.effective_mass_kg,
            new_mass_kg=new_mass_kg,
            note=note,
        )
        updated = EventRecord(
            event=record.event,
            patrol_id=record.patrol_id,
            officer=record.officer,
            reasons=record.reasons,
            verdict=record.verdict,
            corrections=record.corrections + (correction,),
            appeal=record.appeal,
        )
        self._events[(device_id, sequence)] = updated
        self.journal.record(
            action="event_corrected",
            actor=actor,
            ref=f"{device_id}#{sequence}",
            patrol_id=record.patrol_id,
            payload={
                "correction_id": correction.correction_id,
                "old_mass_kg": str(correction.old_mass_kg),
                "new_mass_kg": str(new_mass_kg),
                "note": note,
            },
            at=at,
        )
        return correction

    # ---------------------------------------------------------------- 申诉

    def open_appeal(
        self,
        device_id: str,
        sequence: int,
        opened_by: str,
        at: datetime | None = None,
    ) -> Appeal:
        record = self._require_record(device_id, sequence)
        if record.event.disposition is not EventDisposition.QUARANTINED or record.appeal is not None:
            raise LedgerError("仅尚无申诉的隔离记录可以提出申诉")
        at = at or datetime.now().astimezone()
        self._appeal_seq += 1
        appeal = Appeal(
            appeal_id=f"appeal-{self._appeal_seq}",
            opened_at=at,
            opened_by=opened_by,
            status=AppealStatus.OPEN,
        )
        self._store_appeal(record, appeal)
        self.journal.record(
            action="appeal_opened",
            actor=opened_by,
            ref=f"{device_id}#{sequence}",
            patrol_id=record.patrol_id,
            payload={"appeal_id": appeal.appeal_id},
            at=at,
        )
        return appeal

    def decide_appeal(
        self,
        device_id: str,
        sequence: int,
        reviewer: str,
        release: bool,
        rationale: str,
        at: datetime | None = None,
    ) -> Appeal:
        """复核裁决：reviewer 不得是申诉人，也不得是原承办巡护员。"""
        record = self._require_record(device_id, sequence)
        if record.appeal is None or record.appeal.status is not AppealStatus.OPEN:
            raise LedgerError("该事件没有待裁决的申诉")
        if reviewer == record.appeal.opened_by:
            raise LedgerError("申诉不能由申诉提出人自行复核")
        if reviewer == record.officer:
            raise LedgerError("申诉不能由原承办巡护员复核，须由另一名复核人处理")

        if release:
            # 解除隔离即计入合法收获，必须重新通过许可/边界/限额校验，
            # 防止一次放行冲垮年度限额。
            blocking = self._release_blockers(record)
            if blocking:
                raise LedgerError(
                    "当前仍无法解除隔离：" + ", ".join(r.value for r in blocking)
                )

        at = at or datetime.now().astimezone()
        decided = Appeal(
            appeal_id=record.appeal.appeal_id,
            opened_at=record.appeal.opened_at,
            opened_by=record.appeal.opened_by,
            status=AppealStatus.RELEASED if release else AppealStatus.UPHELD,
            decided_at=at,
            reviewer=reviewer,
            rationale=rationale,
        )
        self._store_appeal(record, decided)
        self.journal.record(
            action="appeal_released" if release else "appeal_upheld",
            actor=reviewer,
            ref=f"{device_id}#{sequence}",
            patrol_id=record.patrol_id,
            payload={
                "appeal_id": decided.appeal_id,
                "rationale": rationale,
                "reasons": [r.value for r in record.reasons],
            },
            at=at,
        )
        return decided

    def _projected_quota_breaches(
        self, record: EventRecord, hypothetical_mass_kg: Decimal
    ) -> list[QuarantineReason]:
        """假设该事件按给定量计量（替换其当前贡献）时，四本账的超限项。"""
        event = record.event
        permit = self.registry.permit_record(event.permit_id).permit
        currently_counts = record.effective_mass_kg if record.is_legal else Decimal(0)
        delta = hypothetical_mass_kg - currently_counts

        breaches: list[QuarantineReason] = []
        if self._permit_used(permit.permit_id) + delta > permit.quota_kg:
            breaches.append(QuarantineReason.PERMIT_QUOTA_EXCEEDED)
        season_id = _season_of(event.captured_at.date(), self.registry)
        if season_id is not None:
            limits = self.registry.season(season_id)
            for cap, used, reason in (
                (limits.team(permit.team_id), self._team_used(season_id, permit.team_id),
                 QuarantineReason.TEAM_QUOTA_EXCEEDED),
                (limits.parcel(permit.parcel_id), self._parcel_used(season_id, permit.parcel_id),
                 QuarantineReason.PARCEL_QUOTA_EXCEEDED),
                (limits.species(permit.species_code),
                 self._species_used(season_id, permit.species_code),
                 QuarantineReason.SPECIES_QUOTA_EXCEEDED),
            ):
                if cap is not None and used + delta > cap:
                    breaches.append(reason)
        return breaches

    def _permit_time_reasons(
        self, record: PermitRecord, day: date
    ) -> list[QuarantineReason]:
        reasons: list[QuarantineReason] = []
        permit = record.permit
        if record.status is PermitStatus.SUSPENDED and not (
            record.suspended_at is not None and day < record.suspended_at
        ):
            reasons.append(QuarantineReason.PERMIT_SUSPENDED)
        if not permit.valid_from <= day <= permit.valid_until:
            reasons.append(QuarantineReason.PERMIT_EXPIRED)
        return reasons

    @staticmethod
    def _boundary_reasons(v: BoundaryVerdict) -> list[QuarantineReason]:
        if v.inside_core_zone and v.core_certain:
            return [QuarantineReason.CORE_ZONE]
        if not v.core_certain:
            return [QuarantineReason.CORE_ZONE_UNCERTAIN]
        if v.clearly_outside:
            return [QuarantineReason.OUTSIDE_PARCEL]
        if v.parcel_uncertain:
            return [QuarantineReason.BOUNDARY_UNCERTAIN]
        return []

    def _release_blockers(self, record: EventRecord) -> list[QuarantineReason]:
        """在“假设该事件不存在”的余额上重新判定其是否可以合法计入。"""
        event = record.event
        permit_record = self.registry.permit_record(event.permit_id)
        permit = permit_record.permit
        day = event.captured_at.date()
        blockers: list[QuarantineReason] = []

        blockers.extend(self._permit_time_reasons(permit_record, day))
        assert record.verdict is not None
        blockers.extend(self._boundary_reasons(record.verdict))

        mass = record.effective_mass_kg
        blockers.extend(self._projected_quota_breaches(record, mass))
        return blockers

    def _store_appeal(self, record: EventRecord, appeal: Appeal) -> None:
        self._events[(record.event.device_id, record.event.sequence)] = EventRecord(
            event=record.event,
            patrol_id=record.patrol_id,
            officer=record.officer,
            reasons=record.reasons,
            verdict=record.verdict,
            corrections=record.corrections,
            duplicate_of=record.duplicate_of,
            appeal=appeal,
        )

    # ---------------------------------------------------------------- 查询

    def _require_record(self, device_id: str, sequence: int) -> EventRecord:
        key = (device_id, sequence)
        if key not in self._events:
            raise LedgerError(f"事件不存在：{device_id}#{sequence}")
        return self._events[key]

    def record(self, device_id: str, sequence: int) -> EventRecord:
        return self._require_record(device_id, sequence)

    @property
    def events(self) -> tuple[EventRecord, ...]:
        return tuple(self._events.values())

    # -------------------------------------------------------- 巡护还原

    def reconstruct_patrol(self, patrol_id: str) -> dict:
        """按一次巡护还原许可余额、越界理由、申诉结果与全部状态变化。"""
        records = [
            r for r in self._events.values() if r.patrol_id == patrol_id
        ]
        records.extend(r for r in self._duplicates if r.patrol_id == patrol_id)
        records.sort(key=lambda r: (r.event.captured_at, r.event.device_id))

        permit_ids = {r.event.permit_id for r in records if not r.is_duplicate}
        balances = {}
        for pid in sorted(permit_ids):
            balance = self.permit_balance(pid)
            balances[pid] = {
                **balance,
                "quota_kg": str(balance["quota_kg"]),
                "used_kg": str(balance["used_kg"]),
                "remaining_kg": str(balance["remaining_kg"]),
            }

        event_views = []
        for r in records:
            event_views.append(self._event_view(r))

        timeline = [
            {
                "seq": e.seq,
                "at": e.at.isoformat(),
                "actor": e.actor,
                "action": e.action,
                "ref": e.ref,
                "payload": e.payload,
            }
            for e in self.journal.for_patrol(patrol_id)
        ]
        return {
            "patrol_id": patrol_id,
            "permit_balances": balances,
            "events": event_views,
            "audit_timeline": timeline,
            "journal_intact": self.journal.verify(),
        }

    def _event_view(self, r: EventRecord) -> dict:
        v = r.verdict
        view = {
            "event_ref": f"{r.event.device_id}#{r.event.sequence}",
            "permit_id": r.event.permit_id,
            "captured_at": r.event.captured_at.isoformat(),
            "received_at": r.event.received_at.isoformat(),
            "mass_kg": str(r.event.mass_kg),
            "effective_mass_kg": str(r.effective_mass_kg),
            "disposition": r.effective_disposition.value,
            "duplicate": r.is_duplicate,
            "quarantine_reasons": [x.value for x in r.reasons],
            "officer": r.officer,
        }
        if r.is_duplicate:
            view["duplicate_of"] = r.duplicate_of
        if v is not None:
            view["boundary"] = {
                "parcel_id": v.parcel_id,
                "parcel_version": v.parcel_version,
                "origin": {
                    "latitude": str(v.origin.latitude),
                    "longitude": str(v.origin.longitude),
                },
                "accuracy_meters": str(v.accuracy_meters),
                "inside_parcel": v.inside_parcel,
                "parcel_certain": v.parcel_certain,
                "distance_to_parcel_boundary_m": str(v.distance_to_parcel_boundary_m),
                "inside_core_zone": v.inside_core_zone,
                "core_zone_index": v.core_zone_index,
                "core_certain": v.core_certain,
                "distance_to_core_boundary_m": (
                    str(v.distance_to_core_boundary_m)
                    if v.distance_to_core_boundary_m is not None else None
                ),
            }
        if r.corrections:
            view["corrections"] = [
                {
                    "correction_id": c.correction_id,
                    "at": c.at.isoformat(),
                    "actor": c.actor,
                    "old_mass_kg": str(c.old_mass_kg),
                    "new_mass_kg": str(c.new_mass_kg),
                    "note": c.note,
                }
                for c in r.corrections
            ]
        if r.appeal is not None:
            a = r.appeal
            view["appeal"] = {
                "appeal_id": a.appeal_id,
                "opened_by": a.opened_by,
                "status": a.status.value,
                "reviewer": a.reviewer,
                "rationale": a.rationale,
                "decided_at": a.decided_at.isoformat() if a.decided_at else None,
            }
        return view
