# 모바일 현장접수 품목 추가 Implementation Plan

> **For Claude:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this plan task-by-task.

**Goal:** 현장접수에서 검색·품목 선택·수량 입력을 함께 개선한다. 사용자가 이 범위를 선택했다.

**Architecture:** 접수 등록과 현장 품목 추가가 공유하는 `pickScreen`을 개선한다. 기존 수수료표 검색·동의어·품목 ID·행 ID·단가·확인 대기·저장 재시도 계약은 유지하고, 화면의 탐색과 표시만 보강한다.

**Tech Stack:** Vanilla JavaScript ES modules, 기존 CSS 변수, Playwright, 실제 PIN/IndexedDB와 가상 접수.

---

## 확인한 문제와 설계

- 첫 사용의 자주 쓴 목록이 비어 있다. 소파·매트리스 등 알아보기 쉬운 이름으로 빠른 선택을 제공하고 공식 품목명도 함께 표시한다.
- `rows.map(row)`가 배열 인덱스를 `specOnly` 인자로 넘겨 두 번째 결과부터 품목명을 숨긴다. 검색·자주 쓴 결과는 항상 이름과 규격을 함께 표시한다.
- 검색 결과 수와 40개 초과 결과의 더 보기를 제공한다. 검색 결과가 없으면 검색어를 직접 입력란으로 옮길 수 있게 한다.
- 상단에서 담은 품목을 바로 확인하고 원래 검색으로 돌아온다. 수량 직접 입력, 행별 금액, 품목 삭제를 제공하고 현장 기록으로 잠긴 행은 보존한다.
- 품목 선택기에 진입하거나 검색어를 바꾸면 첫 결과가 보이도록 스크롤을 맞춘다. 기존 접수의 스크롤이 남아 첫 안내와 결과를 가리던 문제를 확인했다.
- 수량은 실제 부과 단위로 요약한다. 확인 대기 금액을 확정 합계나 무료로 표시하지 않는다.
- 기존 화면의 녹색 선택 상태, 터치 크기, 밝게/어둡게·큰 글자·짧은 화면을 유지한다. 기존 동별 기본팀 변경은 별도 작업으로 보존한다.

## Task 1: 변경 전 회귀

- Create: `tests/test_mobile_intake_picker.py`
- 빠른 선택, 모든 검색 결과의 이름, 검색 더 보기, 담은 품목과 검색 복귀, 확인 대기·혼합 단위, 실제 초안/저장, 화면 크기를 검증한다.
- Run: `.venv/Scripts/python.exe tests/test_mobile_intake_picker.py`
- 변경 전 실패를 확인한 뒤 구현한다.

## Task 2: 화면 개선

- Modify: `web/widgets.js`, `web/style.css`, `web/screens/intake.js`
- 공유 품목 선택기와 그 전용 CSS만 변경한다. 저장·수수료 계산·암호화·전송 형식은 바꾸지 않는다.
- Run: 새 회귀, `test_mobile_intake_save.py`, `test_mobile_add_save.py`, `test_mobile_layout.py`, `test_mobile_field_layout.py`, `tests/e2e.py` 중 변경 경로 검사를 실행한다.
- 화면 캡처는 `shots/mobile-intake-picker/`에 보관하고 직접 확인한다. 소스 검증과 실제 휴대폰/PWA 배포를 구분한다.
- Modify: `package.json`, `.github/workflows/ci.yml`
- 새 회귀를 모바일 전체 시험과 CI 모바일 행렬에 연결한다.

## 검증 결과

- 변경 전 새 회귀 5개 시나리오가 실패했다. 검색 두 번째 결과의 품목명 누락도 실제 브라우저에서 재현했다. 개선 후 전체 시나리오가 통과했다.
- 새 검사에서 빠른 선택·공식 이름·규격·초성·검색 건수·40개 초과 결과·담은 목록·150개 직접 입력·행 ID/금액/초점·검색 복귀·삭제를 확인했다. PC 연결/단독 각각 실제 접수 저장 후 새로고침·PIN 재진입으로 수수료표 품목과 암호화 저장 내용 일치를 확인했다.
- 미등록 품목은 확인 대기로 유지하고 확정 소계와 분리했다. 개/쪽/20ℓ당 표시를 검증했고, `120ℓ당`으로 붙어 읽히던 표시를 `20ℓ당 1`로 바로잡았다.
- 접수→품목 화면의 이전 스크롤이 첫 안내를 가리는 실패를 재현·수정했다. 360/390/430px, 보통/큰 글자, 밝게/어둡게, 높이 400px에서 수량·삭제·완료 버튼의 실제 좌표 접근을 확인했다. 최대 수량·4개 부과 단위·긴 금액의 하단 요약 높이 129px에 기존 여유가 110px뿐인 경우도 재현·수정했다.
- 기존 `test_mobile_intake_save.py`, `test_mobile_add_save.py`, `test_mobile_field_layout.py --case picker/aggregate`, `test_mobile_layout.py --case picker/auto-hc`가 통과했다. 저장 실패·재시도·연타·중복·잠금·늦은 완료·기록으로 잠긴 행 보존을 포함한다.
- `node tests/fees.test.mjs` 43개, JS/Python 구문, 버전 일치, 생성물 대조, `git diff --check`가 통과했다. 새 npm 명령과 CI 모바일 시험 행렬을 연결했다.
- 구현 당시에는 로컬 Chromium·실제 PIN/IndexedDB·가상 접수로 검증했다.
- 배포 준비 중 전체 종단 검사에서 단독 접수의 품목 선택기가 설정에서 확정한 단가를 읽지 못하는 문제를 발견했다. 접수에서도 현장 품목 추가와 같은 수수료표를 읽도록 수정했다. 원문에 적힌 금액과 다른 단가로 표시와 저장을 확인하는 회귀를 추가했다.
