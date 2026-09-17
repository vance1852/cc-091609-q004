"""可持续采集监管台账（append-only 事件溯源）。

所有状态变化都以不可篡改的日志条目保存：资源调查/边界版本发布、许可审批
（冻结当时的调查数据与地理边界快照）、离线事件签收、状态转换、勘误与申诉。

事件由现场设备按 ``(设备, 序号)`` 幂等签收；恢复联网后按**采集时间**归位，
统一重算许可、团队年度、地块-物种年度、物种年度四层限额。越界、进入核心区、
超任一限额的记录裁定为隔离（quarantined），不计入合法收获。

重放（:meth:`Ledger.replay`）只依赖日志条目即可还原全部当前状态，并独立重算
最终裁定与日志中的状态转换链互相校验；任意一条被篡改都会在重放时暴露。
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass, field
from datetime import datetime
from decimal import Decimal
from pathlib import Path
from typing import Any, Iterable

from . import geo

GENESIS = "0" * 64

# ---------------------------------------------------------------------------
# 违规原因代码
# ---------------------------------------------------------------------------
R_PERMIT_UNKNOWN = "permit_unknown"
R_PERMIT_SUSPENDED = "permit_suspended"
R_OUTSIDE_VALIDITY = "outside_validity"
R_OUTSIDE_PARCEL = "outside_parcel"
R_BUFFER_PARCEL = "accuracy_buffer_crosses_parcel"
R_INSIDE_CORE = "inside_core_zone"
R_BUFFER_CORE = "accuracy_buffer_crosses_core_zone"
R_PERMIT_QUOTA = "permit_quota_exceeded"
R_TEAM_QUOTA = "team_annual_quota_exceeded"
R_PARCEL_SPECIES_QUOTA = "parcel_species_annual_quota_exceeded"
R_SPECIES_QUOTA = "species_annual_quota_exceeded"
R_MASS_INVALID = "mass_invalid"

SPATIAL_REASONS = {R_OUTSIDE_PARCEL, R_BUFFER_PARCEL, R_INSIDE_CORE, R_BUFFER_CORE}


def _D(value: Any) -> Decimal:
    return value if isinstance(value, Decimal) else Decimal(str(value))


def _now() -> str:
    return datetime.now().astimezone().isoformat(timespec="seconds")


def _dt(value: Any) -> datetime:
    return value if isinstance(value, datetime) else datetime.fromisoformat(str(value))


class LedgerError(ValueError):
    """台账规则冲突（重复序号、版本重发、越权操作等）。"""


class DuplicateSequence(LedgerError):
    """同一设备重复上报序号；内容一致为重复提交，不一致为序号伪造。"""


# ---------------------------------------------------------------------------
# 日志条目
# ---------------------------------------------------------------------------
@dataclass
class Entry:
    seq: int
    at: str
    actor: str
    type: str
    payload: dict[str, Any]
    prev_hash: str = GENESIS
    hash: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "seq": self.seq,
            "at": self.at,
            "actor": self.actor,
            "type": self.type,
            "payload": self.payload,
            "prev_hash": self.prev_hash,
            "hash": self.hash,
        }


def entry_hash(prev_hash: str, body: dict[str, Any]) -> str:
    material = json.dumps(body, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256((prev_hash + material).encode("utf-8")).hexdigest()


# ---------------------------------------------------------------------------
# 读模型
# ---------------------------------------------------------------------------
@dataclass
class Violation:
    code: str
    detail: dict[str, Any]


@dataclass
class EventView:
    key: str
    device_id: str
    sequence: int
    permit_id: str
    patrol_id: str | None
    captured_at: datetime
    received_at: datetime
    raw: dict[str, Any]
    """原始上报值（定位原值永不覆盖）。"""
    current: dict[str, Any]
    """叠加最新勘误后的判定用值。"""
    disposition: str
    """pending / counted / quarantined / corrected。"""
    effective: str
    """勘误/申诉后实际裁定：counted / quarantined / pending。"""
    reasons: list[Violation]
    waived_reasons: list[str]
    corrections: list[dict[str, Any]] = field(default_factory=list)
    appeal: dict[str, Any] | None = None
    transitions: list[dict[str, Any]] = field(default_factory=list)

    @property
    def mass_kg(self) -> Decimal:
        return _D(self.current["mass_kg"])


@dataclass
class PermitView:
    permit_id: str
    team_id: str
    species_code: str
    parcel_id: str
    survey_version: str
    parcel_version: str
    valid_from: str
    valid_until: str
    quota_kg: Decimal
    approver: str
    status: str
    frozen_survey: dict[str, Any]
    frozen_parcel: dict[str, Any]
    counted_kg: Decimal = Decimal(0)

    @property
    def remaining_kg(self) -> Decimal:
        return self.quota_kg - self.counted_kg


# ---------------------------------------------------------------------------
# 台账
# ---------------------------------------------------------------------------
class Ledger:
    def __init__(self) -> None:
        self._entries: list[Entry] = []

    # ----- 日志读写 -------------------------------------------------------
    def _append(self, etype: str, payload: dict[str, Any], actor: str) -> Entry:
        entry = Entry(len(self._entries) + 1, _now(), actor, etype, payload)
        prev_hash = self._entries[-1].hash if self._entries else GENESIS
        body = {k: entry.to_dict()[k] for k in ("seq", "at", "actor", "type", "payload")}
        entry.prev_hash = prev_hash
        entry.hash = entry_hash(prev_hash, body)
        self._entries.append(entry)
        return entry

    @property
    def entries(self) -> tuple[Entry, ...]:
        return tuple(self._entries)

    def save(self, path: str | Path) -> None:
        Path(path).write_text(
            "\n".join(json.dumps(e.to_dict(), ensure_ascii=False, sort_keys=True) for e in self._entries),
            encoding="utf-8",
        )

    @classmethod
    def load(cls, path: str | Path) -> "Ledger":
        ledger = cls()
        text = Path(path).read_text(encoding="utf-8")
        prev = GENESIS
        for i, line in enumerate(filter(None, text.splitlines()), start=1):
            d = json.loads(line)
            if d["seq"] != i:
                raise LedgerError("日志序号不连续，台账可能被截断或篡改")
            recorded_hash = d.pop("hash")
            recorded_prev = d["prev_hash"]
            if recorded_prev != prev:
                raise LedgerError(f"第 {i} 条哈希链断裂，台账可能被篡改")
            body = {k: d[k] for k in ("seq", "at", "actor", "type", "payload")}
            if entry_hash(prev, body) != recorded_hash:
                raise LedgerError(f"第 {i} 条哈希校验失败，台账可能被篡改")
            ledger._entries.append(
                Entry(d["seq"], d["at"], d["actor"], d["type"], d["payload"],
                      prev_hash=recorded_prev, hash=recorded_hash)
            )
            prev = recorded_hash
        return ledger

    # ----- 基础资料发布（版本化、不可变） ---------------------------------
    def publish_survey(
        self,
        version: str,
        species: dict[str, dict[str, Any]],
        actor: str,
        effective_from: str | None = None,
    ) -> Entry:
        """发布资源调查版本。

        species: ``{物种代码: {name, protection_level, annual_quota_kg}}``。
        版本一经发布即冻结，重发同号版本直接拒绝。
        """

        state = self.snapshot()
        if version in state.surveys:
            raise LedgerError(f"调查版本 {version} 已冻结，不能覆盖")
        snap = {
            code: {
                "name": rec.get("name", code),
                "protection_level": rec["protection_level"],
                "annual_quota_kg": str(_D(rec["annual_quota_kg"])),
            }
            for code, rec in species.items()
        }
        return self._append(
            "survey-published",
            {"version": version, "effective_from": effective_from, "species": snap},
            actor,
        )

    def publish_parcel_layer(
        self,
        version: str,
        parcels: dict[str, dict[str, Any]],
        core_zones: dict[str, list] | None = None,
        actor: str = "registry",
        effective_from: str | None = None,
    ) -> Entry:
        """发布地块边界版本。

        parcels: ``{地块号: {ring: [[lat,lon]...], species_quota_kg: {物种: 限额}}}``；
        core_zones: ``{核心区编号: ring}``。审批许可时会冻结整层快照。
        重叠申报不阻断发布（纸质许可确实可能重叠），但写入重叠告警。
        """

        state = self.snapshot()
        if version in state.parcel_layers:
            raise LedgerError(f"边界版本 {version} 已冻结，不能覆盖")
        rings = {pid: p["ring"] for pid, p in parcels.items()}
        overlaps = geo.find_overlaps(rings)
        layer = {
            "version": version,
            "effective_from": effective_from,
            "parcels": {
                pid: {
                    "ring": [[str(x) for x in pt] for pt in p["ring"]],
                    "species_quota_kg": {
                        s: str(_D(q)) for s, q in p.get("species_quota_kg", {}).items()
                    },
                }
                for pid, p in parcels.items()
            },
            "core_zones": {
                cid: [[str(x) for x in pt] for pt in ring]
                for cid, ring in (core_zones or {}).items()
            },
        }
        entry = self._append("parcel-layer-published", layer, actor)
        if overlaps:
            self._append(
                "parcel-overlap-warning",
                {"parcel_layer_version": version, "overlaps": [[a, b] for a, b in overlaps]},
                actor,
            )
        return entry

    def register_team(self, team_id: str, name: str, annual_quota_kg: Any, actor: str) -> Entry:
        state = self.snapshot()
        if team_id in state.teams:
            raise LedgerError(f"团队 {team_id} 已登记")
        return self._append(
            "team-registered",
            {"team_id": team_id, "name": name, "annual_quota_kg": str(_D(annual_quota_kg))},
            actor,
        )

    # ----- 许可审批（冻结版本与地理范围） ---------------------------------
    def approve_permit(
        self,
        permit_id: str,
        team_id: str,
        species_code: str,
        survey_version: str,
        parcel_version: str,
        parcel_id: str,
        valid_from: str,
        valid_until: str,
        quota_kg: Any,
        approver: str,
    ) -> Entry:
        state = self.snapshot()
        if permit_id in state.permits:
            raise LedgerError(f"许可 {permit_id} 已存在")
        if survey_version not in state.surveys:
            raise LedgerError(f"调查版本 {survey_version} 未发布")
        layer = state.parcel_layers.get(parcel_version)
        if layer is None:
            raise LedgerError(f"边界版本 {parcel_version} 未发布")
        if species_code not in state.surveys[survey_version]:
            raise LedgerError(f"调查版本 {survey_version} 中没有物种 {species_code}")
        if parcel_id not in layer["parcels"]:
            raise LedgerError(f"边界版本 {parcel_version} 中没有地块 {parcel_id}")
        if team_id not in state.teams:
            raise LedgerError(f"团队 {team_id} 未登记")
        if valid_from > valid_until:
            raise LedgerError("许可有效期起始晚于截止")
        if _D(quota_kg) <= 0:
            raise LedgerError("许可限额必须为正")
        payload = {
            "permit_id": permit_id,
            "team_id": team_id,
            "species_code": species_code,
            "survey_version": survey_version,
            "parcel_version": parcel_version,
            "parcel_id": parcel_id,
            "valid_from": valid_from,
            "valid_until": valid_until,
            "quota_kg": str(_D(quota_kg)),
            "approver": approver,
            # 冻结快照：此后调查/边界再发新版，本许可仍按审批时的版本判定
            "survey_snapshot": state.surveys[survey_version][species_code],
            "parcel_snapshot": layer["parcels"][parcel_id],
            "core_zones_snapshot": layer["core_zones"],
        }
        return self._append("permit-approved", payload, approver)

    def suspend_permit(self, permit_id: str, actor: str, reason: str) -> Entry:
        """暂停尚未使用（无任何采集记录）的许可。"""

        state = self.snapshot()
        permit = state.permits.get(permit_id)
        if permit is None:
            raise LedgerError(f"许可 {permit_id} 不存在")
        if permit["status"] != "active":
            raise LedgerError(f"许可状态为 {permit['status']}，不能暂停")
        used = [k for k, e in state.events.items() if e["raw"]["permit_id"] == permit_id]
        if used:
            raise LedgerError("许可已有采集记录，不能暂停；只能对记录追加勘误")
        return self._append(
            "permit-suspended",
            {"permit_id": permit_id, "reason": reason},
            actor,
        )

    # ----- 巡护批次 -------------------------------------------------------
    def open_patrol(self, patrol_id: str, ranger: str, note: str = "") -> Entry:
        state = self.snapshot()
        if patrol_id in state.patrols:
            raise LedgerError(f"巡护 {patrol_id} 已开启")
        return self._append(
            "patrol-opened", {"patrol_id": patrol_id, "ranger": ranger, "note": note}, ranger
        )

    def finalize_patrol(self, patrol_id: str, actor: str) -> Entry:
        state = self.snapshot()
        if state.patrols.get(patrol_id, {}).get("status") != "open":
            raise LedgerError(f"巡护 {patrol_id} 未处于开启状态")
        return self._append("patrol-finalized", {"patrol_id": patrol_id}, actor)

    # ----- 离线事件签收 ---------------------------------------------------
    def ingest_event(
        self,
        *,
        device_id: str,
        sequence: int,
        permit_id: str,
        captured_at: Any,
        received_at: Any,
        latitude: Any,
        longitude: Any,
        accuracy_meters: Any,
        mass_kg: Any,
        actor: str,
        patrol_id: str | None = None,
        offline: bool = True,
    ) -> Entry:
        """签收一条现场采集事件（断网时由设备本地签发序号）。

        同一 ``(device_id, sequence)`` 重复上报：内容完全一致视为断网重传，
        写入去重审计条目后忽略；内容不一致则拒绝（序号被伪造/错位）。
        """

        state = self.snapshot()
        if patrol_id is not None:
            patrol = state.patrols.get(patrol_id)
            if patrol is None:
                raise LedgerError(f"巡护 {patrol_id} 不存在，请先开启")
            if patrol["status"] != "open":
                raise LedgerError(f"巡护 {patrol_id} 已结案，不能补录")
        key = f"{device_id}#{sequence}"
        if key in state.events:
            existing = state.events[key]["raw"]
            candidate = {
                "permit_id": permit_id,
                "captured_at": _dt(captured_at).isoformat(),
                "latitude": str(_D(latitude)),
                "longitude": str(_D(longitude)),
                "accuracy_meters": str(_D(accuracy_meters)),
                "mass_kg": str(_D(mass_kg)),
            }
            comparable = {k: existing[k] for k in candidate}
            if comparable == candidate:
                return self._append(
                    "duplicate-suppressed",
                    {"event_key": key, "patrol_id": patrol_id, "reason": "offline_retry_identical"},
                    actor,
                )
            raise DuplicateSequence(
                f"设备 {device_id} 序号 {sequence} 已用于另一条记录，疑似序号伪造"
            )

        raw = {
            "device_id": device_id,
            "sequence": sequence,
            "permit_id": permit_id,
            "patrol_id": patrol_id,
            "captured_at": _dt(captured_at).isoformat(),
            "received_at": _dt(received_at).isoformat(),
            "latitude": str(_D(latitude)),
            "longitude": str(_D(longitude)),
            "accuracy_meters": str(_D(accuracy_meters)),
            "mass_kg": str(_D(mass_kg)),
            "offline": offline,
        }
        ingested = self._append("event-ingested", {"event_key": key, "raw": raw}, actor)
        # 快照已包含刚追加的 event-ingested，直接重算
        self._recompute(self.snapshot(), actor, trigger={"event_key": key})
        return ingested

    # ----- 勘误（只追加，不改原始记录） -----------------------------------
    def append_erratum(
        self,
        event_key: str,
        corrections: dict[str, Any],
        actor: str,
        reason: str,
    ) -> Entry:
        """对已发生记录追加勘误。可修正定位与重量；原值随勘误保留。"""

        state = self.snapshot()
        ev = state.events.get(event_key)
        if ev is None:
            raise LedgerError(f"记录 {event_key} 不存在")
        allowed = {"latitude", "longitude", "accuracy_meters", "mass_kg"}
        unknown = set(corrections) - allowed
        if unknown:
            raise LedgerError(f"勘误字段不允许：{sorted(unknown)}")
        original = {k: ev["current"][k] for k in corrections}
        snap = {
            "event_key": event_key,
            "reason": reason,
            "original": original,
            "corrections": {k: str(_D(v)) for k, v in corrections.items()},
        }
        self._append("erratum-appended", snap, actor)
        state = self.snapshot()
        self._recompute(state, actor, trigger={"erratum_for": event_key})
        return self._entries[-1]

    # ----- 申诉（另一名复核人） -------------------------------------------
    def file_appeal(self, event_key: str, appellant: str, grounds: str) -> Entry:
        state = self.snapshot()
        ev = state.events.get(event_key)
        if ev is None:
            raise LedgerError(f"记录 {event_key} 不存在")
        if state.effective.get(event_key) != "quarantined":
            raise LedgerError("仅隔离记录可以申诉")
        if ev.get("appeal") and ev["appeal"]["status"] == "open":
            raise LedgerError("该记录已有待处理申诉")
        return self._append(
            "appeal-filed",
            {"event_key": event_key, "appellant": appellant, "grounds": grounds},
            appellant,
        )

    def decide_appeal(
        self,
        event_key: str,
        reviewer: str,
        uphold: bool,
        rationale: str,
        waive_reasons: Iterable[str] = (),
        corrections: dict[str, Any] | None = None,
    ) -> Entry:
        """复核人裁定申诉。

        - uphold=True：维持隔离。
        - uphold=False：撤销隔离。可附带勘误（新证据定位/重量），或对判定原因
          逐条 waive（须写明理由，例如缓冲圆按更精确大地线复核后确认不跨界）。
          复核人不得是申诉发起人，也不得是该许可的审批人（职责分离）。
        """

        state = self.snapshot()
        ev = state.events.get(event_key)
        if ev is None:
            raise LedgerError(f"记录 {event_key} 不存在")
        appeal = ev.get("appeal")
        if appeal is None or appeal["status"] != "open":
            raise LedgerError("该记录没有待裁定的申诉")
        if reviewer == appeal["appellant"]:
            raise LedgerError("申诉必须由另一名复核人处理")
        permit = state.permits.get(ev["raw"]["permit_id"])
        if permit and reviewer == permit["approver"]:
            raise LedgerError("复核人不能是该许可的审批人")
        waive = sorted(set(waive_reasons))
        unknown = [r for r in waive if not r.startswith(("permit_", "outside_", "accuracy_", "inside_", "team_", "parcel_", "species_", "mass_"))]
        if unknown:
            raise LedgerError(f"未知违规代码：{unknown}")
        decision = {
            "event_key": event_key,
            "reviewer": reviewer,
            "uphold": uphold,
            "rationale": rationale,
            "waived_reasons": waive if not uphold else [],
            "corrections": {k: str(_D(v)) for k, v in (corrections or {}).items()},
        }
        self._append("appeal-decided", decision, reviewer)
        if not uphold:
            trial = self.snapshot()
            trial_eff = self._adjudicate(trial)[event_key][0]
            if trial_eff != "counted":
                self._entries.pop()
                raise LedgerError(
                    "撤销隔离必须通过勘误或逐条豁免消除全部违规原因（含限额），"
                    f"当前重算结果仍为 {trial_eff}"
                )
        state = self.snapshot()
        self._recompute(state, reviewer, trigger={"appeal_for": event_key})
        return self._entries[-1]

    # ======================================================================
    # 裁定引擎：按采集时间归位，重算四层限额与空间合规
    # ======================================================================
    def _recompute(self, state: "Projection", actor: str, trigger: dict[str, str]) -> None:
        """对全部在途记录重算裁定，并把与现状不同的结果以状态转换追加日志。"""

        result = self._adjudicate(state)
        for key in sorted(result, key=lambda k: state.events[k]["order"]):
            new_eff, reasons, evidence = result[key]
            old = state.effective.get(key, "pending")
            old_reasons = [
                (r["code"], r["detail"]) if isinstance(r, dict) else (r[0], r[1])
                for r in state.reasons.get(key, [])
            ]
            if new_eff == old and old_reasons == reasons:
                continue
            self._append(
                "status-transition",
                {
                    "event_key": key,
                    "trigger": trigger,
                    "from_status": old,
                    "to_status": new_eff,
                    "reasons": [{"code": c, "detail": d} for c, d in reasons],
                    "evidence": evidence,
                },
                actor,
            )
            state = state.with_transition(self._entries[-1].payload)

    def _adjudicate(
        self, state: "Projection"
    ) -> dict[str, tuple[str, list[tuple[str, dict]], dict]]:
        """纯函数：当前全部记录的预期裁定。

        空间/资格类原因逐记录判定；限额类原因按采集时间归位后，在四个累计
        维度上依次分配，先占先得，超限记录隔离且不占用后续额度。
        """

        keys = sorted(state.events, key=lambda k: state.events[k]["order"])
        base: dict[str, list[tuple[str, dict]]] = {}
        evidence: dict[str, dict] = {}

        for key in keys:
            cur = state.events[key]["current"]
            reasons: list[tuple[str, dict]] = []
            ev: dict[str, Any] = {
                "original_point": {
                    "latitude": state.events[key]["raw"]["latitude"],
                    "longitude": state.events[key]["raw"]["longitude"],
                    "accuracy_meters": state.events[key]["raw"]["accuracy_meters"],
                },
                "judged_point": {
                    "latitude": cur["latitude"],
                    "longitude": cur["longitude"],
                    "accuracy_meters": cur["accuracy_meters"],
                },
            }
            permit = state.permits.get(cur["permit_id"])
            mass = _D(cur["mass_kg"])
            if mass <= 0:
                reasons.append((R_MASS_INVALID, {"mass_kg": str(mass)}))
            if permit is None:
                reasons.append((R_PERMIT_UNKNOWN, {"permit_id": cur["permit_id"]}))
            else:
                if permit["status"] == "suspended":
                    reasons.append((R_PERMIT_SUSPENDED, {"permit_id": permit["permit_id"]}))
                captured = cur["captured_at"][:10]
                if not (permit["valid_from"] <= captured <= permit["valid_until"]):
                    reasons.append(
                        (
                            R_OUTSIDE_VALIDITY,
                            {
                                "captured_on": captured,
                                "valid_from": permit["valid_from"],
                                "valid_until": permit["valid_until"],
                            },
                        )
                    )
                # 空间判定：精度圆必须整体在地块内、整体在核心区外
                parcel_v = geo.classify_against_parcel(
                    permit["parcel_snapshot"]["ring"],
                    _D(cur["latitude"]),
                    _D(cur["longitude"]),
                    _D(cur["accuracy_meters"]),
                )
                if parcel_v.verdict == "outside":
                    reasons.append((R_OUTSIDE_PARCEL, parcel_v.as_evidence()))
                elif parcel_v.verdict == "buffer_crosses":
                    reasons.append((R_BUFFER_PARCEL, parcel_v.as_evidence()))
                ev["parcel"] = {
                    "parcel_id": permit["parcel_id"],
                    "parcel_version": permit["parcel_version"],
                    **parcel_v.as_evidence(),
                }
                for cid, ring in permit["core_zones_snapshot"].items():
                    core_v = geo.classify_against_core(
                        ring, _D(cur["latitude"]), _D(cur["longitude"]), _D(cur["accuracy_meters"])
                    )
                    if core_v.verdict == "inside":
                        reasons.append((R_INSIDE_CORE, {"core_zone_id": cid, **core_v.as_evidence()}))
                    elif core_v.verdict == "buffer_crosses":
                        reasons.append((R_BUFFER_CORE, {"core_zone_id": cid, **core_v.as_evidence()}))
                    ev.setdefault("core_zones", []).append(
                        {"core_zone_id": cid, **core_v.as_evidence()}
                    )

            base[key] = reasons
            evidence[key] = ev

        # 申诉复核人明确豁免的原因不再阻断（含限额类豁免）
        for key in keys:
            waived = set(state.waived.get(key, ()))
            if waived:
                base[key] = [(c, d) for c, d in base[key] if c not in waived]
                evidence[key]["waived_reasons"] = sorted(waived)
        eligible = [
            k
            for k in keys
            if state.permits.get(state.events[k]["current"]["permit_id"]) is not None
            and not base[k]
        ]

        out: dict[str, tuple[str, list[tuple[str, dict]], dict]] = {}
        for key in keys:
            reasons = list(base[key])
            if state.permits.get(state.events[key]["current"]["permit_id"]) is None:
                out[key] = ("quarantined", reasons, evidence[key])
            elif reasons:
                out[key] = ("quarantined", reasons, evidence[key])
            else:
                out[key] = ("pending", [], evidence[key])

        # 限额分配：按采集时间归位（同刻按设备序号定序），四个维度并行累计
        def year_of(k: str) -> str:
            return state.events[k]["current"]["captured_at"][:4]

        buckets: dict[Any, Decimal] = {}

        def cap(bucket: Any, limit: Decimal | None) -> Decimal | None:
            if limit is None:
                return None
            return limit - buckets.get(bucket, Decimal(0))

        for key in eligible:
            cur = state.events[key]["current"]
            permit = state.permits[cur["permit_id"]]
            year = year_of(key)
            species = permit["species_code"]
            parcel_id = permit["parcel_id"]
            survey_rec = state.surveys[permit["survey_version"]][species]
            layer = state.parcel_layers[permit["parcel_version"]]
            mass = _D(cur["mass_kg"])
            limits = {
                R_PERMIT_QUOTA: (
                    ("permit", permit["permit_id"]),
                    permit["quota_kg"],
                ),
                R_TEAM_QUOTA: (
                    ("team-year", permit["team_id"], year),
                    state.teams[permit["team_id"]]["annual_quota_kg"],
                ),
                R_PARCEL_SPECIES_QUOTA: (
                    ("parcel-species-year", parcel_id, species, year),
                    _D(layer["parcels"][parcel_id]["species_quota_kg"][species])
                    if species in layer["parcels"][parcel_id]["species_quota_kg"]
                    else None,
                ),
                R_SPECIES_QUOTA: (
                    ("species-year", species, year),
                    _D(survey_rec["annual_quota_kg"]),
                ),
            }
            hit: list[tuple[str, dict]] = []
            for code, (bucket, limit) in limits.items():
                remaining = cap(bucket, limit)
                if remaining is not None and mass > remaining:
                    hit.append(
                        (
                            code,
                            {
                                "limit_kg": str(limit),
                                "already_counted_kg": str(buckets.get(bucket, Decimal(0))),
                                "remaining_kg": str(remaining),
                                "attempted_kg": str(mass),
                                "allocated_by": "captured_at ordering",
                            },
                        )
                    )
            if hit:
                out[key] = ("quarantined", hit, evidence[key])
            else:
                for code, (bucket, limit) in limits.items():
                    if limit is not None:
                        buckets[bucket] = buckets.get(bucket, Decimal(0)) + mass
                out[key] = ("counted", [], evidence[key])
        return out

    # ----- 投影 -----------------------------------------------------------
    def snapshot(self) -> "Projection":
        return Projection.from_entries(self._entries)

    def replay(self, *, verify: bool = True) -> "Projection":
        """重放整条日志，还原当前状态；verify 时独立重算裁定并核对转换链。"""

        state = Projection.from_entries(self._entries)
        if verify:
            expected = self._adjudicate(state)
            for key, (eff, _reasons, _ev) in expected.items():
                recorded = state.effective.get(key)
                if recorded != eff:
                    raise LedgerError(
                        f"重放校验失败：记录 {key} 重算结果 {eff} 与日志状态 {recorded} 不一致"
                    )
        return state

    def patrol_report(self, patrol_id: str, *, verify: bool = True) -> dict[str, Any]:
        state = self.replay(verify=verify)
        return state.patrol_report(patrol_id)


# ---------------------------------------------------------------------------
# 投影：把日志条目折叠成当前状态
# ---------------------------------------------------------------------------
class Projection:
    def __init__(self) -> None:
        self.surveys: dict[str, dict[str, dict]] = {}
        self.parcel_layers: dict[str, dict] = {}
        self.teams: dict[str, dict] = {}
        self.permits: dict[str, dict] = {}
        self.patrols: dict[str, dict] = {}
        self.events: dict[str, dict] = {}
        self.effective: dict[str, str] = {}
        self.reasons: dict[str, list] = {}
        self.waived: dict[str, list[str]] = {}
        self.timeline: list[dict] = []

    @classmethod
    def from_entries(cls, entries: Iterable[Entry]) -> "Projection":
        s = cls()
        for e in entries:
            s.apply(e)
        return s

    # -- fold -------------------------------------------------------------
    def apply(self, e: Entry) -> None:
        p = e.payload
        kind = e.type
        if kind == "survey-published":
            self.surveys[p["version"]] = p["species"]
        elif kind == "parcel-layer-published":
            self.parcel_layers[p["version"]] = p
        elif kind == "team-registered":
            self.teams[p["team_id"]] = {
                "name": p["name"],
                "annual_quota_kg": _D(p["annual_quota_kg"]),
            }
        elif kind == "permit-approved":
            self.permits[p["permit_id"]] = {
                **p,
                "quota_kg": _D(p["quota_kg"]),
                "status": "active",
            }
        elif kind == "permit-suspended":
            self.permits[p["permit_id"]]["status"] = "suspended"
        elif kind == "patrol-opened":
            self.patrols[p["patrol_id"]] = {"ranger": p["ranger"], "note": p.get("note", ""), "status": "open"}
        elif kind == "patrol-finalized":
            self.patrols[p["patrol_id"]]["status"] = "finalized"
        elif kind == "event-ingested":
            key = p["event_key"]
            self.events[key] = {
                "raw": dict(p["raw"]),
                "current": dict(p["raw"]),
                "order": (
                    p["raw"]["captured_at"],
                    p["raw"]["device_id"],
                    p["raw"]["sequence"],
                ),
                "corrections": [],
                "appeal": None,
                "transitions": [],
            }
            self.effective[key] = "pending"
        elif kind == "erratum-appended":
            ev = self.events[p["event_key"]]
            ev["corrections"].append({"at": e.at, "actor": e.actor, **p})
            ev["current"].update(p["corrections"])
        elif kind == "appeal-filed":
            self.events[p["event_key"]]["appeal"] = {
                "status": "open",
                "appellant": p["appellant"],
                "grounds": p["grounds"],
                "filed_at": e.at,
            }
        elif kind == "appeal-decided":
            ev = self.events[p["event_key"]]
            ev["appeal"].update(
                {
                    "status": "upheld" if p["uphold"] else "overturned",
                    "reviewer": p["reviewer"],
                    "rationale": p["rationale"],
                    "waived_reasons": p["waived_reasons"],
                    "decided_at": e.at,
                }
            )
            if not p["uphold"]:
                if p["waived_reasons"]:
                    self.waived[p["event_key"]] = sorted(
                        set(self.waived.get(p["event_key"], [])) | set(p["waived_reasons"])
                    )
                if p["corrections"]:
                    ev["corrections"].append(
                        {
                            "at": e.at,
                            "actor": p["reviewer"],
                            "reason": "appeal-overturned-correction",
                            "original": {k: ev["current"][k] for k in p["corrections"]},
                            "corrections": dict(p["corrections"]),
                        }
                    )
                    ev["current"].update(p["corrections"])
        elif kind == "status-transition":
            self.with_transition(p, at=e.at, actor=e.actor)
        self.timeline.append({"seq": e.seq, "at": e.at, "actor": e.actor, "type": kind, "payload": p})

    def with_transition(self, p: dict, at: str | None = None, actor: str | None = None) -> "Projection":
        """供 _recompute 增量更新投影；重放时由 apply 调用。"""

        key = p["event_key"]
        self.effective[key] = p["to_status"]
        self.reasons[key] = p["reasons"]
        tr = {k: p[k] for k in ("from_status", "to_status", "reasons", "evidence", "trigger")}
        if at:
            tr["at"] = at
            tr["actor"] = actor
        if key in self.events:
            self.events[key]["transitions"].append(tr)
        return self

    # -- helpers ----------------------------------------------------------
    def permit_view(self, permit_id: str) -> PermitView:
        p = self.permits[permit_id]
        counted = sum(
            (
                _D(self.events[k]["current"]["mass_kg"])
                for k in self.events
                if self.effective.get(k) == "counted"
                and self.events[k]["current"]["permit_id"] == permit_id
            ),
            Decimal(0),
        )
        return PermitView(
            permit_id=p["permit_id"],
            team_id=p["team_id"],
            species_code=p["species_code"],
            parcel_id=p["parcel_id"],
            survey_version=p["survey_version"],
            parcel_version=p["parcel_version"],
            valid_from=p["valid_from"],
            valid_until=p["valid_until"],
            quota_kg=p["quota_kg"],
            approver=p["approver"],
            status=p["status"],
            frozen_survey=p["survey_snapshot"],
            frozen_parcel=p["parcel_snapshot"],
            counted_kg=counted,
        )

    def _event_views(self) -> list[EventView]:
        views = []
        for key, ev in self.events.items():
            disposition = "corrected" if ev["corrections"] else self.effective.get(key, "pending")
            reasons = [Violation(r["code"], r["detail"]) for r in self.reasons.get(key, [])]
            views.append(
                EventView(
                    key=key,
                    device_id=ev["raw"]["device_id"],
                    sequence=ev["raw"]["sequence"],
                    permit_id=ev["raw"]["permit_id"],
                    patrol_id=ev["raw"].get("patrol_id"),
                    captured_at=datetime.fromisoformat(ev["current"]["captured_at"]),
                    received_at=datetime.fromisoformat(ev["raw"]["received_at"]),
                    raw=ev["raw"],
                    current=ev["current"],
                    disposition=disposition,
                    effective=self.effective.get(key, "pending"),
                    reasons=reasons,
                    waived_reasons=self.waived.get(key, []),
                    corrections=ev["corrections"],
                    appeal=ev["appeal"],
                    transitions=ev["transitions"],
                )
            )
        return sorted(views, key=lambda v: (v.captured_at, v.device_id, v.sequence))

    # -- 巡护还原 ----------------------------------------------------------
    def patrol_report(self, patrol_id: str) -> dict[str, Any]:
        if patrol_id not in self.patrols:
            raise LedgerError(f"巡护 {patrol_id} 不存在")
        events = [v for v in self._event_views() if v.patrol_id == patrol_id]
        permit_ids = sorted({v.permit_id for v in events})

        # 年度额度余额（按记录采集年份聚合）
        team_buckets: dict[tuple[str, str], Decimal] = {}
        parcel_buckets: dict[tuple[str, str, str], Decimal] = {}
        species_buckets: dict[tuple[str, str], Decimal] = {}
        for v in self._event_views():
            if v.effective != "counted":
                continue
            permit = self.permits[v.permit_id]
            year = str(v.captured_at.year)
            mass = v.mass_kg
            team_buckets[(permit["team_id"], year)] = (
                team_buckets.get((permit["team_id"], year), Decimal(0)) + mass
            )
            parcel_buckets[(permit["parcel_id"], permit["species_code"], year)] = (
                parcel_buckets.get((permit["parcel_id"], permit["species_code"], year), Decimal(0))
                + mass
            )
            species_buckets[(permit["species_code"], year)] = (
                species_buckets.get((permit["species_code"], year), Decimal(0)) + mass
            )

        permits_out = []
        for pid in permit_ids:
            pv = self.permit_view(pid)
            year_set = sorted({str(v.captured_at.year) for v in events if v.permit_id == pid})
            team_year = []
            for y in year_set:
                limit = self.teams[pv.team_id]["annual_quota_kg"]
                used = team_buckets.get((pv.team_id, y), Decimal(0))
                team_year.append(
                    {"year": y, "limit_kg": str(limit), "used_kg": str(used), "remaining_kg": str(limit - used)}
                )
            permits_out.append(
                {
                    "permit_id": pv.permit_id,
                    "team_id": pv.team_id,
                    "species_code": pv.species_code,
                    "parcel_id": pv.parcel_id,
                    "survey_version": pv.survey_version,
                    "parcel_version": pv.parcel_version,
                    "valid_from": pv.valid_from,
                    "valid_until": pv.valid_until,
                    "status": pv.status,
                    "quota_kg": str(pv.quota_kg),
                    "counted_kg": str(pv.counted_kg),
                    "remaining_kg": str(pv.remaining_kg),
                    "species": pv.frozen_survey,
                    "team_annual": team_year,
                }
            )

        events_out = []
        for v in events:
            events_out.append(
                {
                    "event_key": v.key,
                    "device_id": v.device_id,
                    "sequence": v.sequence,
                    "captured_at": v.captured_at.isoformat(),
                    "received_at": v.received_at.isoformat(),
                    "offline": v.raw.get("offline", True),
                    "raw_point": {
                        "latitude": v.raw["latitude"],
                        "longitude": v.raw["longitude"],
                        "accuracy_meters": v.raw["accuracy_meters"],
                    },
                    "judged_point": {
                        "latitude": v.current["latitude"],
                        "longitude": v.current["longitude"],
                        "accuracy_meters": v.current["accuracy_meters"],
                    },
                    "mass_kg": str(v.mass_kg),
                    "disposition": v.disposition,
                    "effective_status": v.effective,
                    "violations": [{"code": r.code, "detail": r.detail} for r in v.reasons],
                    "waived_reasons": v.waived_reasons,
                    "corrections": v.corrections,
                    "appeal": v.appeal,
                    "status_changes": v.transitions,
                }
            )

        dup_count = sum(
            1 for t in self.timeline if t["type"] == "duplicate-suppressed" and t["payload"].get("patrol_id") == patrol_id
        )
        return {
            "patrol_id": patrol_id,
            "status": self.patrols[patrol_id]["status"],
            "ranger": self.patrols[patrol_id]["ranger"],
            "permits": permits_out,
            "events": events_out,
            "duplicate_events_suppressed": dup_count,
            "legal_harvest_kg": str(sum((v.mass_kg for v in events if v.effective == "counted"), Decimal(0))),
            "quarantined_kg": str(sum((v.mass_kg for v in events if v.effective == "quarantined"), Decimal(0))),
        }
