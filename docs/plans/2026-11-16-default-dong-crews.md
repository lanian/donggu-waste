# 동별 기본 수거팀 Implementation Plan

> **For Claude:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this plan task-by-task.

**Goal:** 기본 행정동마다 수거팀을 하나씩 생성하고 기본 담당팀으로 연결한다.

**Architecture:** DB 이전 12단계에서 사용 중이며 담당팀이 없는 기본 행정동에만 전용팀을 추가한다. 기존 담당팀·사용 중지 동·사용자 추가 동·삭제한 동은 유지한다. 기존의 트랜잭션·사전 암호화 사본을 사용하고, 이후 재시작에는 다시 보충하지 않는다.

**Tech Stack:** Python, SQLCipher, 기존 DB 이전 및 unittest.

---

## 동작 규칙

- 새 자료는 기존 기본 동 13개에 각각 `충장동 수거팀`처럼 팀을 생성·연결한다.
- 팀은 기존 스키마의 적재량·근무시간 기본값을 사용하고 휴대폰은 연결하지 않는다.
- 기존 자료는 담당팀이 없는 사용 중 기본 동만 보충한다. 기존에 여러 동이 같은 팀을 사용해도 그 연결을 보존한다.
- 공백이 다른 기본 동 이름은 기존 이름과 ID를 유지한다. 기존 팀과 이름이 겹치면 `(2)`, `(3)`을 붙여 새 전용팀을 구분한다.
- 요일·한도·코드·차고지·기존 팀·접수·담당자 연결을 보존한다. 센터 차고지는 기존의 명시적 적용 기능을 사용한다.
- 이후 사용자가 담당팀을 비우거나 동을 삭제해도 재시작 때 복원하지 않는다.

## Task 1: 변경 전 실패 확인

- Modify: `tests/test_default_dongs.py`
- 새 자료의 13개 고유 팀, 11→12 보충·기존 행 보존, 이름 충돌, 읽기 전용, 재시작·롤백 회귀를 추가한다.
- Run: `.venv/Scripts/python.exe tests/test_default_dongs.py`
- Expected: 기본 팀 생성·이전이 없어 새 회귀가 실패한다.

## Task 2: 기본값 구현

- Modify: `pc/dgpc/dong_defaults.py`, `pc/dgpc/store.py`
- 기존 기본 동 목록을 이용해 전용팀을 보충하고 12단계로 등록한다. 이전 단계의 의미는 바꾸지 않는다.
- Modify: `pc/ui/screens/settings.js`, `README.md`
- 기본 담당팀 자동 등록 안내를 갱신한다.
- Modify: `tests/test_dong_centers.py`, `tests/test_dong_centers_api.py`, `tests/test_dong_depots.py`
- 기본팀이 모두 연결된 초기 상태를 기대하도록 센터 적용 건수를 갱신하고, 차고지 열 추가의 보존 검사는 10→11단계로 범위를 고정한다.
- Run: 기본 동 회귀와 기존 PC·동별 배차·센터 차고지 검사를 실행한다. 기존 시험의 미지정팀 시나리오는 명시적 fixture로 유지한다.

## 검증

- 임시 암호화 DB와 가상 주민 자료만 사용한다. 실행 중인 사용자 DB는 열지 않는다.
- 소스 변경 검증과 실제 EXE 빌드·배포를 구분해 보고한다.

## 검증 결과

- 변경 전 기본팀 관련 회귀 7개 실패를 확인한 뒤 구현했다. 변경 후 `test_default_dongs.py` 전체 15개가 통과했다.
- `test_pc.py` 업무 검사 688개, `test_dong_work.py` 동별 권한 23개, 센터 차고지 로직·실HTTP 9개, `test_dong_depots.py` 6개가 통과했다.
- 기존 차고지 이전 검사 1개는 새 담당팀 보충까지 함께 실행하던 범위 때문에 실패했다. 10→11단계만 검사하도록 고정한 뒤 전체 6개 통과를 확인했다. 11→12 및 8→12 보존은 기본팀 회귀에서 검증했다.
- `test_dong_centers_ui.py`의 실제 임시 SQLCipher/HTTP/Chromium 흐름이 통과했다. 저장·초안·응답 유실/지연·기존 차고지·기존 담당팀 보존을 확인하고 생성 화면에서 나머지 동의 기본팀 선택값을 확인했다.
- 모듈 계층 83개, 버전 일치, 생성물 대조, Python/JS 구문, `git diff --check`가 통과했다.
- 로컬 소스와 임시 자료 검증이다. 실제 업무 DB 적용, 전체 휴대폰 E2E, EXE 빌드·배포는 수행하지 않았다.
