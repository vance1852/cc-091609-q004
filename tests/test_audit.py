"""审计哈希链与巡护还原测试。"""

import unittest
from datetime import datetime
from decimal import Decimal as D

from domain.audit import GENESIS_HASH, Journal

from .helpers import build_world, make_event


class JournalTests(unittest.TestCase):
    def test_chain_links_and_verifies(self):
        journal = Journal()
        e1 = journal.record(action="a", actor="u", ref="x")
        e2 = journal.record(action="b", actor="u", ref="y")
        self.assertEqual(e1.prev_hash, GENESIS_HASH)
        self.assertEqual(e2.prev_hash, e1.entry_hash)
        self.assertTrue(journal.verify())

    def test_tampering_breaks_chain(self):
        journal = Journal()
        journal.record(action="a", actor="u", ref="x", payload={"mass": "1"})
        good = journal.verify()
        # 直接篡改条目负载（模拟事后改台账）。
        journal.entries[0].payload["mass"] = "999"
        self.assertTrue(good)
        self.assertFalse(journal.verify())

    def test_patrol_filter(self):
        journal = Journal()
        journal.record(action="a", actor="u", ref="x", patrol_id="p1")
        journal.record(action="b", actor="u", ref="y", patrol_id="p2")
        journal.record(action="c", actor="u", ref="z", patrol_id="p1")
        self.assertEqual([e.ref for e in journal.for_patrol("p1")], ["x", "z"])


class ReconstructionTests(unittest.TestCase):
    def test_patrol_report_is_self_contained(self):
        _, ledger, journal = build_world()
        out = ledger.ingest_patrol(
            "patrol-1", "ranger-wang",
            [
                make_event(1, mass="10"),
                make_event(2, mass="8", lat="31.2200", lon="103.5050",
                           accuracy="36"),
            ],
        )
        ledger.append_correction("field-3", 1, "ranger-wang", D("9"), "复称",
                                 at=datetime(2026, 9, 13, 10, 0))
        report = ledger.reconstruct_patrol("patrol-1")

        self.assertTrue(report["journal_intact"])
        self.assertEqual(report["patrol_id"], "patrol-1")
        # 许可余额
        self.assertEqual(report["permit_balances"]["permit-a"]["used_kg"], "9")
        self.assertEqual(report["permit_balances"]["permit-a"]["remaining_kg"], "36")
        # 事件视图含越界理由与判定依据
        views = {e["event_ref"]: e for e in report["events"]}
        self.assertEqual(views["field-3#2"]["quarantine_reasons"], ["outside_parcel"])
        self.assertEqual(
            views["field-3#2"]["boundary"]["origin"]["latitude"], "31.2200"
        )
        self.assertEqual(
            views["field-3#2"]["boundary"]["accuracy_meters"], "36"
        )
        # 勘误留痕
        self.assertEqual(views["field-3#1"]["corrections"][0]["new_mass_kg"], "9")
        # 状态变化时间线覆盖计数、隔离与勘误
        actions = [t["action"] for t in report["audit_timeline"]]
        self.assertIn("event_counted", actions)
        self.assertIn("event_quarantined", actions)
        self.assertIn("event_corrected", actions)


if __name__ == "__main__":
    unittest.main()
