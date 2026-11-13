"""동별 전체 기간 조회의 날짜/상태/현재 담당 동 경계 회귀."""
import unittest
import test_label_usage_api as fixture
from dgpc import area, core as C, service as S


class DateVisibilityAPI(fixture.UsageHTTPCase):
    def setUp(self):
        super().setUp()
        self.records = []
        for day, status in [('2026-01-02', 'DONE'), (C.today_kst(), 'RECEIVED'), (None, 'RECEIVED'), ('2026-01-02', 'CANCELLED')]:
            r, _ = self.request()
            self.con.execute('UPDATE request SET planned_date=?,status=? WHERE id=?', (day, status, r['id']))
            self.records.append(r)
        self.other, _ = self.request(dong='동명동')

    def test_all_period_includes_old_done_cancelled_and_undated_without_other_dong(self):
        dong = area.get_dong(self.con, '충장동')['id']
        S.create_user(self.con, 'date_dong', '가상 날짜 담당자', 'DONG', 'date-dong-test-123', dong_ids=[dong])
        cookie = self.login('date_dong', 'date-dong-test-123')
        status, result = self.get('dong-work/requests?bucket=all&date=' + C.today_kst(), cookie)
        self.assertEqual(status, 200, result)
        self.assertEqual({r['id'] for r in result['rows']}, {r['id'] for r in self.records})
        status, result = self.get('dong-work/requests?bucket=day&date=' + C.today_kst(), cookie)
        self.assertEqual((status, result['total']), (200, 1))
        other = area.get_dong(self.con, '동명동')['id']
        self.assertEqual(self.get('dong-work/requests?bucket=all&dong=' + other, cookie)[0], 403)

    def test_all_period_text_search_finds_old_receipt_with_day_metric_unchanged(self):
        receipt = self.records[0]['receipt_no']
        status, result = self.get('dong-work/requests?bucket=all&q=' + receipt, self.admin_cookie)
        self.assertEqual(status, 200, result)
        self.assertEqual([r['id'] for r in result['rows']], [self.records[0]['id']])
        status, overview = self.get('dong-work?date=' + C.today_kst(), self.admin_cookie)
        self.assertEqual((status, overview['summary']['done']), (200, 0))


if __name__ == '__main__':
    unittest.main()
