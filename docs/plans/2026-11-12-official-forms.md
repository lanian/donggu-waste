# Official Forms and Dong Label Stock Implementation Plan

> Apply writing-plans and test-driven-development in the existing checkout. Preserve prior changes and original Downloads files. Do not commit or push automatically.

**Goal:** 선택한 동의 실제 수거일·수량으로 원본 XLSX 월보와 ODT 일일 대장을 작성해 내려받고, 해당 동의 입고 번호를 자동 배정·차감한다.

**Confirmed choices:** 월보는 동·월, ODT는 동·처리일 기준. 실제 수거일·수거 수량을 사용한다. 입고 번호를 자동 배정하며 재고는 행정동별로 분리한다.

**Architecture:** 반영된 현장 이벤트를 기기·세대·순번으로 재구성하고 최종 결과별 누적 수거량을 날짜별 증가분으로 나눈다. DB migration 10에 동별 재고 연결과 번호별 자동 배정 정보를 추가한다. 수거 결과 반영·충돌 적용·단독 백업 반영·입고/동 배부가 같은 트랜잭션에서 배정을 맞춘다. 출력은 개인정보 내보내기 권한·기록을 적용하며 번호를 새로 차감하지 않는다.

**Tech stack:** Python/SQLCipher, 기존 HTTP/ES module UI, XLSX/ODT XML template packages, local Artifact Tool and native office rendering for validation.

**Status (2026-11-12):** Tasks 1–7 implemented and locally verified. Packaged 0.14.7 and updated `E:\Projects\waste\동구 PC 실행.lnk` with arguments/window style preserved. No commit, push, deployment, production DB access or user app shutdown.

## Behavior and boundaries

- 수거 3개는 번호 3개, 부분 수거 1개는 번호 1개. 재방문은 누적 수량의 증가분만 사용하고 재전송은 중복 차감하지 않는다.
- 동 ID가 지정된 재고에서 작은 번호부터 배정한다. 다른 동·무효·이미 사용한 번호는 제외한다. 수령일/배부일보다 앞선 수거분에 번호를 소급 배정하지 않는다(일자 기준).
- 예전 공용/기관명 재고는 동을 명시적으로 지정할 수 있다. 기존 수동 번호 연결은 존중한다. 현장 추가 품목도 배정할 수 있다.
- 재고 부족은 수거 결과 반영을 막지 않는다. 미배정 장수를 표시하고 추가 입고 뒤 재배정한다. 번호가 필요한 출력에는 부족 상태를 구체적으로 알린다.
- 수량 감소로 초과된 자동 배정 번호는 이력과 함께 무효 처리하고 다시 사용하지 않는다. 수동 번호는 자동 무효 처리하지 않는다. 출력에서 정정 무효 번호와 정상 사용분을 구분한다.
- 실제 수거일별 품목 수량과 저장된 단가·감면 기준의 수거분 금액을 사용한다. 수거분 수수료와 실제 현금 수납을 혼동하지 않도록 해당 표제를 명확히 한다. 미확정 단가는 0원으로 만들지 않는다.
- 월보의 목록/수수료/배출표/월별 명세 구성과 표 모양을 유지한다. 수수료의 끊어진 참조와 잔량의 순환 참조를 복구하고, 출력 데이터 행의 숨김·필터·인쇄 범위를 맞춘다.
- ODT는 결재란·머리글·열 너비·글꼴을 유지한다. 긴 대장은 페이지가 이어져도 행이 잘리지 않고 열 머리글을 반복한다.
- 원본의 샘플 주민·번호·수령 내역 및 작성자 메타데이터는 배포용 템플릿에서 제거한다. 테스트는 가상 주민·임시 상태만 사용한다.

## Tasks

1. **Trace regressions and reducer:** create `tests/test_collection_trace.py`; implement `pc/dgpc/collection.py` for finalized dated increments, late files, corrections, field items and safe timestamps. Retain the existing all-time label count contract.
2. **Migration and allocation:** add migration 10 in `store.py`; implement `pc/dgpc/label_auto.py` and extend `numbered_labels.py` with dong/date inventory input and legacy scope assignment. Test migration preservation, duplicate/idempotent imports, shortage/refill, cross-dong isolation, field items, manual links, corrections and rollback/concurrency.
3. **Wire mutation paths:** result import, solo import, conflict resolution and label stock mutations call the allocator within their transactions. Extend real signed-HTTP tests and existing role/route snapshots.
4. **Prepare clean templates:** inspect and render the original; author the reusable XLSX with Artifact Tool, keep package-native template features required by the user, and sanitize both output templates under `data/report_templates/`. Add a repeatable local preparation/validation script rather than depending on the user's Downloads folder at runtime.
5. **Report service and formats:** create `pc/dgpc/official_forms.py` and archive writers; compute collection-day rows, fee rounding, stock carry-in/receipts/issues/balance, number ranges and missing inputs. Add read-only preview and authorized XLSX/ODT/ZIP downloads with access logs and safe filenames.
6. **PC UI:** add a separate official-form section in reports, and dong/date stock inputs and automatic allocation status in label management. Keep drafts, filters, errors, retry and late-response guards.
7. **Verification and release:** run focused unit/migration/HTTP/browser and template-content/recalculation/render checks, the existing PC/report/label contracts, static/generated/version checks and independent review. Package 0.14.7, verify actual EXE in temporary state, and update the existing shortcut with arguments preserved. Do not stop the user's running app.

## Source evidence

- XLSX has four sheets: 목록, 수수료, 배출표, 1월. The detail has A:P headers and data from row 5. The 수수료 sheet contains broken `#REF!` SUMIF references; detail N4 uses a full-column last-value formula that rendered as a circular reference. Some source detail rows are hidden.
- ODT is landscape A4, with a 6-column approval/title table and an 11-column ledger. Both tables are inside text-box frames; the repeating ledger must flow as a normal table to avoid clipping long output.
- Source workbook ranges were rendered locally with Artifact Tool. Native Excel reference rendering became unresponsive; its owned automation process was verified and closed. Original files remain untouched. Native Word opened the ODT read-only and exported a reference PDF successfully.

## Final local verification

- `tests/test_pc.py`: 688 checks passed. Existing label/report/default-dong suites: 63 unittest cases passed. Both updated dong migration cases passed after replacing obsolete v9 expectations with the current version.
- All nine `test:forms` commands passed using `.venv\Scripts\python.exe`: 49 unit/HTTP/migration/template tests plus browser race and actual source-server workflows. Includes 300 output rows, literal identifiers, invalid XML characters, safe ZIP basenames, stock corrections, scope changes, repeated import/download and logout/relogin boundaries.
- Existing `tests/test_reports_labels_ui.py`: 30 checks passed; existing report/label source E2E and appearance race regressions passed. Generated documents, versions and `git diff --check` passed.
- Artifact Tool imported/recalculated the generated 35-resident workbook: quantity 53, fee 795,000, received 200, issued 53, closing 147; fee-sheet total matched, opening-stock input change recalculated, formula-error scan returned zero. Four sheet previews inspected.
- Native Excel opened the same sample and confirmed totals; monthly/fee/stock PDFs exported. Native Word rendered the ODT in six landscape pages, repeated headers and disabled row splitting; all 35 fictitious resident names remained present. First/last pages inspected. Office automation intermittently returned busy/open failures on repeated helper runs; these were not counted as passing runs. Hangul itself and real resident documents were not tested.
- Actual 0.14.7 EXE passed the new form workflow, existing report/label workflow and desktop bundle with legacy migration/theme checks. These ran with fictitious encrypted state and no Python on the child PATH. Verified exact packaged source/template assets, normal shutdown and existing desktop restart preservation.
- Independent read-only review found and helped fix scope leakage, invalid XML characters, unsafe ZIP paths and delayed downloads after logout; final review reported no remaining Critical/Important findings.
- Local build used Python 3.14.3; CI specifies Python 3.12/Node 22. Full `npm run test:all` and remote CI/deployment were not run.

Release: `dist/desktop/v0.14.7/Donggu-PC/Donggu-PC.exe`; ZIP `Donggu-PC-windows-x64.zip` (66,664,726 bytes), SHA256 `48da3cd763c9bfed41588ecf5743c7aaf430ce2ee1a9e1abbce28360b94e9f5d`.
