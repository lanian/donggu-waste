"""센터 차고지: 임시 암호화 DB에서 기존 설정 보존과 일괄 적용을 확인한다."""
import unittest
from unittest.mock import patch
import test_dong_depots as fixture
from dgpc import area as A, core as C, service as S
from dgpc import dong_centers as DC
from dgpc.dong_defaults import DONGGU_DONGS


class CenterDepotTests(fixture.DepotCase):
    def setUp(self):
        super().setUp()
        self.crew = S.save_crew(self.con, {'name': '가상 충장동 팀'})
        self.other = S.save_crew(self.con, {'name': '가상 동명동 팀'})

    def dong(self, name, crew=None, **extra):
        old = A.get_dong(self.con, name)
        return A.save_dong(self.con, {'id': old['id'] if old else None, 'name': name, 'crew_id': crew, **extra})

    def test_complete_official_reference_and_new_locations(self):
        self.assertEqual(tuple(row['dong'] for row in DC.centers()), DONGGU_DONGS)
        self.assertIn('동계천로 172', DC.center_for('동명동')['address'])
        self.assertIn('남문로753번길 20', DC.center_for('학동')['address'])
        for row in DC.centers():
            self.assertTrue(35.10 < row['lat'] < 35.20 and 126.90 < row['lon'] < 126.95)
            self.assertTrue(row['source'].startswith('https://www.donggu.kr/'))
        self.assertIsNone(DC.center_for('가상동'))

    def test_only_missing_ready_depots_are_filled_and_other_fields_are_preserved(self):
        did = self.dong('충장동', self.crew, code='CJ', weekdays=[0, 2], daily_limit=7, keywords=['가상길'])
        before = C.one(self.con, 'SELECT * FROM dong WHERE id=?', (did,))
        result = DC.apply_missing(self.con)
        self.assertEqual(result['applied'], 1)
        after = C.one(self.con, 'SELECT * FROM dong WHERE id=?', (did,))
        for field in before:
            if field not in ('depot_json', 'updated_at'):
                self.assertEqual(before[field], after[field], field)
        center = DC.center_for('충장동')
        self.assertEqual(A.get_dong(self.con, '충장동')['depot'], {key: center[key] for key in ('label', 'lat', 'lon')})

    def test_custom_unmapped_inactive_and_shared_teams_are_preserved(self):
        custom = {'label': '가상 별도 차고지', 'lat': 35.14, 'lon': 126.92}
        self.dong('충장동', self.crew, depot=custom)
        self.dong('동명동', self.other, active=False)
        self.dong('산수1동')
        shared = S.save_crew(self.con, {'name': '가상 중복 연결 팀'})
        self.dong('계림1동', shared)
        self.dong('계림2동', shared)
        result = DC.apply_missing(self.con)
        self.assertEqual(result['applied'], 0)
        self.assertEqual(A.get_dong(self.con, '충장동')['depot'], custom)
        for name in ('동명동', '산수1동', '계림1동', '계림2동'):
            self.assertIsNone(A.get_dong(self.con, name)['depot'])
        self.assertTrue(any('중복' in row['reason'] or '여러' in row['reason'] for row in result['skipped']))

    def test_repeat_is_idempotent_and_metadata_does_not_activate_a_depot(self):
        self.dong('충장동', self.crew)
        row = A.get_dong(self.con, '충장동')
        self.assertEqual(row['center_depot']['label'], '충장동 행정복지센터')
        self.assertIsNone(row['depot'])
        self.assertEqual(DC.apply_missing(self.con)['applied'], 1)
        count = C.one(self.con, 'SELECT COUNT(*) AS n FROM audit_log')['n']
        self.assertEqual(DC.apply_missing(self.con)['applied'], 0)
        self.assertEqual(C.one(self.con, 'SELECT COUNT(*) AS n FROM audit_log')['n'], count)

    def test_inactive_crew_is_not_applied(self):
        self.dong('학운동', self.other)
        S.save_crew(self.con, {'id': self.other, 'name': '가상 동명동 팀', 'active': False})
        result = DC.apply_missing(self.con)
        self.assertIsNone(A.get_dong(self.con, '학운동')['depot'])
        self.assertIn('담당팀 사용 중지', [row['reason'] for row in result['skipped']])

    def test_failure_rolls_back_the_entire_fill_and_audit(self):
        self.dong('충장동', self.crew)
        self.dong('동명동', self.other)
        before = C.one(self.con, 'SELECT COUNT(*) AS n FROM audit_log')['n']
        original, calls = C.audit, []
        def fail_second(*args, **kwargs):
            calls.append(1)
            if len(calls) == 2:
                raise RuntimeError('가상 두 번째 저장 실패')
            return original(*args, **kwargs)
        with patch.object(C, 'audit', side_effect=fail_second), self.assertRaises(RuntimeError):
            DC.apply_missing(self.con)
        self.assertIsNone(A.get_dong(self.con, '충장동')['depot'])
        self.assertIsNone(A.get_dong(self.con, '동명동')['depot'])
        self.assertEqual(C.one(self.con, 'SELECT COUNT(*) AS n FROM audit_log')['n'], before)


if __name__ == '__main__':
    unittest.main()
