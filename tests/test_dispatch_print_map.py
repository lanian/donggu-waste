"""배차표 인쇄용 노선도는 순서, 경로 종류와 위치 누락을 정확히 표시한다."""

from pathlib import Path
import re
import sys
import unittest
from unittest.mock import Mock, patch
from xml.etree import ElementTree


sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "pc"))
from dgpc.dispatch_print_map import route_figure
from dgpc import server


VISITS = [
    ("1", (126.92121, 35.15121)),
    ("2", (126.92555, 35.15333)),
    ("3", (126.92999, 35.15444)),
    ("4", (126.93001, 35.15678)),
    ("5", (126.92121, 35.15121)),
]


class DispatchPrintMapTest(unittest.TestCase):
    def test_protected_flow_prints_visit_order_without_geo_data(self):
        result = ElementTree.fromstring(route_figure([VISITS[:3], VISITS[3:]]))
        self.assertEqual([node.text for node in result.findall(".//ol/li")],
                         ["1", "2", "3", "4", "5"])
        self.assertIsNone(result.find(".//svg"))
        self.assertNotIn("126.92555", ElementTree.tostring(result, encoding="unicode"))

    def test_road_geometry_is_printed_with_direction_and_distinct_visit_labels(self):
        coordinates = [[126.92121, 35.15121], [126.924, 35.15121], [126.92555, 35.15333],
                       [126.92999, 35.15444], [126.93001, 35.15678], [126.92121, 35.15121]]
        road = {"source": "OSRM", "geometry": {"type": "LineString", "coordinates": coordinates},
                "distance_km": 2.38}
        html = route_figure([VISITS], geographic=True, roads=[road])
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
        self.assertCountEqual([mark.text for mark in svg.findall(".//text[@class='print-route-stop']")],
                              ["1", "2", "3", "4", "5"])

    def test_straight_fallback_does_not_connect_across_an_unknown_stop(self):
        visits = [*VISITS[:2], ("3", None), *VISITS[3:]]
        result = ElementTree.fromstring(route_figure([visits], geographic=True))
        fallback = result.findall(".//*[@class='print-route-straight']")
        self.assertTrue(fallback)
        self.assertEqual(sum(node.get("d").count("M") for node in fallback), 2)
        self.assertTrue(all(node.get("stroke-dasharray") for node in fallback))
        self.assertIsNone(result.find(".//*[@class='print-route-arrow']"))
        self.assertIn("1곳", "".join(result.itertext()))

    def test_missing_locations_fall_back_to_escaped_visit_labels(self):
        html = route_figure([[("1<A", None), ("2", None)]], geographic=True)
        result = ElementTree.fromstring(html)
        self.assertEqual([node.text for node in result.findall(".//ol/li")], ["1<A", "2"])
        self.assertNotIn("1<A", html)
        self.assertIn("2곳", "".join(result.itertext()))

    def test_road_and_straight_runs_do_not_bridge_the_gap(self):
        first, second = VISITS[:2], VISITS[2:4]
        coordinates = [point for _, point in first]
        road = {"source": "OSRM", "geometry": {"type": "LineString", "coordinates": coordinates},
                "distance_km": 0.5}
        svg = ElementTree.fromstring(route_figure([first, second], geographic=True, roads=[road, {}])).find(".//svg")
        road_lines = svg.findall(".//*[@class='print-route-road']")
        estimates = svg.findall(".//*[@class='print-route-straight']")
        self.assertEqual(len(road_lines), 1)
        self.assertEqual(len(estimates), 1)
        self.assertEqual(road_lines[0].get("d").count("L"), 1)
        self.assertEqual(estimates[0].get("d").count("L"), 1)


class DispatchSheetMapTest(unittest.TestCase):
    depot = {"lon": 126.81, "lat": 35.29}
    unload = {"lon": 127.05, "lat": 35.02, "eta": "11:10", "label": "가상 반입지",
              "minutes": 20, "volume_m3": 2}

    def stop(self, seq, point, unload=None):
        stop = {"seq": seq, "name": "가상 주민", "phone": "", "address": "가상 주소",
                "items": [], "revisit": False, "qr_frames": []}
        if point:
            stop["lon"], stop["lat"] = point
        if unload:
            stop["unload_after"] = unload
        return stop

    def print_sheet(self, stops, *, protected=False, positions=False, detached=None):
        sheet = {"work_date": "2099-01-01", "crew": "가상 팀", "assign_ver": 1, "fee_version": "시험",
                 "printed_at": "09:00", "stops": stops, "final_unload": detached,
                 "map_route": {"depot": self.depot, "end": self.depot, "complete": True}}
        batch = {"id": "batch", "crew_id": "crew", "status": "ACTIVE"}
        handler = Mock()
        handler._batch_print_access.return_value = None
        with patch.object(server.S, "one", return_value=batch), \
                patch.object(server.S, "dispatch_sheet", return_value=sheet), \
                patch.object(server.S, "local_osrm", return_value=("", "DISABLED")), \
                patch.object(server.DW, "road_history_basis", return_value={}), \
                patch.object(server.AS, "print_sheet_stamp", return_value="unchanged"):
            server.Handler.print_page(handler, "batch", mode="protected" if protected else "plain",
                                      route_mode="positions" if positions else None)
        handler.send_bytes.assert_called_once()
        page = handler.send_bytes.call_args.args[0].decode()
        match = re.search(r'<section\b[^>]*class="print-route"[^>]*>.*?</section>', page, re.DOTALL)
        return page, ElementTree.fromstring(match.group()) if match else None

    def test_only_pickups_set_print_extent_and_no_line_crosses_unload(self):
        stops = [self.stop(2, (126.92, 35.15), self.unload),
                 self.stop(4, (126.921, 35.151), {**self.unload, "final": True})]
        for protected in (False, True):
            with self.subTest(protected=protected):
                page, figure = self.print_sheet(stops, protected=protected, positions=protected)
                self.assertIn("가상 반입지 하차", page)
                self.assertIn("가상 반입지 최종 하차 후 차고지 복귀", page)
                self.assertEqual([node.text for node in figure.findall(".//*[@class='print-route-stop']")],
                                 ["2", "4"])
                markers = figure.findall(".//circle")
                self.assertEqual(len(markers), 2)
                # 멀리 떨어진 차고지·반입지가 축척에 끼면 두 인접 수거지는 몇 픽셀로 겹친다.
                self.assertGreater(abs(float(markers[0].get("cy")) - float(markers[1].get("cy"))), 100)
                for style in ("road", "arrow", "straight"):
                    self.assertIsNone(figure.find(f".//*[@class='print-route-{style}']"))

    def test_protected_order_shows_only_pickups_and_no_coordinates(self):
        stops = [self.stop(2, (126.92, 35.15), self.unload), self.stop(4, (126.921, 35.151))]
        page, figure = self.print_sheet(stops, protected=True)
        self.assertEqual([node.text for node in figure.findall(".//ol/li")], ["2", "4"])
        self.assertIsNone(figure.find(".//svg"))
        for private in ("가상 주소", "가상 주민", "126.92"):
            self.assertNotIn(private, page)
        self.assertIn("가상 반입지 하차", page)

    def test_local_street_map_surrounds_one_or_more_pickups_in_estimate_mode(self):
        stops = [self.stop(1, (126.922, 35.146)), self.stop(2, (126.923, 35.148))]
        for count in (1, 2):
            with self.subTest(count=count):
                with patch("socket.socket.connect", side_effect=AssertionError("도로 배경은 오프라인이어야 합니다.")) as network:
                    page, figure = self.print_sheet(stops[:count])
                network.assert_not_called()
                base = figure.find(".//*[@class='print-map-base']")
                self.assertIsNotNone(base, "수거지 주위의 실제 도로 배경이 없습니다.")
                streets = [node for node in base.iter("path") if "print-map-street" in node.get("class", "").split()]
                self.assertTrue(any(node.get("d") for node in streets))
                labels = [node.text or "" for node in base.iter("text")
                          if "print-map-street-name" in node.get("class", "").split()]
                self.assertTrue(any(re.search(r"[가-힣]", label) for label in labels))
                markers = figure.findall(".//*[@class='print-route-stop']")
                self.assertEqual([m.text for m in markers], [str(i + 1) for i in range(count)])
                nodes = list(figure.iter())
                self.assertLess(max(nodes.index(s) for s in streets), nodes.index(markers[0]))
                self.assertIn("© OpenStreetMap contributors", "".join(figure.itertext()))
                for raw in ("126.922", "35.146", "126.923", "35.148"):
                    self.assertNotIn(raw, page)

    def test_single_pickup_and_missing_pickup_never_invent_a_leg(self):
        a = self.stop(1, (126.92, 35.15))
        b = self.stop(2, None)
        c = self.stop(3, (126.921, 35.151))
        for stops, labels in (([a], ["1"]), ([a, b, c], ["1", "3"])):
            with self.subTest(labels=labels):
                _, figure = self.print_sheet(stops)
                self.assertEqual([n.text for n in figure.findall(".//*[@class='print-route-stop']")], labels)
                for style in ("road", "arrow", "straight"):
                    self.assertIsNone(figure.find(f".//*[@class='print-route-{style}']"))
                if b in stops:
                    self.assertIn("1곳", "".join(figure.itertext()))

    def test_detached_unload_remains_in_table_without_printing_a_map(self):
        for protected, positions in ((False, False), (True, True), (True, False)):
            with self.subTest(protected=protected, positions=positions):
                page, figure = self.print_sheet([], protected=protected, positions=positions,
                                                detached=self.unload)
                self.assertIn("가상 반입지 최종 하차 후 차고지 복귀", page)
                self.assertIsNone(figure)


if __name__ == "__main__":
    unittest.main()
