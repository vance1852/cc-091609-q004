"""资源调查与地理范围的版本注册表。

许可审批时必须引用具体的 ``survey_version`` 与 ``parcel_version``；
此后即使发布了新调查或调整了核心区边界，已批许可的判定仍按冻结版本执行。
"""

from dataclasses import dataclass
from datetime import date

from .contracts import (
    HarvestPermit,
    Parcel,
    PermitStatus,
    ProtectionLevel,
    SeasonalLimits,
    Species,
)


class RegistryError(ValueError):
    pass


@dataclass(frozen=True)
class PermitRecord:
    permit: HarvestPermit
    approved_by: str
    approved_at: date
    status: PermitStatus = PermitStatus.ACTIVE
    suspended_by: str | None = None
    suspended_at: date | None = None
    suspend_reason: str | None = None


class VersionedRegistry:
    def __init__(self) -> None:
        self._species: dict[tuple[str, str], Species] = {}
        self._parcels: dict[tuple[str, str], Parcel] = {}
        self._seasons: dict[str, SeasonalLimits] = {}
        self._permits: dict[str, PermitRecord] = {}

    # ---- 调查与地理版本登记 -------------------------------------------------

    def register_species(self, species: Species) -> None:
        self._species[(species.species_code, species.survey_version)] = species

    def register_parcel(self, parcel: Parcel) -> None:
        if len(parcel.polygon) < 3:
            raise RegistryError(f"地块 {parcel.parcel_id} 边界至少需要 3 个点")
        self._parcels[(parcel.parcel_id, parcel.parcel_version)] = parcel

    def register_season(self, limits: SeasonalLimits) -> None:
        self._seasons[limits.season] = limits

    def species_at(self, species_code: str, survey_version: str) -> Species:
        key = (species_code, survey_version)
        if key not in self._species:
            raise RegistryError(f"调查版本 {survey_version} 中不存在物种 {species_code}")
        return self._species[key]

    def parcel_at(self, parcel_id: str, parcel_version: str) -> Parcel:
        key = (parcel_id, parcel_version)
        if key not in self._parcels:
            raise RegistryError(f"地理版本 {parcel_version} 中不存在地块 {parcel_id}")
        return self._parcels[key]

    def season(self, season: str) -> SeasonalLimits:
        if season not in self._seasons:
            raise RegistryError(f"未登记采集年度 {season}")
        return self._seasons[season]

    # ---- 许可 ---------------------------------------------------------------

    def add_permit(self, record: PermitRecord) -> HarvestPermit:
        if record.permit.permit_id in self._permits:
            raise RegistryError(f"许可编号重复：{record.permit.permit_id}")
        p = record.permit
        # 冻结版本必须能在注册表中查到，杜绝“先批后补”。
        species = self.species_at(p.species_code, p.survey_version)
        if species.protection_level is ProtectionLevel.STRICTLY_PROTECTED:
            raise RegistryError(f"物种 {p.species_code} 属一级保护，不得签发采集许可")
        self.parcel_at(p.parcel_id, p.parcel_version)
        if p.valid_until < p.valid_from:
            raise RegistryError("许可有效期起止日期颠倒")
        if p.quota_kg <= 0:
            raise RegistryError("许可限额必须为正数")
        self._permits[p.permit_id] = record
        return p

    def permit_record(self, permit_id: str) -> PermitRecord:
        if permit_id not in self._permits:
            raise RegistryError(f"许可不存在：{permit_id}")
        return self._permits[permit_id]

    def replace_permit_record(self, record: PermitRecord) -> None:
        """供暂停等状态流转使用，许可本身（frozen）不被修改。"""
        if record.permit.permit_id not in self._permits:
            raise RegistryError(f"许可不存在：{record.permit.permit_id}")
        self._permits[record.permit.permit_id] = record

    @property
    def permits(self) -> tuple[PermitRecord, ...]:
        return tuple(self._permits.values())
