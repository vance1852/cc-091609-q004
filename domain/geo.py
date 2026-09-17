"""坐标、地块边界与判定依据。

现场 GPS 存在精度误差，因此边界判定从不直接回答“点是否在多边形内”，
而是输出 :class:`BoundaryVerdict`：

* 若设备精度圆盘完全位于地块内/核心区外，结论确定；
* 若圆盘跨在边界上（例如雨后补录点落在核心保护区边缘），结论不确定，
  调用方按“存疑从禁”隔离，并把原点、精度、到各边界的最短距离一并留存。
"""

from dataclasses import dataclass
from decimal import Decimal

from .contracts import GeoPoint, Parcel, Ring

EARTH_RADIUS_METERS = 6_371_000


def haversine_meters(a: GeoPoint, b: GeoPoint) -> Decimal:
    """两点大圆距离（米）。"""
    from math import asin, cos, radians, sin, sqrt

    lat1, lon1 = radians(float(a.latitude)), radians(float(a.longitude))
    lat2, lon2 = radians(float(b.latitude)), radians(float(b.longitude))
    dlat, dlon = lat2 - lat1, lon2 - lon1
    h = sin(dlat / 2) ** 2 + cos(lat1) * cos(lat2) * sin(dlon / 2) ** 2
    return Decimal(str(2 * EARTH_RADIUS_METERS * asin(min(1.0, sqrt(h)))))


def _point_in_ring(point: GeoPoint, ring: Ring) -> bool:
    """射线法判定点是否在环内（边界上算作在内）。"""
    inside = False
    n = len(ring)
    if n < 3:
        return False
    x, y = float(point.longitude), float(point.latitude)
    j = n - 1
    for i in range(n):
        xi, yi = float(ring[i].longitude), float(ring[i].latitude)
        xj, yj = float(ring[j].longitude), float(ring[j].latitude)
        if (xi - x) * (xj - x) <= 0 and (yi - y) * (yj - y) <= 0:
            cross = (xj - xi) * (y - yi) - (yj - yi) * (x - xi)
            if abs(cross) < 1e-9 and min(xi, xj) <= x <= max(xi, xj):
                return True
        intersects = (yi > y) != (yj > y)
        if intersects:
            cross_x = xi + (y - yi) * (xj - xi) / (yj - yi)
            if x < cross_x:
                inside = not inside
        j = i
    return inside


def _distance_to_segment(point: GeoPoint, a: GeoPoint, b: GeoPoint) -> Decimal:
    """等距圆柱近似下点到线段的平面距离（米），用于短距离缓冲比较。"""
    from math import cos, radians

    lat0 = radians(float(point.latitude))
    mx = Decimal(str(111_320.0 * cos(lat0)))
    my = Decimal("110540")

    def xy(p: GeoPoint) -> tuple[Decimal, Decimal]:
        return (p.longitude * mx, p.latitude * my)

    px, py = xy(point)
    ax, ay = xy(a)
    bx, by = xy(b)
    dx, dy = bx - ax, by - ay
    length_sq = dx * dx + dy * dy
    if length_sq == 0:
        t = Decimal(0)
    else:
        t = ((px - ax) * dx + (py - ay) * dy) / length_sq
        t = max(Decimal(0), min(Decimal(1), t))
    qx, qy = ax + t * dx, ay + t * dy
    return ((px - qx) ** 2 + (py - qy) ** 2).sqrt()


def distance_to_ring(point: GeoPoint, ring: Ring) -> Decimal:
    """点到环边界的最短距离；环内点返回 0。"""
    return Decimal(0) if _point_in_ring(point, ring) else _edge_distance(point, ring)


@dataclass(frozen=True)
class BoundaryVerdict:
    """一次边界判定的完整依据，随事件永久留存。"""

    parcel_id: str
    parcel_version: str
    origin: GeoPoint
    accuracy_meters: Decimal
    inside_parcel: bool
    """原点判定为在地块内（含边界）。"""
    parcel_certain: bool
    """精度圆盘是否全部位于地块一侧，False 表示跨边界、结论存疑。"""
    distance_to_parcel_boundary_m: Decimal
    inside_core_zone: bool
    core_zone_index: int | None
    core_certain: bool
    distance_to_core_boundary_m: Decimal | None

    @property
    def clearly_outside(self) -> bool:
        return not self.inside_parcel and self.parcel_certain

    @property
    def parcel_uncertain(self) -> bool:
        return not self.parcel_certain

    @property
    def clearly_inside_core(self) -> bool:
        return self.inside_core_zone and self.core_certain

    @property
    def core_uncertain(self) -> bool:
        return self.inside_core_zone and not self.core_certain


def evaluate_boundary(point: GeoPoint, accuracy_meters: Decimal, parcel: Parcel) -> BoundaryVerdict:
    """以“原点 ± 设备精度圆盘”判定点相对地块与核心区的位置。

    两侧各有三种结论：明确在内、明确在外、圆盘跨边界（存疑）。
    """
    inside = _point_in_ring(point, parcel.polygon)
    d_parcel = _edge_distance(point, parcel.polygon)
    parcel_certain = d_parcel > accuracy_meters

    core_index: int | None = None
    crosses_core = False
    d_core: Decimal | None = None
    for idx, zone in enumerate(parcel.core_zones):
        in_zone = _point_in_ring(point, zone)
        edge = _edge_distance(point, zone)
        if in_zone and core_index is None:
            core_index = idx
            d_core = edge
        elif d_core is None or edge < d_core:
            # 环外：保留到最近核心区边界的距离。
            d_core = edge
        if edge <= accuracy_meters:
            # 精度圆盘与核心区边界相交，真实位置可能在核心区内。
            crosses_core = True

    return BoundaryVerdict(
        parcel_id=parcel.parcel_id,
        parcel_version=parcel.parcel_version,
        origin=point,
        accuracy_meters=accuracy_meters,
        inside_parcel=inside,
        parcel_certain=parcel_certain,
        distance_to_parcel_boundary_m=d_parcel,
        inside_core_zone=core_index is not None,
        core_zone_index=core_index,
        core_certain=not crosses_core,
        distance_to_core_boundary_m=d_core,
    )


def _edge_distance(point: GeoPoint, ring: Ring) -> Decimal:
    """无论点在环内还是环外，到环边的最短距离。"""
    if len(ring) < 2:
        return Decimal(0)
    best = None
    for i in range(len(ring)):
        d = _distance_to_segment(point, ring[i], ring[(i + 1) % len(ring)])
        best = d if best is None or d < best else best
    return best or Decimal(0)
