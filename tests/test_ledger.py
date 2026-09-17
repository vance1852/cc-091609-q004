"""台账业务规则测试。"""

import json
import tempfile
import unittest
from datetime import datetime
from decimal import Decimal
from pathlib import Path

from wildharvest import Ledger, LedgerError
from wildharvest.ledger import DuplicateSequence, Entry


def make_ledger() -> Ledger:
    """构造含调查版本、边界层、团队和一张 10kg 许可的标准台账。"""

    ledger = Ledger()
    ledger.publish_survey(
        "sv-1",
        {"QH": {"name": "羌活", "protection_level": "二级", "annual_quota_kg": "100"}},
        "bureau",
    )
    ledger.publish_parcel_layer(
        "pl-1",
        {
            "p1": {
                "ring": [
                    [31.1000, 103.2000],
                    [31.1036, 103.2000],
                    [31.1036, 103.2042],
                    [31.1000, 103.2042],
                ],
                "species_quota_kg": {"QH": "60"},
            }
        },
        core_zones={
            "core-1": [
                [31.1028, 103.2020],
                [31.1040, 103.2020],
                [31.1040, 103.2034],
                [31.1028, 103.2034],
            ]
        },
    )
    ledger.register_team("t1", "一队", "50", "bureau")
    ledger.approve_permit(
        "pm1", "t1", "QH", "sv-1", "pl-1", "p1",
        "2026-08-01", "2026-10-31", "10", "approver-1",
    )
    return ledger


def ingest(ledger: Ledger, seq: int, **over) -> object:
    args = dict(
        device_id="d1",
        sequence=seq,
        permit_id="pm1",
        captured_at=datetime.fromisoformat(f"2026-09-{10 + (seq % 5):02d}T08:00:00+08:00"),
        received_at=datetime.fromisoformat("2026-09-20T10:00:00+08:00"),
        latitude=Decimal("31.1010"),
        longitude=Decimal("103.2010"),
        accuracy_meters=Decimal("10"),
        mass_kg=Decimal("4"),
        actor="sync",
        patrol_id=None,
    )
    args.update(over)
    return ledger.ingest_event(**args)


class TestFreezeAndVersioning(unittest.TestCase):
    def test_versions_immutable_and_required(self) -> None:
        l = make_ledger()
        with self.assertRaises(LedgerError):
            l.publish_survey("sv-1", {}, "bureau")
        with self.assertRaises(LedgerError):
            l.publish_parcel_layer("pl-1", {})
        with self.assertRaises(LedgerError):
            l.approve_permit("pmX", "t1", "QH", "missing", "pl-1", "p1",
                             "2026-08-01", "2026-10-31", "1", "a")
        with self.assertRaises(LedgerError):
            l.approve_permit("pmX", "t1", "QH", "sv-1", "missing", "p1",
                             "2026-08-01", "2026-10-31", "1", "a")

    def test_permit_freezes_snapshot(self) -> None:
        l = make_ledger()
        # 审批后再发新版调查（限额放宽/收紧）不影响已批许可
        l.publish_survey(
            "sv-2",
            {"QH": {"name": "羌活", "protection_level": "二级", "annual_quota_kg": "1"}},
            "bureau",
        )
        ingest(l, 1)
        state = l.replay()
        frozen = state.permits["pm1"]["survey_snapshot"]
        self.assertEqual(frozen["annual_quota_kg"], "100")
        self.assertEqual(state.effective["d1#1"], "counted")

    def test_invalid_dates_and_quota(self) -> None:
        l = make_ledger()
        with self.assertRaises(LedgerError):
            l.approve_permit("pm2", "t1", "QH", "sv-1", "pl-1", "p1",
                             "2026-11-01", "2026-08-01", "10", "a")
        with self.assertRaises(LedgerError):
            l.approve_permit("pm2", "t1", "QH", "sv-1", "pl-1", "p1",
                             "2026-08-01", "2026-10-31", "0", "a")


class TestOfflineDedupAndOrdering(unittest.TestCase):
    def test_identical_retry_suppressed_counted_once(self) -> None:
        l = make_ledger()
        ingest(l, 1)
        before = len(l.entries)
        ingest(l, 1)  # 断网重传，内容一致
        after = len(l.entries)
        self.assertEqual(after - before, 1)  # 仅追加一条 duplicate-suppressed
        self.assertEqual(l.entries[-1].type, "duplicate-suppressed")
        state = l.replay()
        self.assertEqual(len(state.events), 1)
        self.assertEqual(state.effective["d1#1"], "counted")

    def test_arrival_order_independent(self) -> None:
        # 许可限额 10kg：两次 6kg，只有先采集的一笔合法
        l1, l2 = make_ledger(), make_ledger()
        evs = [
            dict(seq=1, captured_at="2026-09-11T08:00:00+08:00", mass_kg="6"),
            dict(seq=2, captured_at="2026-09-12T08:00:00+08:00", mass_kg="6"),
        ]
        for e in evs:
            ingest(l1, seq=e["seq"], captured_at=datetime.fromisoformat(e["captured_at"]),
                   mass_kg=Decimal(e["mass_kg"]))
        # 乱序到达
        for e in reversed(evs):
            ingest(l2, seq=e["seq"], captured_at=datetime.fromisoformat(e["captured_at"]),
                   mass_kg=Decimal(e["mass_kg"]))
        for lx in (l1, l2):
            s = lx.replay()
            self.assertEqual(s.effective["d1#1"], "counted")
            self.assertEqual(s.effective["d1#2"], "quarantined")
            self.assertEqual(s.reasons["d1#2"][0]["code"], "permit_quota_exceeded")

    def test_sequence_collision_different_payload_rejected(self) -> None:
        l = make_ledger()
        ingest(l, 1, mass_kg=Decimal("4"))
        with self.assertRaises(DuplicateSequence):
            ingest(l, 1, mass_kg=Decimal("5"))


class TestSpatialAndQuotaQuarantine(unittest.TestCase):
    def test_outside_parcel_quarantined(self) -> None:
        l = make_ledger()
        ingest(l, 1, latitude=Decimal("31.0990"), longitude=Decimal("103.2010"),
               accuracy_meters=Decimal("10"))
        self.assertEqual(l.replay().effective["d1#1"], "quarantined")
        self.assertEqual(l.replay().reasons["d1#1"][0]["code"], "outside_parcel")

    def test_inside_core_zone_quarantined(self) -> None:
        l = make_ledger()
        ingest(l, 1, latitude=Decimal("31.1032"), longitude=Decimal("103.2027"),
               accuracy_meters=Decimal("8"))
        self.assertEqual(l.replay().effective["d1#1"], "quarantined")
        codes = [r["code"] for r in l.replay().reasons["d1#1"]]
        self.assertIn("inside_core_zone", codes)

    def test_buffer_crosses_boundary_quarantined_with_evidence(self) -> None:
        l = make_ledger()
        ingest(l, 1, latitude=Decimal("31.1002"), longitude=Decimal("103.2010"),
               accuracy_meters=Decimal("36"))
        s = l.replay()
        self.assertEqual(s.effective["d1#1"], "quarantined")
        codes = [r["code"] for r in s.reasons["d1#1"]]
        self.assertIn("accuracy_buffer_crosses_parcel", codes)
        # 证据保留原点、精度与边距
        last = [e for e in l.entries if e.type == "status-transition"][-1]
        ev = last.payload["evidence"]
        self.assertEqual(ev["original_point"]["accuracy_meters"], "36")
        self.assertIn("edge_distance_m", ev["parcel"])
        self.assertIn("projection", ev["parcel"])

    def test_permit_team_parcel_species_quotas(self) -> None:
        l = Ledger()
        l.publish_survey(
            "sv", {"QH": {"name": "羌活", "protection_level": "x", "annual_quota_kg": "12"}}, "bureau"
        )
        l.publish_parcel_layer(
            "pl",
            {"p": {"ring": [[31.1000, 103.2000], [31.1036, 103.2000], [31.1036, 103.2042], [31.1000, 103.2042]],
                    "species_quota_kg": {"QH": "9"}}},
        )
        l.register_team("t", "队", "10", "bureau")
        l.approve_permit("p1", "t", "QH", "sv", "pl", "p", "2026-01-01", "2026-12-31", "100", "a")
        l.approve_permit("p2", "t", "QH", "sv", "pl", "p", "2026-01-01", "2026-12-31", "100", "a")
        # p1 用 6（团队余 5、地块余 3、物种余 6），p2 用 5 → 超团队(11)与地块物种(9)
        ingest(l, 1, permit_id="p1", mass_kg=Decimal("6"),
               captured_at=datetime.fromisoformat("2026-09-01T08:00:00+08:00"))
        ingest(l, 2, permit_id="p2", mass_kg=Decimal("5"),
               captured_at=datetime.fromisoformat("2026-09-02T08:00:00+08:00"))
        s = l.replay()
        self.assertEqual(s.effective["d1#1"], "counted")
        self.assertEqual(s.effective["d1#2"], "quarantined")
        codes = {r["code"] for r in s.reasons["d1#2"]}
        self.assertIn("team_annual_quota_exceeded", codes)
        self.assertIn("parcel_species_annual_quota_exceeded", codes)
        # 隔离量不占额度：再来一笔 3kg（团队余 5、地块余 3、物种余 6）应合法
        ingest(l, 3, permit_id="p2", mass_kg=Decimal("3"),
               captured_at=datetime.fromisoformat("2026-09-03T08:00:00+08:00"))
        self.assertEqual(l.replay().effective["d1#3"], "counted")

    def test_validity_window(self) -> None:
        l = make_ledger()
        ingest(l, 1, captured_at=datetime.fromisoformat("2026-11-05T08:00:00+08:00"))
        self.assertEqual(l.replay().reasons["d1#1"][0]["code"], "outside_validity")


class TestSuspendErrataAppeals(unittest.TestCase):
    def test_suspend_unused_and_used_permit(self) -> None:
        l = make_ledger()
        l.approve_permit("pm2", "t1", "QH", "sv-1", "pl-1", "p1",
                         "2026-08-01", "2026-10-31", "5", "approver-1")
        ingest(l, 1)
        with self.assertRaises(LedgerError):
            l.suspend_permit("pm1", "officer", "已用")
        l.suspend_permit("pm2", "officer", "未用，暂停")
        self.assertEqual(l.replay().permits["pm2"]["status"], "suspended")
        # 暂停后新采集被隔离
        ingest(l, 2, permit_id="pm2")
        self.assertEqual(l.replay().effective["d1#2"], "quarantined")
        self.assertEqual(l.replay().reasons["d1#2"][0]["code"], "permit_suspended")

    def test_erratum_append_only_keeps_original(self) -> None:
        l = make_ledger()
        ingest(l, 1, mass_kg=Decimal("9"))
        l.append_erratum("d1#1", {"mass_kg": "3"}, "ranger", "复称")
        s = l.replay()
        self.assertEqual(s.events["d1#1"]["raw"]["mass_kg"], "9")
        self.assertEqual(s.events["d1#1"]["current"]["mass_kg"], "3")
        # 9kg 原本占 9/10，勘误为 3kg 后余额释放
        self.assertEqual(s.effective["d1#1"], "counted")
        ingest(l, 2, mass_kg=Decimal("8"),
               captured_at=datetime.fromisoformat("2026-09-12T08:00:00+08:00"))
        # 3+8=11 仍超限
        self.assertEqual(l.replay().effective["d1#2"], "quarantined")
        ingest(l, 3, mass_kg=Decimal("7"),
               captured_at=datetime.fromisoformat("2026-09-13T08:00:00+08:00"))
        self.assertEqual(l.replay().effective["d1#3"], "counted")

    def test_appeal_requires_second_reviewer(self) -> None:
        l = make_ledger()
        ingest(l, 1, latitude=Decimal("31.0990"), accuracy_meters=Decimal("5"))
        l.file_appeal("d1#1", "captain", "定位漂移")
        with self.assertRaises(LedgerError):
            l.decide_appeal("d1#1", "captain", True, "自己")
        with self.assertRaises(LedgerError):
            l.decide_appeal("d1#1", "approver-1", False, "审批人")
        l.decide_appeal("d1#1", "reviewer-2", False, "复测在界内",
                        corrections={"latitude": "31.1010", "longitude": "103.2010",
                                     "accuracy_meters": "8"})
        s = l.replay()
        self.assertEqual(s.effective["d1#1"], "counted")
        # 勘误原值保留
        self.assertEqual(s.events["d1#1"]["raw"]["latitude"], "31.0990")

    def test_appeal_uphold_keeps_quarantine(self) -> None:
        l = make_ledger()
        ingest(l, 1, latitude=Decimal("31.0990"), accuracy_meters=Decimal("5"))
        l.file_appeal("d1#1", "captain", "有异议")
        l.decide_appeal("d1#1", "reviewer-2", True, "越界属实")
        self.assertEqual(l.replay().effective["d1#1"], "quarantined")

    def test_overturn_must_resolve_all_reasons(self) -> None:
        l = make_ledger()
        ingest(l, 1, latitude=Decimal("31.0990"), accuracy_meters=Decimal("5"))
        l.file_appeal("d1#1", "captain", "有异议")
        # 不附勘误/豁免直接撤销应被拒绝，且不留日志痕迹
        before = len(l.entries)
        with self.assertRaises(LedgerError):
            l.decide_appeal("d1#1", "reviewer-2", False, "就是想放行")
        self.assertEqual(len(l.entries), before)

    def test_waive_reason_via_appeal(self) -> None:
        l = make_ledger()
        ingest(l, 1, latitude=Decimal("31.1002"), accuracy_meters=Decimal("36"))
        self.assertEqual(l.replay().effective["d1#1"], "quarantined")
        l.file_appeal("d1#1", "captain", "大地线复核")
        l.decide_appeal("d1#1", "reviewer-2", False, "经大地线复核缓冲圆不跨界",
                        waive_reasons=["accuracy_buffer_crosses_parcel"])
        self.assertEqual(l.replay().effective["d1#1"], "counted")


class TestPersistenceAndReplay(unittest.TestCase):
    def test_save_load_replay_verification(self) -> None:
        l = make_ledger()
        ingest(l, 1)
        with tempfile.TemporaryDirectory() as d:
            path = Path(d) / "ledger.jsonl"
            l.save(path)
            reloaded = Ledger.load(path)
            s = reloaded.replay(verify=True)
            self.assertEqual(s.effective["d1#1"], "counted")
            # 篡改任一日志行 → 哈希链在加载时即失效
            lines = path.read_text(encoding="utf-8").splitlines()
            lines[0] = lines[0].replace('"bureau"', '"attacker"')
            path.write_text("\n".join(lines), encoding="utf-8")
            with self.assertRaises(LedgerError):
                Ledger.load(path)

    def test_replay_detects_semantic_drift(self) -> None:
        # 日志完整但状态转换与重算不符（例如手工插入伪造的 status-transition）
        l = make_ledger()
        ingest(l, 1)
        forged = Entry(
            seq=len(l.entries) + 1,
            at="2026-09-20T12:00:00+08:00",
            actor="forger",
            type="status-transition",
            payload={
                "event_key": "d1#1",
                "trigger": {},
                "from_status": "counted",
                "to_status": "quarantined",
                "reasons": [],
                "evidence": {},
            },
        )
        l._entries.append(forged)
        with self.assertRaises(LedgerError):
            l.replay(verify=True)

    def test_patrol_report(self) -> None:
        l = make_ledger()
        l.open_patrol("pt", "ranger")
        ingest(l, 1, patrol_id="pt")
        report = l.patrol_report("pt")
        self.assertEqual(report["legal_harvest_kg"], "4")
        self.assertEqual(report["quarantined_kg"], "0")
        self.assertEqual(report["permits"][0]["remaining_kg"], "6")
        # JSON 可序列化
        json.dumps(report, ensure_ascii=False)


if __name__ == "__main__":
    unittest.main()
