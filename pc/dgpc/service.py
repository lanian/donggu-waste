"""PC 업무 로직의 겉면(façade): 예전 `from dgpc import service as S` 로 쓰던 이름을 모두 그대로 내보낸다.

실제 코드는 업무 모듈에 있다. 계층(아래 → 위, 위는 아래만 부른다):
  settings(등록표) · store → core → fees → derive → payments·devices·users·auditlog·addresses → area·finance → intake
  → results → privacy·photos → assign → close·reports·backup → config → addrsearch → (service 겉면) → ops → server
새 코드는 필요한 모듈을 직접 불러 써도 된다(아래 import 목록이 곧 모듈 지도). 설정 키는 docs/settings.md, API 경로는 docs/routes.md (둘 다 tools/ 가 만든다).

원칙
- 원본(접수)은 PC에만 있다. 수정은 이전 내용을 버전으로 보존하고 사유를 남긴다. 취소·정정도 지우지 않는다.
- 현장 결과는 이벤트로만 들어오며 같은 이벤트는 한 번만 반영된다(event_id 고유).
- 오래된 배정의 결과, 취소된 건의 결과는 자동 반영하지 않고 충돌함에 넣는다. 기기 시각으로 덮어쓰지 않는다.
- 한 접수는 활성 배정이 하나뿐이다(두 팀이 오프라인으로 같은 건을 처리하지 않게).
"""
# ruff: noqa: F401
from __future__ import annotations

from dgpc.core import (
    KST, _csv_cell, _SafeCsv, safe_csv_writer, ROOT, UserError, now, today_kst, StaleError, _SP, tx, flush_dirty, is_readonly, WRITE_SINK,
    write, _sink, rows, one, ACTOR, chain_hash, _audit_fields, audit, get_setting, set_setting, norm_phone, norm_addr, close_info,
    _after_close, STALE_MSG, check_req_ver, _CHO, chosung_of, is_chosung, _chunks, chosung_key, _sane_ts, _num, _int_clamp, _secs, _date_ok,
    incremental_vacuum, _kst_date, _kst_range, IDEM_HOURS, idempotent, purge_idem, on_dirty,
)
from dgpc.fees import (
    FEE_TABLE, FEE_ROWS, fee_usage, volume_overrides, save_volume_override, _active_revision, fee_state, fee_rows, fee_table_for_phone,
    list_fee_revisions, review_rows, save_fee_draft, approve_fee_revision,
)
from dgpc.derive import (
    FINAL, _events_for, prefetch, _price, derive, derive_pure, refresh_request_summary, ROUND_UNITS, reduction_round_unit, payment_core,
    payment_status,
)
from dgpc.payments import (
    PAY_METHODS, LEGACY_PAY_METHODS, REFUND_PAY_METHODS, _won, add_payment, void_payment,
)
from dgpc.devices import (
    ensure_pc_keys, pc_key, registration_text, device_fingerprint, register_device, confirm_device, device_ready, set_device_status,
    list_devices, save_crew, list_crews, _open_from_device, make_qr_test, record_qr_test, list_qr_tests, crew_request_ids, request_in_crew,
    device_crew,
)
from dgpc.users import (
    ROLES, _pw_hash, _check_pw_rules, users_exist, migrate_legacy_login, create_user, verify_login, check_login, THEMES, FONT_SIZES,
    user_prefs, save_prefs, list_users, set_user_active, set_password, set_user_crew,
)
from dgpc.auditlog import (
    AUDIT_ENTITIES, _AUDIT_HIDE, _audit_where, list_audit, _with_note, audit_page, _audit_detail_text, audit_csv,
)
from dgpc.addresses import (
    set_location, parse_addr_points, import_addr_points, addr_stats, geocode, _addr_std, geocode_missing, _ROAD_RE, _JIBUN_RE,
    match_juso, ADDR_STALE_DAYS,
    address_warnings, jurisdiction_warnings,
)
from dgpc.intake import (
    CHANNELS, CANCEL_CODES, POSTPONE_CODES, CONTACT_KINDS, build_items, validate, _resolve_dong, find_duplicates, _insert_request,
    create_request, _insert_item, _snapshot, locked_lines, update_request, _reason, _cancel, cancel_request, SORTABLE, QUICK, _NOT_FINAL,
    _quick_sql, _enrich, _request_sql, search_requests, list_requests, get_request, list_contacts, add_contact, _ids, _reschedule,
    reschedule_requests, cancel_requests, set_requests_dong, IMPORT_COLS, import_template, _match_fee, parse_import, import_requests,
    link_similar, unlink_similar, notice_text,
)
from dgpc.results import (
    _refresh_status, KNOWN_KINDS, REVIEW_KINDS, CORRECTABLE, CANCELLED_OK_KINDS, prepare_result, import_result, _photo_meta,
    _sanitize_item_event, _apply_revisit, list_conflicts, resolve_conflict, resolve_conflicts_bulk, _strip_request_pii, _field_request,
    _apply_correction, field_intakes, check_field_intake, open_solo_backup, _solo_receipt, _solo_items, _solo_check, prepare_solo,
    import_solo,
)
from dgpc.photos import (
    TYPE_PHOTOS, prepare_photos, import_photos, photo_bytes, purge_candidates, purge_photos, photo_request,
)
from dgpc.assign import (
    confirm_plan, _clean_stop, _write_batch, _release_dropped, current_assignment, ASSIGN_OPS, edit_assignment, list_batches, build_daylist,
    release_info, make_bundle, confirm_receipt, add_unload, list_unloads, load_check, dispatch_sheet,
)
from dgpc.close import (
    CARRY_KIND, carry_candidates, _carry, _post_close_edits, crews_without_result, day_summary, close_day, approve_close,
)
from dgpc.reports import (
    EXPORT_COLS, STATUS_LABEL, PAY_LABEL, _export_rows, _export_line, _export_row, export_requests, print_requests, STATS_BASIS, _basis_date,
    _basis_rows, _last_crew_names, _check_period, stats, stats_csv, PILOT_BASE, PILOT_MISS_KINDS, pilot_settings, save_pilot, pilot_report,
    pilot_csv,
)
from dgpc.backup import (
    backup_dir, make_backup, list_backups, BACKUP_NAME_RE, backup_path, verify_backup,
)
from dgpc.config import (
    PAYMENT_GUIDE_DEFAULT, CONTACT_PLACEHOLDER, settings, save_settings, EXTERNAL_KEYS, TRAVEL_METHODS, secret, travel_method,
    external_settings, _save_external, external_test,
)
from dgpc.addrsearch import (
    _known_names, _dong_for_juso, juso_search, juso_coord, addr_search, coord_available, NO_LOCAL,
)
