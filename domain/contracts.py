"""许可、物种、空间边界和离线采集事件的数据形状。

本模块只定义形状（枚举与不可变值对象），规则与流程见同包其他模块。
坐标一律使用 ``(纬度, 经度)`` 的 :class:`GeoPoint`，质量与精度使用
:class:`decimal.Decimal`，避免台账在浮点误差上产生判定分歧。
"""

from dataclasses import dataclass, field
from datetime import date, datetime
from decimal import Decimal
from enum import StrEnum
from typing import Mapping


class ProtectionLevel(StrEnum):
    """物种保护等级（随资源调查版本冻结）。"""

    STRICTLY_PROTECTED = "strictly_protected"
    """一级保护：禁止一切生产性采集。"""

    LICENSED = "licensed"
    """二级保护：凭有效许可、在核定地块与限额内采集。"""

    SUSTAINABLE_USE = "sustainable_use"
    """可持续利用等级：仍须申报地点与数量。"""


class PermitStatus(StrEnum):
    ACTIVE = "active"
    SUSPENDED = "suspended"


class EventDisposition(StrEnum):
    PENDING = "pending"
    COUNTED = "counted"
    QUARANTINED = "quarantined"
    CORRECTED = "corrected"


class QuarantineReason(StrEnum):
    NO_VALID_PERMIT = "no_valid_permit"
    SPECIES_FORBIDDEN = "species_forbidden"
    PERMIT_EXPIRED = "permit_expired"
    PERMIT_SUSPENDED = "permit_suspended"
    OUTSIDE_PARCEL = "outside_parcel"
    BOUNDARY_UNCERTAIN = "boundary_uncertain"
    CORE_ZONE = "core_zone"
    CORE_ZONE_UNCERTAIN = "core_zone_uncertain"
    PERMIT_QUOTA_EXCEEDED = "permit_quota_exceeded"
    TEAM_QUOTA_EXCEEDED = "team_quota_exceeded"
    PARCEL_QUOTA_EXCEEDED = "parcel_quota_exceeded"
    SPECIES_QUOTA_EXCEEDED = "species_quota_exceeded"


class AppealStatus(StrEnum):
    OPEN = "open"
    UPHELD = "upheld"
    """复核维持隔离决定。"""
    RELEASED = "released"
    """复核解除隔离，事件重新参与合法收获计量。"""


@dataclass(frozen=True)
class GeoPoint:
    latitude: Decimal
    longitude: Decimal


# 多边形表示首尾自动闭合的环，点的集合在冻结后不可变。
Ring = tuple[GeoPoint, ...]


@dataclass(frozen=True)
class Species:
    """资源调查中记载的物种及其保护等级（绑定调查版本）。"""

    species_code: str
    name: str
    protection_level: ProtectionLevel
    survey_version: str


@dataclass(frozen=True)
class Parcel:
    """地块边界的一个不可变版本，核心保护区单独成环。"""

    parcel_id: str
    parcel_version: str
    polygon: Ring
    core_zones: tuple[Ring, ...] = ()


@dataclass(frozen=True)
class HarvestPermit:
    """许可审批即对调查版本与地理版本的冻结快照。"""

    permit_id: str
    team_id: str
    species_code: str
    survey_version: str
    parcel_id: str
    parcel_version: str
    valid_from: date
    valid_until: date
    quota_kg: Decimal


@dataclass(frozen=True)
class HarvestEvent:
    """采集设备签发的一条原始上报；勘误不修改本对象，只追加更正。"""

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


@dataclass(frozen=True)
class SeasonalLimits:
    """一个采集年度的三本外部限额账：团队、地块、物种。"""

    season: str
    team_kg: Mapping[str, Decimal] = field(default_factory=dict)
    parcel_kg: Mapping[str, Decimal] = field(default_factory=dict)
    species_kg: Mapping[str, Decimal] = field(default_factory=dict)

    def team(self, team_id: str) -> Decimal | None:
        return self.team_kg.get(team_id)

    def parcel(self, parcel_id: str) -> Decimal | None:
        return self.parcel_kg.get(parcel_id)

    def species(self, species_code: str) -> Decimal | None:
        return self.species_kg.get(species_code)
