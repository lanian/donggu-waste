"""가상 주소·암호화 DB로 본청 및 담당 동 행정동 자동 선택을 확인한다."""
import http.client
import json
import unittest
from urllib.parse import urlencode

from test_pc_session_revocation import SessionHTTPTestCase
from dgpc import addrsearch, area, users


class DongAutoTest(SessionHTTPTestCase):
    def setUp(self):
        super().setUp()
        self.a = area.save_dong(self.con, {"name": "가상가동", "keywords": ["공유로", "단독로"]})
        self.b = area.save_dong(self.con, {"name": "가상나동", "keywords": ["공유로"]})

    def detect(self, path, address, cookie):
        connection = http.client.HTTPConnection("127.0.0.1", self.port, timeout=8)
        try:
            url = "/api/" + path + "?" + urlencode({"address": address})
            connection.request("GET", url, headers={"Cookie": cookie, "X-DG-CSRF": "1"})
            response = connection.getresponse()
            return response.status, json.loads(response.read())
        finally:
            connection.close()

    def point(self, dong, *, road="경계로", sub=0, sigungu="가상구", underground=0):
        self.con.execute("""
            INSERT INTO addr_point (sigungu, road, main, sub, underground, dong_h, lat, lon)
            VALUES (?, ?, 10, ?, ?, ?, 35.15, 126.92)
        """, (sigungu, road, sub, underground, dong))

    def test_scope_filters_global_decision_before_returning_names_or_ids(self):
        users.create_user(self.con, "dong_scope", "가상 담당자", "DONG", "test-dong-scope-123",
                          dong_ids=[self.a])
        cookie = self.login("dong_scope", "test-dong-scope-123")
        path = "dong-work/dong-detect"
        expected = {"dong": "가상가동", "dong_id": self.a, "source": "KEYWORD", "candidates": ["가상가동"]}
        self.assertEqual(self.detect(path, "단독로 10", cookie), (200, expected))
        unresolved = {"dong": None, "dong_id": None, "source": None, "candidates": []}
        self.assertEqual(self.detect(path, "가상나동 10", cookie), (200, {**unresolved, "outside_scope": True}))
        self.assertEqual(self.detect(path, "공유로 10", cookie),
                         (200, {**unresolved, "ambiguous": True, "candidates": ["가상가동"]}))
        self.assertEqual(self.detect(path, "무명길 10", cookie), (200, unresolved))
        self.assertEqual(self.detect("dong-detect", "공유로 10", cookie)[0], 403)
        status, result = self.detect(path, "공유로 10", self.admin_cookie)
        self.assertEqual(status, 200)
        self.assertTrue(result["ambiguous"])
        self.assertCountEqual(result["candidates"], ["가상가동", "가상나동"])

    def test_default_names_and_inactive_dongs(self):
        self.assertEqual(area.detect_dong(self.con, "충장동 1")["dong"], "충장동")
        self.assertIsNone(area.detect_dong(self.con, "계림동 1")["dong"])
        self.assertIsNone(area.detect_dong(self.con, "새충장동길 12 (충장동아파트)")["dong"])
        self.assertIsNone(area.detect_dong(self.con, "무명로 1 (충장동)")["dong"])
        area.save_dong(self.con, {"id": self.b, "name": "가상나동", "active": False})
        self.assertIsNone(area.detect_dong(self.con, "가상나동 1")["dong"])
        self.point("가상나동", road="단독로")
        self.assertIsNone(area.detect_dong(self.con, "가상구 단독로 10")["dong"])

    def test_different_building_or_district_is_not_administrative_evidence(self):
        self.point("가상가동", sub=1)
        self.assertEqual(area.detect_dong(self.con, "가상구 경계로 10-1")["dong"], "가상가동")
        conflict = area.detect_dong(self.con, "가상구 가상나동 경계로 10-1")
        self.assertIsNone(conflict["dong"])
        self.assertCountEqual(conflict["candidates"], ["가상가동", "가상나동"])
        self.con.execute("UPDATE addr_point SET dong_b='가상나동'")
        self.assertEqual(area.detect_dong(self.con, "가상구 가상나동 경계로 10-1")["dong"], "가상가동")
        for address in ("가상구 경계로 10-2", "가상구 경계로 지하 10-1", "다른구 경계로 10-1"):
            with self.subTest(address=address):
                self.assertIsNone(area.detect_dong(self.con, address)["dong"])

    def test_duplicate_building_across_districts_preserves_ambiguity(self):
        self.point("가상가동")
        self.point("가상나동", sigungu="다른구")
        result = area.detect_dong(self.con, "경계로 10")
        self.assertIsNone(result["dong"])
        self.assertTrue(result.get("ambiguous"))
        self.assertCountEqual(result["candidates"], ["가상가동", "가상나동"])
        self.assertEqual(area.detect_dong(self.con, "다른구 경계로 10")["dong"], "가상나동")
        area.save_dong(self.con, {"name": "가상 가동"})
        aliases = area.detect_dong(self.con, "가상구 경계로 10")
        self.assertIsNone(aliases["dong"])
        self.assertCountEqual(aliases["candidates"], ["가상가동", "가상 가동"])

    def test_exact_lot_matches_across_buildings_without_guessing_subnumber(self):
        self.point("가상가동")
        self.point("가상가동", road="모의길")
        self.con.execute("UPDATE addr_point SET dong_b='예시로1가', jibun_main=30, jibun_sub=2")
        self.assertEqual(area.detect_dong(self.con, "가상구 예시로1가 30-2")["dong"], "가상가동")
        for address in ("가상구 예시로1가 30-1", "가상구 예시로1가 30", "가상구 예시로1가 산 30-2"):
            self.assertIsNone(area.detect_dong(self.con, address)["dong"])
        self.con.execute("UPDATE addr_point SET dong_h='가상나동' WHERE road='모의길'")
        result = area.detect_dong(self.con, "가상구 예시로1가 30-2")
        self.assertTrue(result["ambiguous"])
        self.assertCountEqual(result["candidates"], ["가상가동", "가상나동"])

    def test_selected_address_uses_active_administrative_evidence(self):
        self.point("가상가동")
        result = {"road_part1": "가상구 경계로 10", "emdNm": "가상나동", "hemdNm": ""}
        self.assertEqual(addrsearch._dong_for_juso(self.con, result), "가상가동")
        self.assertIsNone(addrsearch._dong_for_juso(self.con, {**result, "road_part1": "무명로 1"}))
        self.assertEqual(addrsearch._dong_for_juso(self.con, {**result, "hemdNm": "가상나동"}), "가상나동")
        area.save_dong(self.con, {"id": self.b, "name": "가상나동", "active": False})
        self.assertIsNone(addrsearch._dong_for_juso(self.con, {**result, "hemdNm": "가상나동"}))
        self.point("가상나동", road="단독로")
        local = addrsearch.addr_search(self.con, "단독로 10")
        self.assertIsNone(local[0]["dong"])


if __name__ == "__main__":
    unittest.main(verbosity=2)
