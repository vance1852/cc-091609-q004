"""把 fixtures 中的注册表种子与断网巡护导出装配成监管台账。

用法：
    python -m wildharvest.seedrun            # 装配并输出巡护报告
    python -m wildharvest.seedrun --save build/ledger.jsonl

事件刻意按设备 ``received_at``（恢复联网后的到达顺序）录入，由台账按
``captured_at`` 归位核算，以证明到达顺序不影响限额裁定。
"""

from __future__ import annotations

import argparse
import json
from pathlib import Path

from .ledger import Ledger

ROOT = Path(__file__).resolve().parent.parent
SEED = ROOT / "fixtures" / "registry_seed.json"
PATROL_EXPORT = ROOT / "fixtures" / "harvest_patrol.json"


def build_ledger(seed_path: Path = SEED, patrol_path: Path = PATROL_EXPORT) -> Ledger:
    seed = json.loads(seed_path.read_text(encoding="utf-8"))
    patrol_export = json.loads(patrol_path.read_text(encoding="utf-8"))
    ledger = Ledger()

    # 1) 冻结发布资源调查版本
    for survey in seed["surveys"]:
        ledger.publish_survey(
            survey["version"],
            survey["species"],
            actor="bureau",
            effective_from=survey.get("effective_from"),
        )

    # 2) 冻结发布地块与核心区边界版本
    for layer in seed["parcel_layers"]:
        ledger.publish_parcel_layer(
            layer["version"],
            layer["parcels"],
            core_zones=layer.get("core_zones"),
            actor="bureau",
            effective_from=layer.get("effective_from"),
        )

    # 3) 登记采集团队年度限额
    for team in seed["teams"]:
        ledger.register_team(team["team_id"], team["name"], team["annual_quota_kg"], "bureau")

    # 4) 纸质许可审批：冻结当时调查版本与地理范围快照
    for permit in seed["permits"]:
        ledger.approve_permit(
            permit["permit_id"],
            permit["team_id"],
            permit["species_code"],
            permit["survey_version"],
            permit["parcel_version"],
            permit["parcel_id"],
            permit["valid_from"],
            permit["valid_until"],
            permit["quota_kg"],
            permit["approver"],
        )

    # 脱敏巡护导出里的许可/调查版本必须与登记一致
    exported_permits = {p["id"]: p for p in patrol_export["permits"]}
    for pid, ep in exported_permits.items():
        seed_permit = next(p for p in seed["permits"] if p["permit_id"] == pid)
        assert str(seed_permit["quota_kg"]) == ep["quotaKg"]
        assert seed_permit["survey_version"] == patrol_export["surveyVersion"]

    # 5) 开启巡护
    patrol_id = seed["patrol_id"]
    ledger.open_patrol(patrol_id, ranger="ranger-chen", note="雨后恢复联网，补录断网期间事件")

    # 6) 按接收顺序（received_at）恢复离线事件，序号由设备离线签发
    events_in_arrival_order = sorted(seed["events"], key=lambda e: e["received_at"])
    for ev in events_in_arrival_order:
        ledger.ingest_event(
            device_id=ev["device_id"],
            sequence=ev["sequence"],
            permit_id=ev["permit_id"],
            captured_at=ev["captured_at"],
            received_at=ev["received_at"],
            latitude=ev["latitude"],
            longitude=ev["longitude"],
            accuracy_meters=ev["accuracy_meters"],
            mass_kg=ev["mass_kg"],
            actor="field-sync",
            patrol_id=patrol_id,
            offline=True,
        )

    # 7) 设备断网重传导出：fixtures/harvest_patrol.json 里同一 (设备,序号) 出现两次。
    #    导出只含设备侧序号与精度；按序号找到原记录后重发完整报文，应被幂等去重。
    for row in patrol_export["events"]:
        key = f"{row['device']}#{row['sequence']}"
        original = next(
            (
                e
                for e in seed["events"]
                if e["device_id"] == row["device"] and e["sequence"] == row["sequence"]
            ),
            None,
        )
        if original is None:
            raise ValueError(f"巡护导出中的 {key} 在台账中找不到原始记录")
        assert original["accuracy_meters"] == row["accuracyMeters"]
        ledger.ingest_event(
            device_id=original["device_id"],
            sequence=original["sequence"],
            permit_id=original["permit_id"],
            captured_at=original["captured_at"],
            received_at=original["received_at"],
            latitude=original["latitude"],
            longitude=original["longitude"],
            accuracy_meters=original["accuracy_meters"],
            mass_kg=original["mass_kg"],
            actor="field-retry",
            patrol_id=patrol_id,
            offline=row.get("offline", True),
        )

    # 8) 现场勘误（只追加，原值保留）：seq15 复称纠正
    for ev in seed["events"]:
        if "erratum" in ev:
            erratum = ev["erratum"]
            ledger.append_erratum(
                f"{ev['device_id']}#{ev['sequence']}",
                corrections={k: v for k, v in erratum.items() if k in ("mass_kg", "latitude", "longitude", "accuracy_meters")},
                actor=erratum.get("actor", "ranger-chen"),
                reason=erratum["reason"],
            )

    # 9) 管理员暂停一张尚未使用的许可（permit-c 无任何记录）
    ledger.suspend_permit("permit-c", actor="officer-wang", reason="巡护发现重叠申报，待核查后再启用")
    try:
        ledger.suspend_permit("permit-a", actor="officer-wang", reason="应被拒绝")
    except Exception as exc:  # 已有记录的许可不能暂停
        print(f"[规则] 暂停已使用许可被拒绝: {exc}")

    # 10) 申诉：必须由另一名复核人裁定
    for ev in seed["events"]:
        appeal = ev.get("appeal")
        if not appeal:
            continue
        key = f"{ev['device_id']}#{ev['sequence']}"
        ledger.file_appeal(key, appellant=appeal["appellant"], grounds=appeal["grounds"])
        try:
            ledger.decide_appeal(
                key,
                reviewer=appeal["appellant"],  # 自己复核自己，应被拒绝
                uphold=appeal["uphold"],
                rationale=appeal["rationale"],
                corrections=appeal.get("corrections"),
            )
            raise AssertionError("职责分离校验失效")
        except ValueError as exc:
            print(f"[规则] 自己裁定申诉被拒绝: {exc}")
        ledger.decide_appeal(
            key,
            reviewer=appeal["reviewer"],
            uphold=appeal["uphold"],
            rationale=appeal["rationale"],
            corrections=appeal.get("corrections"),
            waive_reasons=appeal.get("waive_reasons", []),
        )

    ledger.finalize_patrol(patrol_id, actor="ranger-chen")
    return ledger


def main() -> None:
    parser = argparse.ArgumentParser(description="装配可持续采集监管台账并输出巡护报告")
    parser.add_argument("--save", type=Path, default=ROOT / "build" / "ledger.jsonl")
    parser.add_argument("--report", type=Path, default=ROOT / "build" / "patrol_report.json")
    args = parser.parse_args()

    ledger = build_ledger()
    args.save.parent.mkdir(parents=True, exist_ok=True)
    ledger.save(args.save)

    # 从落盘日志全新重放（含篡改校验），再还原巡护
    reloaded = Ledger.load(args.save)
    report = reloaded.patrol_report("patrol-2026-09-13")
    args.report.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"台账日志已写入 {args.save}（{len(ledger.entries)} 条）")
    print(f"巡护报告已写入 {args.report}")
    print(f"合法收获 {report['legal_harvest_kg']} kg，隔离 {report['quarantined_kg']} kg，"
          f"去重抑制 {report['duplicate_events_suppressed']} 条")


if __name__ == "__main__":
    main()
