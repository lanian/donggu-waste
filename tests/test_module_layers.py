"""PC 업무 모듈 계층 검사: 모듈 맨 위 import 는 아래 계층만, 위 계층은 등록된 늦은 import(함수 안)로만 부른다.

계층의 정본은 pc/dgpc/service.py docstring 이다. 모듈을 새로 만들거나 import 를 바꾸면 이 표(LAYERS)와
docstring 을 함께 고친다. 등록되지 않은 모듈이 있거나 위 계층을 부르면 실패한다.
"""
from __future__ import annotations

import ast
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
PKG = ROOT / "pc" / "dgpc"

# 아래 → 위. 같은 단계끼리는 맨 위 import 로 서로 부르지 않는다.
LAYERS = [
    ["settings", "paths", "geo", "dong_defaults", "operation_print", "dispatch_print_map", "osrm"],  # 다른 업무 모듈을 부르지 않는 바닥
    ["store"],
    ["core"],
    ["fees", "external"],
    ["derive", "routing"],
    ["collection", "payments", "devices", "users", "auditlog", "addresses"],
    ["area", "finance", "label_auto", "photos", "dong_centers", "route_depots", "form_files"],
    ["numbered_labels", "official_forms"],
    ["intake"],
    ["results"],
    ["privacy", "pc_collection"],
    ["assign", "backup"],
    ["close", "reports"],
    ["config"],
    ["addrsearch", "vworld"],
    ["dong_work"],
    ["followups"],
    ["service"],
    ["ops"],
    ["server"],
]

# 위 계층을 함수 안에서 늦게 부르는 허용 목록 (모듈 → 부르는 모듈). 순환을 피하려는 것이므로 늘릴 때 이유를 적는다.
LAZY_UP = {
    "store": {"derive", "dong_defaults"},  # 구조 정리 단계에서 요약 열·기본 동 채우기
    "derive": {"collection"},  # 이벤트 순서(event_order)
    "area": {"dong_centers", "route_depots", "routing"},  # 동 변경 때 센터·차고지·노선 갱신
    "dong_centers": {"area"},
    "route_depots": {"area"},
    "label_auto": {"numbered_labels"},  # 배출표 자동 배정 ⇄ 번호 재고
    "form_files": {"official_forms"},  # 원본 서식 ⇄ 번호 구간
    "intake": {"pc_collection"},  # 접수 상세의 PC 직접 수거 표시
    "assign": {"area", "pc_collection", "routing"},
    "pc_collection": {"intake", "results"},
    "config": {"vworld"},
    "vworld": {"addrsearch"},
    "ops": {"server"},
}

passed = 0


def ok(cond, msg):
    global passed
    if not cond:
        raise SystemExit(f"  ✗ {msg}")
    passed += 1
    print(f"  ✓ {msg}")


def imports(path: Path):
    """(맨 위 import 대상, 함수 안 import 대상)."""
    top, lazy = set(), set()
    for node in ast.walk(ast.parse(path.read_text(encoding="utf-8"))):
        if isinstance(node, ast.ImportFrom) and node.module:
            if node.module == "dgpc":
                names = [a.name for a in node.names]
            elif node.module.startswith("dgpc."):
                names = [node.module.split(".")[1]]
            else:
                continue
        elif isinstance(node, ast.Import):
            names = [a.name.split(".")[1] for a in node.names if a.name.startswith("dgpc.")]
        else:
            continue
        (top if node.col_offset == 0 else lazy).update(names)
    return top, lazy


rank = {m: i for i, layer in enumerate(LAYERS) for m in layer}
modules = {p.stem for p in PKG.glob("*.py") if p.stem != "__init__"}
ok(not (modules - rank.keys()), f"모든 업무 모듈이 계층표에 있음 (빠짐: {sorted(modules - rank.keys())})")
ok(not (rank.keys() - modules), f"계층표에 없는 모듈 이름 없음 (남음: {sorted(rank.keys() - modules)})")

for m in sorted(modules):
    top, lazy = imports(PKG / f"{m}.py")
    top &= modules
    lazy = (lazy & modules) - top
    if m != "service":  # 겉면은 모든 하위 모듈을 다시 내보낸다
        bad = sorted(t for t in top if rank[t] >= rank[m])
        ok(not bad, f"{m}: 맨 위 import 는 아래 계층만 ({bad})")
    up = sorted(t for t in lazy if rank[t] >= rank[m] and t not in LAZY_UP.get(m, set()))
    ok(not up, f"{m}: 위 계층 늦은 import 는 허용 목록만 ({up})")

print(f"\n통과 {passed}개")
