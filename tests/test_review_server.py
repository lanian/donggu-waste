"""전체 검토 회귀시험: 암호화 백업 메타 정보와 로그인 중 계정 변경.

실행: python tests/test_review_server.py (실제 SQLCipher, 가상 자료·임시 폴더만 사용)
"""
from __future__ import annotations

import hashlib
import os
import queue
import sys
import tempfile
import threading
from contextlib import contextmanager
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "pc"))

from dgpc import backup as BK, server as SV, service as S, store  # noqa: E402

passed = 0
PASSWORD = "review-backup-pass-01"


def ok(condition, message):
    global passed
    if not condition:
        raise AssertionError("실패: " + message)
    passed += 1
    print("  ✓", message)


@contextmanager
def test_state():
    """다른 시험의 작업 폴더·키를 보존하며 독립된 실제 암호화 DB 사용."""
    keys = ("DG_STATE", "DG_DB_KEY")
    old = {k: os.environ.get(k) for k in keys}
    with tempfile.TemporaryDirectory(prefix="dg-review-server-") as directory:
        os.environ.update(DG_STATE=directory, DG_DB_KEY="review-db-key-not-for-production")
        try:
            yield Path(directory)
        finally:
            for k, value in old.items():
                if value is None:
                    os.environ.pop(k, None)
                else:
                    os.environ[k] = value


def metadata(con):
    return tuple(con.execute(f"PRAGMA {key}").fetchone()[0] for key in ("user_version", "auto_vacuum"))


def backup_metadata():
    with test_state() as directory:
        con = store.connect(create=True)
        try:
            ok(store.ENCRYPTED and metadata(con) == (store.LATEST, 2), "실제 SQLCipher 원본의 구조 버전·점진적 공간 반환 확인")
            dest = store.backup_to(con, directory / "manual.dgbak", PASSWORD)
            saved, _ = store.open_backup(dest, PASSWORD)
            try:
                actual = metadata(saved)
                ok(actual == metadata(con), f"수동 백업이 구조 버전·공간 반환 설정 보존: {actual}")
            finally:
                saved.close()
            pem, _ = BK.generate_key(con, PASSWORD)
            result = BK.make_auto(con, "TEST")
            ok(result["ok"], "자동 백업 생성")
            decoded = directory / "auto-decoded.db"
            secret = BK.decrypt_to((BK.backup_dir(con) / result["file"]).read_bytes(), pem, PASSWORD, decoded)
            saved, _ = store.open_backup(decoded, secret)
            try:
                ok(metadata(saved) == metadata(con), "자동 백업도 구조 버전·공간 반환 설정 보존")
            finally:
                saved.close()
        finally:
            con.close()


def restore_metadata():
    with test_state() as directory:
        con = store.connect(create=True)
        try:
            dest = store.backup_to(con, directory / "restore.dgbak", PASSWORD)
        finally:
            con.close()
        # 복원 자체의 메타 보존을 별도로 검사(이전 구현이 만든 백업도 입력으로 구성 가능).
        saved, _ = store.open_backup(dest, PASSWORD)
        try:
            saved.execute(f"PRAGMA user_version = {store.LATEST}")
            saved.execute("PRAGMA auto_vacuum = INCREMENTAL")
            saved.execute("VACUUM")
        finally:
            saved.close()
        result = store.restore_from(dest, PASSWORD)
        restored = store.dbapi.connect(str(directory / "pc.db"))
        try:
            restored.execute("PRAGMA key = " + store._quote(os.environ["DG_DB_KEY"]))
            actual = metadata(restored)
            ok(actual == (store.LATEST, 2), f"복원 DB를 마이그레이션 없이 열어도 메타 정보 보존: {actual}")
            ok(result["kept"] and Path(result["kept"]).exists(), "복원 전 DB 사본 보존")
        finally:
            restored.close()
        con = store.connect()
        try:
            ok(not store.pre_migration_info(directory)["count"], "같은 구조 버전 복원 뒤 불필요한 마이그레이션 사본 없음")
        finally:
            con.close()


def restore_future_schema():
    with test_state() as directory:
        con = store.connect(create=True)
        try:
            dest = store.backup_to(con, directory / "future.dgbak", PASSWORD)
        finally:
            con.close()
        saved, _ = store.open_backup(dest, PASSWORD)
        try:
            saved.execute(f"PRAGMA user_version = {store.LATEST + 1}")
        finally:
            saved.close()
        before = hashlib.sha256((directory / "pc.db").read_bytes()).digest()
        try:
            store.restore_from(dest, PASSWORD)
        except ValueError as error:
            ok("버전" in str(error), "더 새 구조의 백업은 복원 전에 버전 오류로 거부")
        else:
            raise AssertionError("실패: 더 새 구조의 백업을 현재 DB로 복원함")
        ok(hashlib.sha256((directory / "pc.db").read_bytes()).digest() == before
           and not list(directory.glob("pc.db.before-restore-*")) and not (directory / "pc.db.restoring").exists(),
           "더 새 백업 복원 거부 시 원래 DB와 파일 상태 보존")


class FailAfterExport:
    """실제 SQLCipher 내보내기가 끝난 직후 오류를 내어 롤백·파일 정리를 검사한다."""
    def __init__(self, con):
        self.con = con
        self.in_export_transaction = False

    def __getattr__(self, name):
        return getattr(self.con, name)

    def execute(self, sql, *args):
        result = self.con.execute(sql, *args)
        if "sqlcipher_export" in sql:
            self.in_export_transaction = self.con.in_transaction
            raise OSError("가상 내보내기 실패")
        return result


def export_rollback():
    with test_state() as directory:
        con = store.connect(create=True)
        try:
            con.execute("INSERT INTO meta(key, value) VALUES('review_export_marker', 'preserved')")
            failing = FailAfterExport(con)
            try:
                store.backup_to(failing, directory / "failed.dgbak", PASSWORD)
            except OSError:
                pass
            else:
                raise AssertionError("실패: 내보내기 오류 주입이 실패하지 않음")
            ok(failing.in_export_transaction, "내보내기는 명시 트랜잭션 안에서 실행(자동 커밋 반복 방지)")
            ok(not con.in_transaction and {r[1] for r in con.execute("PRAGMA database_list")} == {"main", "temp"},
               "내보내기 실패 뒤 롤백·첨부 DB 분리 완료")
            ok(not (directory / "failed.dgbak").exists() and not list(directory.glob("*.part*")),
               "내보내기 실패 뒤 정식 백업·임시 파일 없음")
            ok(con.execute("SELECT value FROM meta WHERE key='review_export_marker'").fetchone()[0] == "preserved"
               and metadata(con) == (store.LATEST, 2), "내보내기 오류가 원본 DB 자료·메타 정보를 바꾸지 않음")
            con.execute("BEGIN")
            con.execute("INSERT INTO meta(key, value) VALUES('review_reuse', '1')")
            con.execute("ROLLBACK")
            ok(not con.in_transaction and con.execute("SELECT 1 FROM meta WHERE key='review_reuse'").fetchone() is None,
               "내보내기 실패 뒤 같은 연결로 새 업무 트랜잭션 사용 가능")
        finally:
            con.close()


def export_caller_transaction():
    with test_state() as directory:
        con = store.connect(create=True)
        try:
            for alias in ("bk_good", "bk_fail"):
                con.execute(f"ATTACH DATABASE {store._quote(str(directory / (alias + '.db')))} AS {alias} KEY {store._quote(PASSWORD)}")
            con.execute("BEGIN")
            con.execute("INSERT INTO meta(key, value) VALUES('review_outer', 'not-committed')")
            store._export_sqlcipher(con, "bk_good")
            ok(con.in_transaction, "내보내기 성공이 호출자의 기존 트랜잭션을 커밋하지 않음")
            try:
                store._export_sqlcipher(FailAfterExport(con), "bk_fail")
            except OSError:
                pass
            else:
                raise AssertionError("실패: 중첩 내보내기 오류 주입이 실패하지 않음")
            ok(con.in_transaction and con.execute("SELECT value FROM meta WHERE key='review_outer'").fetchone()[0] == "not-committed",
               "내보내기 실패는 자기 저장점만 되돌리고 호출자 변경 보존")
            con.execute("ROLLBACK")
            ok(con.execute("SELECT 1 FROM meta WHERE key='review_outer'").fetchone() is None,
               "호출자가 내보내기 이후 자기 트랜잭션을 직접 되돌릴 수 있음")
        finally:
            con.close()


def login_account_change(change):
    """실제 암호 검증 직후 계정을 바꾸고 로그인 확정까지 이어가는 경쟁을 Event로 고정."""
    with test_state():
        con = store.connect(create=True)
        con.execute("INSERT INTO crew (id, name) VALUES ('crew-a', '가상A팀'), ('crew-b', '가상B팀')")
        uid = S.create_user(con, "review_field", "가상수거", "FIELD", "review-login-pass-01", crew_id="crew-a")
        names = ("CON", "LAN", "RPOOL", "SESSIONS", "FAILS", "IP_FAILS", "FAIL_AGG", "LOGIN_INFLIGHT")
        old = {name: getattr(SV, name) for name in names}
        SV.CON, SV.LAN, SV.RPOOL = con, None, queue.LifoQueue()
        SV.SESSIONS, SV.FAILS, SV.IP_FAILS, SV.FAIL_AGG, SV.LOGIN_INFLIGHT = {}, {}, {}, {}, set()
        real_verify = S.verify_login
        verified, resume = threading.Event(), threading.Event()
        responses, errors = [], []
        handler = object.__new__(SV.Handler)
        handler.client_address = ("127.0.0.1", 0)
        handler.json_body = lambda: {"username": "review_field", "password": "review-login-pass-01"}
        handler.send_json = lambda body, status=200, extra=None: responses.append((body, status, extra))

        def paused_verify(*args, **kwargs):
            user = real_verify(*args, **kwargs)
            verified.set()
            if not resume.wait(10):
                raise AssertionError("계정 변경 후 로그인 재개 신호 없음")
            return user

        def login():
            try:
                handler.login()
            except Exception as error:
                errors.append(error)

        S.verify_login = paused_verify
        thread = threading.Thread(target=login)
        try:
            thread.start()
            if not verified.wait(10):
                raise AssertionError("로그인 암호 검증 신호 없음")
            # 이 시점에 scrypt는 끝났지만 DB 잠금은 잡지 않아 관리자 작업을 실행할 수 있다.
            acquired = SV.LOCK.acquire(timeout=5)
            ok(acquired, f"{change}: 암호 검증 동안 DB 잠금 사용 가능")
            if not acquired:
                return
            try:
                if change == "중지":
                    S.set_user_active(con, uid, False, "다른관리자")
                elif change == "암호 초기화":
                    S.set_password(con, uid, "changed-login-pass-02", must_change=True)
                else:
                    S.set_user_crew(con, uid, "crew-b")
            finally:
                SV.LOCK.release()
            resume.set()
            thread.join(10)
            ok(not thread.is_alive(), f"{change}: 로그인 작업 완료")
            if change in ("중지", "암호 초기화"):
                ok(len(errors) == 1 and isinstance(errors[0], S.UserError) and not responses and not SV.SESSIONS,
                   f"{change}: 이전 암호 검증 결과로 새 세션을 만들지 않음")
            else:
                ok(not errors and len(responses) == 1 and len(SV.SESSIONS) == 1, "팀 변경 뒤 유효한 암호 로그인 성공")
                session = next(iter(SV.SESSIONS.values()))
                ok(session["crew_id"] == "crew-b", "로그인 확정 때 현재 팀을 읽어 이전 팀 접근 범위를 남기지 않음")
                allowed = {"exp", "created", "user_id", "username", "role", "display_name", "must_change", "crew_id"}
                ok(set(session) == allowed and responses[0][0] == {"ok": True}, "세션·API 응답에 암호 해시나 검증 증거를 포함하지 않음")
        finally:
            resume.set()
            thread.join(10)
            S.verify_login = real_verify
            while not SV.RPOOL.empty():
                SV.RPOOL.get_nowait().close()
            for name, value in old.items():
                setattr(SV, name, value)
            con.close()


def main():
    backup_metadata()
    restore_metadata()
    restore_future_schema()
    export_rollback()
    export_caller_transaction()
    for change in ("중지", "암호 초기화", "팀 변경"):
        login_account_change(change)
    print(f"\n서버 검토 회귀시험 통과 {passed}개")


if __name__ == "__main__":
    main()
