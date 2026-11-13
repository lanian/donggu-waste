"""동별 차고지 저장·담당팀·이전·범위: 가상 자료와 임시 SQLCipher만 사용."""
import json
import unittest
from unittest.mock import patch
from pathlib import Path
import test_pc_labels as fixture
from dgpc import area as A, core as C, service as S, store


class DepotCase(unittest.TestCase):
    setUp = fixture.NumberedLabelsTest.setUp
    tearDown = fixture.NumberedLabelsTest.tearDown


class DongDepotTests(DepotCase):
    def setUp(self):
        super().setUp()
        self.crew = S.save_crew(self.con, {'name': '가상 동전담1팀'})
        self.other = S.save_crew(self.con, {'name': '가상 동전담2팀'})
        self.p1 = {'lat': 35.15, 'lon': 126.91, 'label': '가상 A 차고지'}
        self.p2 = {'lat': 35.19, 'lon': 126.95, 'label': '가상 B 차고지'}

    def dong(self, name='가상A동', **data):
        return A.save_dong(self.con, {'name': name, 'crew_id': self.crew, **data})

    def test_point_is_saved_preserved_on_partial_save_and_cleared_explicitly(self):
        did = self.dong(depot=self.p1)
        self.assertEqual(A.get_dong(self.con, '가상A동').get('depot'), self.p1)
        self.dong(id=did, sort=8)
        self.assertEqual(A.get_dong(self.con, '가상A동')['depot'], self.p1)
        self.dong(id=did, depot=None)
        self.assertIsNone(A.get_dong(self.con, '가상A동')['depot'])

    def test_bad_coordinates_and_missing_crew_are_rejected_atomically(self):
        for point in ({'lat': 'bad', 'lon': 126.91}, {'lat': 35.1}, {'lat': True, 'lon': 126.9},
                      {'lat': float('nan'), 'lon': 126.9}, {'lat': 0, 'lon': 0}, {'lat': 35.1, 'lon': float('inf')}):
            with self.subTest(point=point), self.assertRaises(C.UserError):
                self.dong(depot=point)
        with self.assertRaises(C.UserError):
            self.dong(depot=self.p1, crew_id=None)
        self.assertIsNone(A.get_dong(self.con, '가상A동'))

    def test_one_crew_cannot_have_two_active_dong_depots(self):
        self.dong(depot=self.p1)
        with self.assertRaises(C.UserError):
            self.dong('가상B동', depot=self.p2)
        self.dong('가상B동', depot=self.p1)
        self.dong('가상C동', depot=self.p2, crew_id=self.other)

    def test_context_uses_dong_team_and_legacy_common_fallback(self):
        self.dong(depot=self.p1)
        from dgpc import route_depots as D
        crews = S.list_crews(self.con)
        reqs = [{'id': 'a', 'dong': '가상A동'}, {'id': 'old', 'dong': '동명동'}]
        result = D.resolve(reqs, crews, A.list_dongs(self.con), S.settings(self.con)['depot'])
        self.assertEqual(result['crew_depots'][self.crew], self.p1)
        self.assertEqual(result['crew_depots'][self.other], S.settings(self.con)['depot'])
        self.assertEqual(result['crew_required'], {'a': self.crew})


class DepotMigrationTests(DepotCase):
    def old(self):
        self.con.close()
        # This fixture's DB is disposable: recreate as an actual v10 database at a separate path.
        import os
        self.env_old_state = os.environ['DG_STATE']
        self.old_state = Path(self.directory.name) / 'v10'
        os.environ['DG_STATE'] = str(self.old_state)
        with patch.object(store, 'LATEST', 10), patch.object(store, 'MIGRATIONS', [m for m in store.MIGRATIONS if m[0] <= 10]):
            self.con = store.connect(create=True)
            A.save_dong(self.con, {'name': '가상 이전동', 'sort': 77, 'keywords': ['가상 보존로']})
            self.before = self.con.execute('SELECT * FROM dong ORDER BY id').fetchall()
        self.con.close()

    def tearDown(self):
        if hasattr(self, 'env_old_state'):
            import os
            os.environ['DG_STATE'] = self.env_old_state
        super().tearDown()

    def test_v10_migrates_without_replacing_any_existing_dong_fields(self):
        self.old()
        self.con = store.connect()
        self.assertGreaterEqual(store.LATEST, 11)
        self.assertEqual([row[:-1] for row in self.con.execute('SELECT * FROM dong ORDER BY id')], self.before)
        self.assertTrue(all(row[-1] is None for row in self.con.execute('SELECT * FROM dong')))

    def test_failed_step_eleven_restores_v10(self):
        self.old()
        with patch.object(store, '_TEST_FAIL_STEP', 11), self.assertRaises(RuntimeError):
            store.connect()
        self.con = store.connect(readonly=True)
        self.assertEqual(self.con.execute('PRAGMA user_version').fetchone()[0], 10)
        self.assertEqual(self.con.execute('SELECT * FROM dong ORDER BY id').fetchall(), self.before)


if __name__ == '__main__':
    unittest.main()
