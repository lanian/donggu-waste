"""인쇄용 주변 도로의 로컬 자료 선택과 지도 경계 자르기."""
import gzip
import json
from pathlib import Path
import sys
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "pc"))
from dgpc import paths
import print_streets


class PrintStreetsTest(unittest.TestCase):
    bounds = (126.90, 35.13, 126.92, 35.15)

    def setUp(self):
        root = Path(self.enterContext(TemporaryDirectory()))
        (root / "data").mkdir()
        self.asset = root / "data" / "print_streets.json.gz"
        self.enterContext(patch.object(paths, "resource_root", return_value=root))

    def write(self, roads):
        with gzip.open(self.asset, "wt", encoding="utf-8") as output:
            json.dump({"schema": 1, "roads": roads}, output, ensure_ascii=False)

    def test_crossing_and_reentering_road_is_clipped_into_distinct_lines(self):
        self.write([
            ["major", "동명로", [[126.89, 35.14], [126.93, 35.14], [126.93, 35.145], [126.91, 35.145]]],
            ["street", "화면 밖", [[126.95, 35.14], [126.96, 35.145]]],
        ])
        lines = print_streets.roads_in_bounds(self.bounds)
        self.assertEqual([(kind, name) for kind, name, _ in lines], [("major", "동명로")] * 2)
        self.assertEqual([[(round(x, 5), round(y, 5)) for x, y in pts] for _, _, pts in lines], [
            [(126.90, 35.14), (126.92, 35.14)], [(126.92, 35.145), (126.91, 35.145)],
        ])

    def test_unavailable_or_unreadable_asset_and_invalid_bounds_give_no_background(self):
        self.assertEqual(print_streets.roads_in_bounds(self.bounds), [])
        self.asset.write_bytes(b"broken")
        self.assertEqual(print_streets.roads_in_bounds(self.bounds), [])
        for bounds in ((126.92, 35.15, 126.90, 35.13), (None, 35, 127, 35.1)):
            self.assertEqual(print_streets.roads_in_bounds(bounds), [])

    def test_bad_record_does_not_break_the_print_request(self):
        self.write([["major", "불완전", [[126.91, 35.14], [126.92, None]]]])
        self.assertEqual(print_streets.roads_in_bounds(self.bounds), [])

    def test_dense_view_caps_output_and_keeps_a_major_street(self):
        streets = [["minor", "", [[126.89, 35.13 + i/100000], [126.93, 35.13 + i/100000]]]
                   for i in range(1900)]
        self.write([*streets, ["major", "동계천로", [[126.91, 35.13], [126.91, 35.15]]]])
        lines = print_streets.roads_in_bounds(self.bounds)
        self.assertTrue(any(name == "동계천로" for _, name, _ in lines))
        self.assertLess(len(lines), 1000)
        self.assertLess(sum(len(pts) for _, _, pts in lines), 12000)
        self.assertEqual(lines, print_streets.roads_in_bounds(self.bounds))

    def test_small_details_of_long_ways_do_not_use_up_overview_budget(self):
        line = [[126.900 + i * .000002, 35.14] for i in range(10001)]
        self.write([["major", "가상 간선", line], ["street", "가상 골목", [[126.91, 35.13], [126.91, 35.15]]]])
        roads = print_streets.roads_in_bounds(self.bounds)
        self.assertEqual({name for _, name, _ in roads}, {"가상 간선", "가상 골목"})
        self.assertEqual(tuple(roads[0][2][0]), tuple(line[0]))
        self.assertEqual(tuple(roads[0][2][-1]), tuple(line[-1]))


if __name__ == "__main__":
    unittest.main()
