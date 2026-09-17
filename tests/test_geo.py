"""几何判定测试：原点、精度缓冲与存疑从禁。"""

import unittest
from decimal import Decimal as D

from domain.contracts import GeoPoint, Parcel
from domain.geo import evaluate_boundary, haversine_meters

from .helpers import CORE, SQUARE


class GeoTests(unittest.TestCase):
    def setUp(self) -> None:
        self.parcel = Parcel("p-17", "geo-2026-1", SQUARE, core_zones=(CORE,))

    def verdict(self, lat, lon, accuracy="12"):
        return evaluate_boundary(
            GeoPoint(D(lat), D(lon)), D(accuracy), self.parcel
        )

    def test_point_inside_far_from_boundary_is_certain(self):
        v = self.verdict("31.2020", "103.5020")
        self.assertTrue(v.inside_parcel)
        self.assertTrue(v.parcel_certain)
        self.assertFalse(v.inside_core_zone)
        self.assertTrue(v.core_certain)

    def test_point_outside_beyond_accuracy_is_clearly_outside(self):
        v = self.verdict("31.2200", "103.5050", "36")
        self.assertTrue(v.clearly_outside)

    def test_point_outside_but_accuracy_crosses_boundary_is_uncertain(self):
        # 地块北界 31.21，点在界外约 11m，100m 精度圆盘跨界。
        v = self.verdict("31.2101", "103.5050", "100")
        self.assertFalse(v.inside_parcel)
        self.assertFalse(v.parcel_certain)

    def test_core_zone_edge_with_rain_accuracy_is_uncertain(self):
        # 雨后补录点落在核心保护区边缘，36m 精度圆盘与核心区边界相交。
        v = self.verdict("31.20527", "103.50500", "36")
        self.assertTrue(v.inside_core_zone)
        self.assertFalse(v.core_certain)

    def test_deep_inside_core_is_certain(self):
        v = self.verdict("31.20500", "103.50500", "5")
        self.assertTrue(v.inside_core_zone)
        self.assertTrue(v.core_certain)

    def test_origin_and_evidence_are_preserved(self):
        v = self.verdict("31.20527", "103.50500", "36")
        self.assertEqual(v.origin.latitude, D("31.20527"))
        self.assertEqual(v.accuracy_meters, D("36"))
        self.assertEqual(v.parcel_version, "geo-2026-1")
        self.assertIsNotNone(v.distance_to_core_boundary_m)

    def test_haversine_known_distance(self):
        # 赤道上经度相差 0.001 度约 111.32m。
        d = haversine_meters(
            GeoPoint(D("0"), D("0")), GeoPoint(D("0"), D("0.001"))
        )
        self.assertAlmostEqual(float(d), 111.19, delta=1.0)


if __name__ == "__main__":
    unittest.main()
