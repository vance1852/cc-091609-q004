"""空间几何判定测试。"""

import unittest
from decimal import Decimal

from wildharvest import geo


class GeoTest(unittest.TestCase):
    def setUp(self) -> None:
        # 约 400m 见方地块：纬度 0.0036°≈400m，经度在 31°N 约 0.0042°≈400m
        self.parcel = [
            [31.1000, 103.2000],
            [31.1036, 103.2000],
            [31.1036, 103.2042],
            [31.1000, 103.2042],
        ]
        self.core = [
            [31.1026, 103.2020],
            [31.1040, 103.2020],
            [31.1040, 103.2034],
            [31.1026, 103.2034],
        ]

    def test_point_clearly_inside_parcel(self) -> None:
        v = geo.classify_against_parcel(self.parcel, Decimal("31.1010"), Decimal("103.2010"), Decimal("5"))
        self.assertEqual(v.verdict, "within")
        self.assertTrue(v.point_inside)

    def test_point_outside_with_small_buffer(self) -> None:
        # 南界在 31.1000，点在界外约 56m，精度 10m，明确越界
        v = geo.classify_against_parcel(self.parcel, Decimal("31.0995"), Decimal("103.2010"), Decimal("10"))
        self.assertEqual(v.verdict, "outside")
        self.assertGreater(v.edge_distance_m, 40)

    def test_buffer_crosses_parcel_edge(self) -> None:
        # 点在界内约 11m，精度 36m 时圆伸出界外
        v = geo.classify_against_parcel(self.parcel, Decimal("31.1001"), Decimal("103.2010"), Decimal("36"))
        self.assertEqual(v.verdict, "buffer_crosses")
        self.assertLess(v.edge_distance_m, 36)

    def test_core_clear_inside_and_buffer(self) -> None:
        clear = geo.classify_against_core(self.core, Decimal("31.1010"), Decimal("103.2025"), Decimal("10"))
        self.assertEqual(clear.verdict, "clear")

        inside = geo.classify_against_core(self.core, Decimal("31.1030"), Decimal("103.2027"), Decimal("5"))
        self.assertEqual(inside.verdict, "inside")

        # 南界 31.1026，点在界南约 22m，精度 36m 圆触及核心区
        touch = geo.classify_against_core(self.core, Decimal("31.1024"), Decimal("103.2027"), Decimal("36"))
        self.assertEqual(touch.verdict, "buffer_crosses")

    def test_overlap_detection(self) -> None:
        other = [
            [31.1030, 103.2030],
            [31.1050, 103.2030],
            [31.1050, 103.2050],
            [31.1030, 103.2050],
        ]
        disjoint = [
            [40.0000, 80.0000],
            [40.0010, 80.0000],
            [40.0010, 80.0010],
            [40.0000, 80.0010],
        ]
        self.assertTrue(geo.polygons_overlap(self.parcel, other))
        self.assertFalse(geo.polygons_overlap(self.parcel, disjoint))
        self.assertEqual(geo.find_overlaps({"a": self.parcel, "b": other, "c": disjoint}), [("a", "b")])


if __name__ == "__main__":
    unittest.main()
