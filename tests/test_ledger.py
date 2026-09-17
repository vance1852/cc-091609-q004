"""台账规则测试：去重归位、四本限额账、暂停、勘误、申诉、版本冻结。"""

import unittest
from datetime import date, datetime
from decimal import Decimal as D

from domain.contracts import (
    AppealStatus,
    EventDisposition,
    HarvestPermit,
    ProtectionLevel,
    QuarantineReason,
)
from domain.ledger import LedgerError

from .helpers import build_world, make_event


def ref(records, seq):
    return next(r for r in records if r.event.sequence == seq and not r.is_duplicate)


class IngestTests(unittest.TestCase):
    def test_dedup_by_device_sequence_keeps_first(self):
        _, ledger, _ = build_world()
        out = ledger.ingest_patrol(
            "p1", "ranger-wang",
            [make_event(18, received="2026-09-10T11:00:00"),
             make_event(18, received="2026-09-12T08:10:00")],
        )
        self.assertEqual(len(out), 2)
        kept, dup = (out[0], out[1]) if not out[0].is_duplicate else (out[1], out[0])
        self.assertFalse(kept.is_duplicate)
        self.assertTrue(dup.is_duplicate)
        self.assertEqual(dup.duplicate_of, "field-3#18")
        # 首条原始接收时间未被重传覆盖。
        self.assertEqual(kept.event.received_at, datetime(2026, 9, 10, 11, 0))

    def test_dedup_across_batches(self):
        _, ledger, _ = build_world()
        ledger.ingest_patrol("p1", "w", [make_event(1)])
        out = ledger.ingest_patrol("p2", "w", [make_event(1)])
        self.assertTrue(out[0].is_duplicate)

    def test_events_ordered_by_captured_time_for_quota(self):
        # 45kg 许可：先到 30kg 计入，同批中采集时间更早但收到更晚的 20kg
        # 按 captured_at 归位后应先计 20kg，再判 30kg 时超限隔离。
        _, ledger, _ = build_world(quota="45")
        events = [
            make_event(2, mass="30", captured="2026-09-11T10:00:00",
                       received="2026-09-11T12:00:00"),
            make_event(1, mass="20", captured="2026-09-10T08:00:00",
                       received="2026-09-12T08:00:00"),
        ]
        out = ledger.ingest_patrol("p1", "w", events)
        order = [r.event.sequence for r in out]
        self.assertEqual(order, [1, 2])
        self.assertEqual(ref(out, 1).event.disposition, EventDisposition.COUNTED)
        self.assertEqual(ref(out, 2).event.disposition, EventDisposition.QUARANTINED)
        self.assertIn(QuarantineReason.PERMIT_QUOTA_EXCEEDED, ref(out, 2).reasons)


class QuotaTests(unittest.TestCase):
    def test_team_parcel_species_ledgers_breach_independently(self):
        _, ledger, _ = build_world(quota="100", team_cap="15",
                                   parcel_cap=None, species_cap=None)
        out = ledger.ingest_patrol("p1", "w", [make_event(1, mass="16")])
        self.assertEqual(ref(out, 1).reasons, (QuarantineReason.TEAM_QUOTA_EXCEEDED,))

    def test_all_four_ledgers_breach(self):
        _, ledger, _ = build_world(quota="5", team_cap="5", parcel_cap="5",
                                   species_cap="5")
        out = ledger.ingest_patrol("p1", "w", [make_event(1, mass="6")])
        self.assertEqual(
            set(ref(out, 1).reasons),
            {
                QuarantineReason.PERMIT_QUOTA_EXCEEDED,
                QuarantineReason.TEAM_QUOTA_EXCEEDED,
                QuarantineReason.PARCEL_QUOTA_EXCEEDED,
                QuarantineReason.SPECIES_QUOTA_EXCEEDED,
            },
        )

    def test_quarantined_mass_not_counted(self):
        _, ledger, _ = build_world(quota="20")
        ledger.ingest_patrol("p1", "w", [make_event(1, mass="25")])
        balance = ledger.permit_balance("permit-a")
        self.assertEqual(balance["used_kg"], D("0"))
        self.assertEqual(balance["remaining_kg"], D("20"))

    def test_boundary_violation_skips_quota_check(self):
        _, ledger, _ = build_world(quota="1")
        out = ledger.ingest_patrol(
            "p1", "w", [make_event(1, mass="5", lat="31.2200", lon="103.5050")]
        )
        self.assertEqual(ref(out, 1).reasons, (QuarantineReason.OUTSIDE_PARCEL,))


class PermitLifecycleTests(unittest.TestCase):
    def test_unused_permit_can_be_suspended(self):
        _, ledger, _ = build_world()
        ledger.suspend_permit("permit-a", "admin-zhao", date(2026, 9, 9), "管制")
        out = ledger.ingest_patrol(
            "p1", "w", [make_event(1, captured="2026-09-10T09:00:00")]
        )
        self.assertIn(QuarantineReason.PERMIT_SUSPENDED, ref(out, 1).reasons)

    def test_used_permit_cannot_be_suspended(self):
        _, ledger, _ = build_world()
        ledger.ingest_patrol("p1", "w", [make_event(1, mass="1")])
        with self.assertRaises(LedgerError):
            ledger.suspend_permit("permit-a", "a", date(2026, 9, 12), "x")

    def test_event_before_suspension_still_valid(self):
        _, ledger, _ = build_world()
        ledger.suspend_permit("permit-a", "a", date(2026, 9, 11), "x")
        out = ledger.ingest_patrol(
            "p1", "w", [make_event(1, captured="2026-09-10T09:00:00")]
        )
        self.assertEqual(ref(out, 1).event.disposition, EventDisposition.COUNTED)

    def test_expired_permit(self):
        _, ledger, _ = build_world()
        out = ledger.ingest_patrol(
            "p1", "w", [make_event(1, captured="2026-12-01T09:00:00")]
        )
        self.assertIn(QuarantineReason.PERMIT_EXPIRED, ref(out, 1).reasons)

    def test_unknown_permit_quarantined(self):
        _, ledger, _ = build_world()
        out = ledger.ingest_patrol("p1", "w", [make_event(1, permit="permit-x")])
        self.assertIn(QuarantineReason.NO_VALID_PERMIT, ref(out, 1).reasons)
        self.assertIsNone(ref(out, 1).verdict)


class CorrectionTests(unittest.TestCase):
    def test_correction_is_appended_never_replaces(self):
        _, ledger, _ = build_world()
        ledger.ingest_patrol("p1", "w", [make_event(1, mass="18")])
        ledger.append_correction("field-3", 1, "w", D("17"), "复称",
                                 at=datetime(2026, 9, 13, 10, 0))
        record = ledger.record("field-3", 1)
        self.assertEqual(record.event.mass_kg, D("18"))  # 原始量不变
        self.assertEqual(len(record.corrections), 1)
        self.assertEqual(record.effective_mass_kg, D("17"))

    def test_upward_correction_that_breaches_quota_rejected(self):
        _, ledger, _ = build_world(quota="20")
        ledger.ingest_patrol("p1", "w", [make_event(1, mass="18")])
        with self.assertRaises(LedgerError):
            ledger.append_correction("field-3", 1, "w", D("25"), "补量")

    def test_downward_correction_frees_balance(self):
        _, ledger, _ = build_world(quota="20")
        ledger.ingest_patrol("p1", "w", [make_event(1, mass="18")])
        ledger.append_correction("field-3", 1, "w", D("10"), "复称")
        self.assertEqual(ledger.permit_balance("permit-a")["used_kg"], D("10"))


class AppealTests(unittest.TestCase):
    def _quarantined(self, ledger):
        # 核心区边缘存疑件。
        ledger.ingest_patrol(
            "p1", "ranger-wang",
            [make_event(20, lat="31.20527", lon="103.50500", accuracy="36")],
        )

    def test_appeal_requires_different_reviewer(self):
        _, ledger, _ = build_world()
        self._quarantined(ledger)
        ledger.open_appeal("field-3", 20, "community-leader-1")
        with self.assertRaises(LedgerError):
            ledger.decide_appeal("field-3", 20, "community-leader-1",
                                 False, "自己复核")
        with self.assertRaises(LedgerError):
            ledger.decide_appeal("field-3", 20, "ranger-wang",
                                 False, "原承办人复核")

    def test_core_uncertain_appeal_upheld(self):
        _, ledger, _ = build_world()
        self._quarantined(ledger)
        ledger.open_appeal("field-3", 20, "community-leader-1")
        decision = ledger.decide_appeal("field-3", 20, "ranger-liu",
                                        False, "存疑从禁")
        self.assertEqual(decision.status, AppealStatus.UPHELD)
        self.assertFalse(ledger.record("field-3", 20).is_legal)

    def test_quota_release_rechecked_against_ledgers(self):
        _, ledger, _ = build_world(quota="10", team_cap="10")
        ledger.ingest_patrol("p1", "w", [make_event(1, mass="12")])
        ledger.open_appeal("field-3", 1, "community-leader-1")
        # 无勘误直接要求解除，四本账仍超限，必须拒绝。
        with self.assertRaises(LedgerError):
            ledger.decide_appeal("field-3", 1, "ranger-liu", True, "放行")

    def test_quota_release_after_correction_succeeds(self):
        _, ledger, _ = build_world(quota="10", team_cap="10")
        ledger.ingest_patrol("p1", "ranger-wang", [make_event(1, mass="12")])
        ledger.open_appeal("field-3", 1, "community-leader-1")
        ledger.append_correction("field-3", 1, "ranger-wang", D("8"), "复称")
        ledger.decide_appeal("field-3", 1, "ranger-liu", True, "余额充足")
        record = ledger.record("field-3", 1)
        self.assertTrue(record.is_legal)
        self.assertEqual(ledger.permit_balance("permit-a")["used_kg"], D("8"))


class FreezeTests(unittest.TestCase):
    def test_approved_permit_uses_frozen_survey_and_parcel(self):
        from domain.contracts import Parcel, Species
        from .helpers import SQUARE

        registry, ledger, _ = build_world()
        # 事后发布新调查版本与扩大核心区的新地理版本。
        registry.register_species(
            Species("QIANGHUO", "羌活", ProtectionLevel.STRICTLY_PROTECTED,
                    "survey-2026-3")
        )
        registry.register_parcel(
            Parcel("p-17", "geo-2026-2", SQUARE, core_zones=(SQUARE,))
        )
        out = ledger.ingest_patrol(
            "p1", "w", [make_event(1, lat="31.2020", lon="103.5020")]
        )
        # 许可冻结在 survey-2026-2 / geo-2026-1，既不因新调查被禁，
        # 也不因新核心区扩大而被隔离。
        self.assertEqual(ref(out, 1).event.disposition, EventDisposition.COUNTED)
        self.assertEqual(ref(out, 1).verdict.parcel_version, "geo-2026-1")

    def test_strictly_protected_species_permit_rejected(self):
        from domain.contracts import Species
        from domain.registry import PermitRecord, RegistryError

        registry, _, _ = build_world()
        registry.register_species(
            Species("QIANGHUO2", "羌活(禁采)",
                    ProtectionLevel.STRICTLY_PROTECTED, "survey-2026-2")
        )
        frozen = HarvestPermit(
            permit_id="permit-forbidden",
            team_id="team-7",
            species_code="QIANGHUO2",
            survey_version="survey-2026-2",
            parcel_id="p-17",
            parcel_version="geo-2026-1",
            valid_from=date(2026, 8, 1),
            valid_until=date(2026, 10, 31),
            quota_kg=D("5"),
        )
        with self.assertRaises(RegistryError):
            registry.add_permit(
                PermitRecord(frozen, approved_by="a", approved_at=date(2026, 7, 1))
            )


if __name__ == "__main__":
    unittest.main()
