"""원본 서식의 실제 HTTP 다운로드·권한·접속기록·재고 불변 계약."""
import http.client
from io import BytesIO
import json
import unittest
from urllib.parse import quote
from zipfile import ZipFile

from test_label_usage_api import UsageHTTPCase
from dgpc import area, core as C, server as SV


class OfficialFormsAPITests(UsageHTTPCase):
    def prepare(self, stock=True):
        self.dong = area.get_dong(self.con, '충장동')['id']
        if stock:
            self.assertEqual(self.post('numbered-labels/receive', {'start': '0901', 'end': '0905', 'dong_id': self.dong, 'received_on': C.today_kst()}, self.admin_cookie)[0], 200)
        request, line = self.request()
        seal, event = self.phone(request)
        events = [event('ITEM_RESULT', {'line_id': line, 'qty_confirmed': 3, 'qty_collected': 2}), event('ATTEMPT', {'outcome': 'PARTIAL'})]
        self.assertEqual(self.post('import/result', seal(events), self.admin_cookie)[0], 200)
        return request

    def download(self, kind, cookie=None, **criteria):
        connection = http.client.HTTPConnection('127.0.0.1', self.port, timeout=20)
        try:
            body = {'dong_id': self.dong, 'date': C.today_kst(), 'format': kind, **criteria}
            connection.request('POST', '/api/reports/forms/download', json.dumps(body).encode(), {'Cookie': cookie or '', 'X-DG-CSRF': '1', 'Content-Type': 'application/json'})
            response = connection.getresponse()
            return response.status, response.read(), dict(response.getheaders())
        finally:
            connection.close()

    def test_preview_and_native_files_log_export_without_consuming_again(self):
        self.prepare()
        status, preview = self.get(f'reports/forms?dong_id={quote(self.dong)}&date={C.today_kst()}', self.admin_cookie)
        self.assertEqual(status, 200, preview)
        self.assertEqual(preview['monthly']['qty'], 2)
        self.assertTrue(preview['daily']['ready'])
        self.assertNotIn('가상비공개', json.dumps(preview, ensure_ascii=False))
        before = {table: self.con.execute('SELECT * FROM ' + table).fetchall() for table in ('numbered_label', 'numbered_label_allocation', 'numbered_label_event')}
        for kind, mime in (('xlsx', 'application/vnd.openxmlformats-officedocument.spreadsheetml.sheet'), ('odt', 'application/vnd.oasis.opendocument.text'), ('zip', 'application/zip'), ('xlsx', 'application/vnd.openxmlformats-officedocument.spreadsheetml.sheet')):
            status, raw, headers = self.download(kind, self.admin_cookie)
            self.assertEqual(status, 200, raw[:200])
            self.assertEqual(headers['Content-Type'], mime)
            self.assertIn("filename*=UTF-8''", headers['Content-Disposition'])
            with ZipFile(BytesIO(raw)) as package:
                if kind == 'zip':
                    self.assertEqual(sorted(name.rsplit('.', 1)[-1] for name in package.namelist()), ['odt', 'xlsx'])
                else:
                    self.assertIn('xl/workbook.xml' if kind == 'xlsx' else 'content.xml', package.namelist())
        self.assertEqual(before, {table: self.con.execute('SELECT * FROM ' + table).fetchall() for table in before})
        logs = C.rows(self.con, "SELECT * FROM access_log WHERE action='EXPORT'")
        self.assertEqual(len(logs), 4)
        self.assertTrue(all(row['count'] == 1 for row in logs))
        self.assertNotIn('가상비공개', json.dumps(logs, ensure_ascii=False))

    def test_shortage_invalid_input_and_role_denial_return_no_file(self):
        self.prepare(stock=False)
        self.assertEqual(self.download('xlsx', self.admin_cookie)[0], 400)
        self.assertEqual(self.download('wrong', self.admin_cookie)[0], 400)
        self.assertEqual(self.download('xlsx', self.admin_cookie, date='2026-02-31')[0], 400)
        self.assertEqual(self.download('odt')[0], 401)
        with C.tx(self.con):
            self.con.execute("UPDATE app_user SET role='LEAD' WHERE id=?", (self.worker_id,))
        self.assertEqual(self.download('odt', self.worker_cookie)[0], 403)
        self.assertEqual(self.con.execute("SELECT COUNT(*) FROM access_log WHERE action='EXPORT'").fetchone()[0], 0)
        for role in ('ADMIN', 'INTAKE', 'LEAD', 'FIELD', 'DONG'):
            self.assertEqual(SV.allowed(role, 'POST', 'reports/forms/download'), role in ('ADMIN', 'INTAKE'))

    def test_zip_entries_and_download_names_are_safe_basenames(self):
        dong = area.save_dong(self.con, {'name': '../../../가상동\\파일:이름\n', 'sort': 100})
        self.dong = dong
        status, raw, headers = self.download('zip', self.admin_cookie)
        self.assertEqual(status, 200, raw[:200])
        with ZipFile(BytesIO(raw)) as package:
            for name in package.namelist():
                for invalid in ('/', '\\', ':', '\n', '\r', '..'):
                    self.assertNotIn(invalid, name)
        self.assertNotIn('%2F', headers['Content-Disposition'])
        self.assertNotIn('%5C', headers['Content-Disposition'])


if __name__ == '__main__':
    unittest.main()
