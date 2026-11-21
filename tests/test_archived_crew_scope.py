"""보관한 수거팀의 과거 조회와 새 업무 연결을 임시 암호화 DB에서 검증한다."""
import unittest

from test_dong_depots import DepotCase
from dgpc import area, assign, core as C, service as S


class ArchivedCrewScopeTests(DepotCase):
    def setUp(self):
        super().setUp()
        self.day = '2024-03-18'
        self.name = '가상 교체 전 팀'
        self.crew = S.save_crew(self.con, {'name': self.name})
        self.batch = S.confirm_plan(self.con, self.day, {self.crew: []})[0]['batch_id']
        self.unload = {'crew_id': self.crew, 'work_date': self.day, 'place': '가상 반입지', 'weight_kg': 100}
        S.add_unload(self.con, self.unload)
        self.old_edit = assign.prepare_edit(self.con, self.day, 'reorder', {'crew_id': self.crew, 'order': []})
        self.assertTrue(S.delete_crew(self.con, self.crew)['archived'])

    def test_settings_and_new_field_work_reject_an_archived_id(self):
        uid = S.create_user(self.con, 'crew_field', '가상 현장', 'FIELD', 'test-crew-password')
        actions = {
            '기본 담당 동': lambda: area.save_dong(self.con, {'name': '가상동', 'crew_id': self.crew}),
            '새 계정': lambda: S.create_user(self.con, 'retired_field', '가상 직원', 'FIELD',
                                            'test-crew-password', crew_id=self.crew),
            '기존 계정': lambda: S.set_user_crew(self.con, uid, self.crew),
            '시범 운영': lambda: S.save_pilot(self.con, {'crew_id': self.crew}),
            'PC 하차': lambda: S.add_unload(self.con, self.unload),
            '단독 휴대폰 반입': lambda: S.import_solo(self.con, b'fixture', '', self.crew,
                                                  opened={'state': {'mode': 'SOLO'}}),
        }
        for name, write in actions.items():
            with self.subTest(name=name), self.assertRaises(C.UserError):
                write()
        self.assertEqual(len(S.list_unloads(self.con, self.day)), 1)
        self.assertIsNone(area.get_dong(self.con, '가상동'))

    def test_stale_edit_as_well_as_new_assignment_cannot_write_new_batch(self):
        change = {'crew_id': self.crew, 'order': []}
        for write in (
            lambda: S.confirm_plan(self.con, self.day, {self.crew: []}),
            lambda: assign.edit_assignment(self.con, self.day, 'reorder', change),
            lambda: assign.apply_edit(self.con, self.day, 'reorder', change, self.old_edit, self.old_edit['changes']),
        ):
            with self.subTest(write=write), self.assertRaises(C.UserError):
                write()
        self.assertEqual(self.con.execute('SELECT count(*) FROM assignment_batch WHERE crew_id=?',
                                          (self.crew,)).fetchone()[0], 1)

    def test_history_still_includes_the_archived_team_name(self):
        self.assertNotIn(self.crew, {crew['id'] for crew in S.list_crews(self.con)})
        self.assertEqual(S.list_unloads(self.con, self.day)[0]['crew_name'], self.name)
        self.assertEqual(S.list_batches(self.con, self.day, route_map=True)[0]['crew_name'], self.name)
        self.assertTrue(S.list_batches(self.con, self.day, for_send=True)[0].get('crew_archived'))
        self.assertEqual(S.dispatch_sheet(self.con, self.batch)['crew'], self.name)
        self.assertEqual(S.pilot_report(self.con, self.day, self.day, self.crew)['crew'], self.name)


if __name__ == '__main__':
    unittest.main()
