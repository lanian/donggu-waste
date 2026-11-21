"""한 차량에 일곱 번의 적재가 필요한 가상 물량으로 자동·수동 중간 하차를 검증한다."""
from contextlib import closing
import os
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "pc"))
from dgpc import routing, service, store


class AutoUnloadsTest(unittest.TestCase):
    def setUp(self):
        self.point = {"lat": 35.15, "lon": 126.92}
        self.crew = {"id": "a", "name": "가상팀", "capacity_m3": 10,
                     "shift_start": "09:00", "shift_end": "14:00"}
        self.item = {"fee_rate_id": "TEST", "name": "의자", "qty": 1}
        self.jobs = [{"id": f"R{i}", "address": f"가상로 {i}", **self.point,
                      "items": [self.item]} for i in range(1, 8)]

    def plan(self, crews=None, **settings):
        options = {"travel_method": "ESTIMATE", "time_limit_s": 3, "service_base": 1,
                   "service_per_item": 0, "volume_overrides": {"TEST": 6},
                   "unload_site": self.point, "unload_min": 5, "final_unload_return": True}
        return routing.plan(self.jobs, crews or [self.crew], self.point, **{**options, **settings})

    def assert_trips(self, result, trips):
        self.assertFalse(result.get("infeasible"), result.get("unassigned"))
        route, = result["routes"]
        stops = route["stops"]
        ids = [stop["request_id"] for stop in stops]
        omitted = [entry["request_id"] for entry in result["unassigned"]]
        self.assertCountEqual(ids + omitted, [job["id"] for job in self.jobs])
        self.assertEqual(len(stops), trips)
        self.assertEqual(route["unloads"], trips)
        self.assertEqual([bool(stop["unload_after"].get("final")) for stop in stops],
                         [False] * (trips - 1) + [True])
        return route

    def test_auto_and_unspecified_allow_six_intermediate_unloads(self):
        for options in ({}, {"max_unloads": "auto"}):
            with self.subTest(options=options):
                self.assert_trips(self.plan(**options), 7)

    def test_explicit_zero_and_two_keep_their_limits(self):
        for maximum in (0, 2):
            with self.subTest(maximum=maximum):
                self.assert_trips(self.plan(max_unloads=maximum), maximum + 1)

    def test_auto_skips_intermediate_unloads_when_all_items_fit(self):
        self.crew["capacity_m3"] = 50
        result = self.plan(max_unloads="auto")
        self.assertEqual(result["unassigned"], [])
        route, = result["routes"]
        self.assertEqual(len(route["stops"]), 7)
        self.assertEqual(route["unloads"], 1)
        self.assertTrue(route["stops"][-1]["unload_after"]["final"])

    def test_auto_unloads_respect_shift_and_site_closing_times(self):
        # 위치를 일치시켜 한 건당 현장 1분 + 하차 5분만 걸린다.
        for end, hours in (("09:10", None), ("14:00", {"start": "09:00", "end": "09:10"})):
            with self.subTest(shift_end=end, hours=hours):
                self.crew["shift_end"] = end
                route = self.assert_trips(self.plan(max_unloads="auto", unload_hours=hours), 1)
                self.assertLessEqual(route["end"], "09:10")

    def test_auto_uses_a_feasible_second_trip_before_closing(self):
        for end, hours in (("09:14", None), ("14:00", {"start": "09:00", "end": "09:14"})):
            with self.subTest(shift_end=end, hours=hours):
                self.crew["shift_end"] = end
                route = self.assert_trips(self.plan(max_unloads="auto", unload_hours=hours), 2)
                self.assertLessEqual(route["end"], "09:14")

    def test_weight_limit_alone_can_require_six_intermediate_unloads(self):
        self.crew.update(capacity_m3=100, capacity_kg=routing.weight_of(self.item) + 1)
        self.assert_trips(self.plan(max_unloads="auto"), 7)

    def test_two_areas_share_auto_unload_slots_and_keep_manual_quotas_per_crew(self):
        crews = [self.crew, {**self.crew, "id": "b", "name": "다른 가상팀"}]
        teams = {job["id"]: "a" if i < 5 else "b" for i, job in enumerate(self.jobs)}
        for maximum, expected in (("auto", {"a": 5, "b": 2}), (0, {"a": 1, "b": 1}), (2, {"a": 3, "b": 2})):
            with self.subTest(max_unloads=maximum):
                result = self.plan(crews=crews, max_unloads=maximum, crew_required=teams)
                self.assertEqual(len(result["unassigned"]), 7 - sum(expected.values()))
                for route in result["routes"]:
                    team = route["crew_id"]
                    self.assertEqual(len(route["stops"]), expected[team])
                    self.assertEqual(route["unloads"], expected[team])
                    self.assertTrue(all(teams[stop["request_id"]] == team for stop in route["stops"]))
                    self.assertTrue(route["stops"][-1]["unload_after"]["final"])

    def test_automatic_and_explicit_settings_survive_reopening(self):
        with tempfile.TemporaryDirectory(prefix="dg-auto-unloads-") as directory, patch.dict(os.environ, {
            "DG_STATE": directory, "DG_OUT": str(Path(directory) / "out"), "DG_DB_KEY": "synthetic-auto-unloads-key"
        }):
            with closing(store.connect(create=True)) as con:
                self.assertEqual(service.settings(con)["max_unloads"], "auto")
            for value in (0, 2, "auto"):
                with closing(store.connect()) as con:
                    service.save_settings(con, {"max_unloads": value})
                with closing(store.connect()) as con:
                    self.assertEqual(service.settings(con)["max_unloads"], value)


if __name__ == "__main__":
    unittest.main(verbosity=2)
