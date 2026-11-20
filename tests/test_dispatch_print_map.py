"""배차표 인쇄용 노선도는 순서, 경로 종류와 위치 누락을 정확히 표시한다."""

from pathlib import Path
import sys
import unittest
from xml.etree import ElementTree


sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "pc"))
from dgpc.dispatch_print_map import route_figure


VISITS = [
    ("출발", (126.92121, 35.15121)),
    ("1", (126.92555, 35.15333)),
    ("하차1", (126.92999, 35.15444)),
    ("2", (126.93001, 35.15678)),
    ("종료", (126.92121, 35.15121)),
]


class DispatchPrintMapTest(unittest.TestCase):
    def test_protected_flow_prints_visit_order_without_geo_data(self):
        result = ElementTree.fromstring(route_figure(VISITS))
        self.assertEqual([node.text for node in result.findall(".//ol/li")],
                         ["출발", "1", "하차1", "2", "종료"])
        self.assertIsNone(result.find(".//svg"))
        self.assertNotIn("126.92555", ElementTree.tostring(result, encoding="unicode"))

    def test_road_geometry_is_printed_with_direction_and_distinct_visit_labels(self):
        coordinates = [[126.92121, 35.15121], [126.924, 35.15121], [126.92555, 35.15333],
                       [126.92999, 35.15444], [126.93001, 35.15678], [126.92121, 35.15121]]
        html = route_figure(VISITS, geographic=True, geometry={"type": "LineString", "coordinates": coordinates},
                            distance_km=2.38)
        result = ElementTree.fromstring(html)
        svg = result.find(".//svg")
        self.assertIsNotNone(svg)
        self.assertIn("2.38 km", html)
        self.assertIn("© OpenStreetMap contributors", html)
        self.assertNotIn("126.92555", html)
        self.assertNotIn("35.15333", html)
        self.assertIsNotNone(svg.find(".//*[@class='print-route-road']"))
        self.assertIsNotNone(svg.find(".//*[@class='print-route-arrow']"))
        self.assertIsNone(svg.find(".//*[@class='print-route-straight']"))
        self.assertEqual([mark.text for mark in svg.findall(".//text[@class='print-route-stop']")],
                         ["출발", "종료", "1", "하차1", "2"])

    def test_straight_fallback_does_not_connect_across_an_unknown_stop(self):
        visits = [*VISITS[:2], ("하차1", None), *VISITS[3:]]
        result = ElementTree.fromstring(route_figure(visits, geographic=True))
        fallback = result.find(".//*[@class='print-route-straight']")
        self.assertIsNotNone(fallback)
        self.assertEqual(fallback.get("d").count("M"), 2)
        self.assertTrue(fallback.get("stroke-dasharray"))
        self.assertIsNone(result.find(".//*[@class='print-route-arrow']"))
        self.assertIn("위치 없는 지점 1곳", "".join(result.itertext()))

    def test_missing_locations_fall_back_to_escaped_visit_labels(self):
        html = route_figure([("1<A", None), ("2", None)], geographic=True)
        result = ElementTree.fromstring(html)
        self.assertEqual([node.text for node in result.findall(".//ol/li")], ["1<A", "2"])
        self.assertNotIn("1<A", html)
        self.assertIn("위치 없는 지점 2곳", "".join(result.itertext()))


if __name__ == "__main__":
    unittest.main()
