"""共享构造工具。"""

from datetime import date, datetime
from decimal import Decimal as D

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
from domain.audit import Journal
from domain.ledger import HarvestLedger
from domain.registry import PermitRecord, VersionedRegistry

SQUARE = (
    GeoPoint(D("31.2000"), D("103.5000")),
    GeoPoint(D("31.2100"), D("103.5000")),
    GeoPoint(D("31.2100"), D("103.5100")),
    GeoPoint(D("31.2000"), D("103.5100")),
)
CORE = (
    GeoPoint(D("31.20473"), D("103.50468")),
    GeoPoint(D("31.20527"), D("103.50468")),
    GeoPoint(D("31.20527"), D("103.50532")),
    GeoPoint(D("31.20473"), D("103.50532")),
)


def build_world(
    *,
    quota: str = "45",
    team_cap: str | None = "70",
    parcel_cap: str | None = None,
    species_cap: str | None = None,
    core: tuple = (CORE,),
    parcel_id: str = "p-17",
    species_level: ProtectionLevel = ProtectionLevel.LICENSED,
) -> tuple[VersionedRegistry, HarvestLedger, Journal]:
    registry = VersionedRegistry()
    registry.register_species(
        Species("QIANGHUO", "羌活", species_level, "survey-2026-2")
    )
    registry.register_parcel(
        Parcel(parcel_id, "geo-2026-1", SQUARE, core_zones=core)
    )
    registry.register_season(
        SeasonalLimits(
            season="2026",
            team_kg={"team-7": D(team_cap)} if team_cap else {},
            parcel_kg={parcel_id: D(parcel_cap)} if parcel_cap else {},
            species_kg={"QIANGHUO": D(species_cap)} if species_cap else {},
        )
    )
    permit = HarvestPermit(
        permit_id="permit-a",
        team_id="team-7",
        species_code="QIANGHUO",
        survey_version="survey-2026-2",
        parcel_id=parcel_id,
        parcel_version="geo-2026-1",
        valid_from=date(2026, 8, 1),
        valid_until=date(2026, 10, 31),
        quota_kg=D(quota),
    )
    registry.add_permit(
        PermitRecord(permit, approved_by="admin-zhao", approved_at=date(2026, 7, 20))
    )
    journal = Journal()
    ledger = HarvestLedger(registry, journal)
    return registry, ledger, journal


def make_event(
    sequence: int,
    *,
    lat: str = "31.2020",
    lon: str = "103.5020",
    accuracy: str = "12",
    mass: str = "10",
    captured: str = "2026-09-10T09:00:00",
    received: str = "2026-09-10T11:00:00",
    device: str = "field-3",
    permit: str = "permit-a",
) -> HarvestEvent:
    return HarvestEvent(
        device_id=device,
        sequence=sequence,
        permit_id=permit,
        captured_at=datetime.fromisoformat(captured),
        received_at=datetime.fromisoformat(received),
        latitude=D(lat),
        longitude=D(lon),
        accuracy_meters=D(accuracy),
        mass_kg=D(mass),
        disposition=EventDisposition.PENDING,
    )
