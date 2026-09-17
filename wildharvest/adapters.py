"""台账读模型与 ``domain.contracts`` 数据形状之间的适配器。"""

from __future__ import annotations

from datetime import date, datetime
from decimal import Decimal

from domain.contracts import EventDisposition, HarvestEvent, HarvestPermit

from .ledger import PermitView, EventView

_DISPOSITION = {
    "counted": EventDisposition.COUNTED,
    "quarantined": EventDisposition.QUARANTINED,
    "corrected": EventDisposition.CORRECTED,
    "pending": EventDisposition.PENDING,
}


def permit_to_contract(view: PermitView) -> HarvestPermit:
    return HarvestPermit(
        permit_id=view.permit_id,
        team_id=view.team_id,
        species_code=view.species_code,
        survey_version=view.survey_version,
        parcel_version=view.parcel_version,
        valid_from=date.fromisoformat(view.valid_from),
        valid_until=date.fromisoformat(view.valid_until),
        quota_kg=view.quota_kg,
    )


def event_to_contract(view: EventView) -> HarvestEvent:
    return HarvestEvent(
        device_id=view.device_id,
        sequence=view.sequence,
        permit_id=view.permit_id,
        captured_at=view.captured_at,
        received_at=view.received_at,
        latitude=Decimal(view.current["latitude"]),
        longitude=Decimal(view.current["longitude"]),
        accuracy_meters=Decimal(view.current["accuracy_meters"]),
        mass_kg=view.mass_kg,
        disposition=_DISPOSITION[view.effective],
    )
