"""许可、空间边界和离线采集事件。"""

from dataclasses import dataclass
from datetime import date, datetime
from decimal import Decimal
from enum import StrEnum


class EventDisposition(StrEnum):
    PENDING = "pending"
    COUNTED = "counted"
    QUARANTINED = "quarantined"
    CORRECTED = "corrected"


@dataclass(frozen=True)
class HarvestPermit:
    permit_id: str
    team_id: str
    species_code: str
    survey_version: str
    parcel_version: str
    valid_from: date
    valid_until: date
    quota_kg: Decimal


@dataclass(frozen=True)
class HarvestEvent:
    device_id: str
    sequence: int
    permit_id: str
    captured_at: datetime
    received_at: datetime
    latitude: Decimal
    longitude: Decimal
    accuracy_meters: Decimal
    mass_kg: Decimal
    disposition: EventDisposition
