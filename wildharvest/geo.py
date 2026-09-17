"""WGS84 经纬度面状边界与定位精度缓冲判定（仅用标准库）。

判定规则 ``buffer-v1``：采集点的精度圆必须**整体**落入许可地块、且**整体**
位于核心保护区之外，才能视为空间合规。圆与边界相交时，原点、设备精度和到
边界的最近距离都作为判据随隔离记录一并保存。

距离使用保护区局部等距圆柱投影（纬度圈修正），在公里级尺度误差可忽略；
证据中会记录投影方式，便于复核时改用更精确的算法复核。
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from decimal import Decimal
from typing import Sequence

# WGS84 纬度方向每度约 111320 米
_METERS_PER_DEGREE_LAT = 111_320.0

Ring = Sequence[Sequence[float]]
"""闭合或不闭合的经纬度环：``[[lat, lon], ...]``。"""


@dataclass(frozen=True)
class SpatialVerdict:
    """单点相对单个面的缓冲判定结果。"""

    point_inside: bool
    accuracy_m: float
    edge_distance_m: float
    verdict: str
    """地块：within / outside / buffer_crosses；核心区：clear / inside / buffer_crosses。"""

    def as_evidence(self, projection: str = "equirectangular-wgs84") -> dict:
        return {
            "verdict": self.verdict,
            "point_inside": self.point_inside,
            "accuracy_m": _q(self.accuracy_m),
            "edge_distance_m": _q(self.edge_distance_m),
            "projection": projection,
        }


def _q(value: float, places: str = "0.01") -> str:
    return str(Decimal(str(value)).quantize(Decimal(places)))


def _project(lat: float, lon: float, lat0: float, lon0: float) -> tuple[float, float]:
    """局部投影为东向/北向米坐标。"""

    x = (lon - lon0) * _METERS_PER_DEGREE_LAT * math.cos(math.radians(lat0))
    y = (lat - lat0) * _METERS_PER_DEGREE_LAT
    return x, y


def _ring_xy(ring: Ring, lat0: float, lon0: float) -> list[tuple[float, float]]:
    return [_project(float(lat), float(lon), lat0, lon0) for lat, lon in ring]


def _point_in_polygon(x: float, y: float, poly: Sequence[tuple[float, float]]) -> bool:
    """射线法判断点是否在多边形内（边界点按在内处理）。"""

    inside = False
    n = len(poly)
    if n < 3:
        return False
    for i in range(n):
        ax, ay = poly[i]
        bx, by = poly[(i + 1) % n]
        if (ax == x and ay == y) or (bx == x and by == y):
            return True
        intersects = (ay > y) != (by > y)
        if intersects:
            cross_x = ax + (bx - ax) * (y - ay) / (by - ay)
            if x == cross_x:
                return True
            if cross_x > x:
                inside = not inside
    return inside


def _distance_to_ring(x: float, y: float, poly: Sequence[tuple[float, float]]) -> float:
    best = math.inf
    n = len(poly)
    for i in range(n):
        ax, ay = poly[i]
        bx, by = poly[(i + 1) % n]
        dx, dy = bx - ax, by - ay
        length2 = dx * dx + dy * dy
        if length2 == 0:
            t = 0.0
        else:
            t = min(1.0, max(0.0, ((x - ax) * dx + (y - ay) * dy) / length2))
        px, py = ax + t * dx, ay + t * dy
        best = min(best, math.hypot(x - px, y - py))
    return best


def _origin(ring: Ring) -> tuple[float, float]:
    lats = [float(p[0]) for p in ring]
    lons = [float(p[1]) for p in ring]
    return sum(lats) / len(lats), sum(lons) / len(lons)


def classify_against_parcel(ring: Ring, lat: Decimal, lon: Decimal, accuracy_m: Decimal) -> SpatialVerdict:
    """点相对许可地块：within / outside / buffer_crosses。"""

    lat_f, lon_f, acc = float(lat), float(lon), float(accuracy_m)
    lat0, lon0 = _origin(ring)
    poly = _ring_xy(ring, lat0, lon0)
    x, y = _project(lat_f, lon_f, lat0, lon0)
    inside = _point_in_polygon(x, y, poly)
    distance = _distance_to_ring(x, y, poly)
    if distance + 1e-9 < acc:
        verdict = "buffer_crosses"
    else:
        verdict = "within" if inside else "outside"
    return SpatialVerdict(inside, acc, distance, verdict)


def classify_against_core(ring: Ring, lat: Decimal, lon: Decimal, accuracy_m: Decimal) -> SpatialVerdict:
    """点相对核心保护区：clear / inside / buffer_crosses（语义与地块相反）。"""

    lat_f, lon_f, acc = float(lat), float(lon), float(accuracy_m)
    lat0, lon0 = _origin(ring)
    poly = _ring_xy(ring, lat0, lon0)
    x, y = _project(lat_f, lon_f, lat0, lon0)
    inside = _point_in_polygon(x, y, poly)
    distance = _distance_to_ring(x, y, poly)
    if inside:
        verdict = "inside"
    elif distance + 1e-9 < acc:
        verdict = "buffer_crosses"
    else:
        verdict = "clear"
    return SpatialVerdict(inside, acc, distance, verdict)


def polygons_overlap(a: Ring, b: Ring) -> bool:
    """两个环是否相交或互相包含（用于发布边界时发现重叠申报）。"""

    def pts(p: Ring) -> list[tuple[float, float]]:
        return [(float(lat), float(lon)) for lat, lon in p]

    def on_segment(p, q, r) -> bool:
        return (
            min(p[0], r[0]) <= q[0] <= max(p[0], r[0])
            and min(p[1], r[1]) <= q[1] <= max(p[1], r[1])
        )

    def orient(p, q, r) -> float:
        return (q[1] - p[1]) * (r[0] - q[0]) - (q[0] - p[0]) * (r[1] - q[1])

    def segments_cross(p1, p2, q1, q2) -> bool:
        o1, o2 = orient(p1, p2, q1), orient(p1, p2, q2)
        o3, o4 = orient(q1, q2, p1), orient(q1, q2, p2)
        if ((o1 > 0) != (o2 > 0)) and ((o3 > 0) != (o4 > 0)):
            return True
        return any(
            (o == 0 and on_segment(a1, m, b1))
            for o, a1, m, b1 in (
                (o1, p1, q1, p2),
                (o2, p1, q2, p2),
                (o3, q1, p1, q2),
                (o4, q1, p2, q2),
            )
        )

    pa, pb = pts(a), pts(b)
    if any(_geo_contains(a, *p) for p in pb) or any(_geo_contains(b, *p) for p in pa):
        return True
    for i in range(len(pa)):
        for j in range(len(pb)):
            if segments_cross(pa[i], pa[(i + 1) % len(pa)], pb[j], pb[(j + 1) % len(pb)]):
                return True
    return False


def _geo_contains(ring: Ring, lat: float, lon: float) -> bool:
    lat0 = sum(float(p[0]) for p in ring) / len(ring)
    lon0 = sum(float(p[1]) for p in ring) / len(ring)
    poly = _ring_xy(ring, lat0, lon0)
    x, y = _project(lat, lon, lat0, lon0)
    return _point_in_polygon(x, y, poly)


def find_overlaps(parcels: dict[str, Ring]) -> list[tuple[str, str]]:
    ids = sorted(parcels)
    return [
        (a, b)
        for i, a in enumerate(ids)
        for b in ids[i + 1 :]
        if polygons_overlap(parcels[a], parcels[b])
    ]
