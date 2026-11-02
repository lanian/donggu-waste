#!/usr/bin/env python3
"""공식 수수료표 원문(HWP, [별표 1])을 운영용 수수료표 JSON으로 변환한다.

  pip install pyhwp
  python tools/convert_fee_hwp.py data/source/별표1_대형폐기물_품목_및_수수료_기준.hwp \
      --checked-on 2026-11-02 --out data/fee_table.json

규칙
- 원문 행을 그대로 보존(source_row)하고, 품목·규격·부과단위·금액을 구조화한다.
- 단위가 다른 행(개·쪽·세트·20ℓ·50ℓ·3.3㎡·장 등)은 서로 다른 단위 코드로 둔다. 서로 합산하지 않는다.
- 원문 규격이 겹치거나 단위 차원이 불명확한 행은 status=REVIEW로 두고 임의로 고치지 않는다.
  REVIEW 행은 검색·선택은 되지만 금액은 "확인 대기"로 남는다(최저가 자동 선택 금지).
- 시행일은 원문에 없으므로 비워 두고, 담당자가 확인해 기록한다.
"""
from __future__ import annotations

import argparse
import hashlib
import html
import json
import re
import subprocess
import sys
import tempfile
from pathlib import Path

UNIT_RULES = [  # (정규식, 단위 코드, 화면 표기)
    (r"1\s*쪽\s*당", "PIECE", "쪽"),
    (r"1\s*세트", "SET", "세트"),
    (r"1\s*짝\s*당", "JJAK", "짝(폭 1m)당"),
    (r"1\s*피스\s*당", "PIECE_ITEM", "피스"),
    (r"50\s*ℓ.*당", "L50", "50ℓ당"),
    (r"20\s*ℓ\s*당", "L20", "20ℓ당"),
    (r"3\.3\s*㎡\s*당", "AREA33", "3.3㎡당"),
    (r"용적\s*1\.0\s*㎡\s*당", "VOL1", "용적 1.0(원문 ㎡)당"),
    (r"3\.3\s*kg\s*당", "KG33", "3.3kg당"),
    (r"장\s*당", "SHEET", "장"),
]

# 원문 규격이 서로 겹치거나 단위가 불명확해 담당 기준 확인이 필요한 행: (품목, 규격 일부) → 사유
REVIEW = {
    ("식탁", "6인용 이상(대리석)"): "대리석 식탁 '6인용 이상'과 '6인용 이하'가 6인용에서 겹침",
    ("식탁", "6인용 이하(대리석)"): "대리석 식탁 '6인용 이상'과 '6인용 이하'가 6인용에서 겹침",
    ("물탱크, 정화조(FRP)", "용적 1.0 ㎡ 당"): "용적 단위가 ㎡(면적)로 표기되어 ㎥ 여부 확인 필요",
    ("싱크대 및 싱크찬장", "소형 1짝당(폭 1 m 당)"): "'1짝당'과 '폭 1m당' 중 산정 기준 확인 필요",
    ("오락기", "29인치 미만"): "'29인치 미만'과 '20인치 이하'가 20인치 이하에서 겹침",
    ("오락기", "20인치 이하"): "'29인치 미만'과 '20인치 이하'가 20인치 이하에서 겹침",
    ("고무통", "90cm ☓100 cm 미만"): "'90×100 미만'과 '70×80 이하'가 겹침",
    ("고무통", "70cm ☓80 cm 이하"): "'90×100 미만'과 '70×80 이하'가 겹침",
}

ALIASES = {
    "쇼파": "소파", "장농": "장롱", "메트리스": "매트리스", "매트": "매트리스", "티비": "텔레비전", "tv": "텔레비전",
    "회전의자": "의자 회전식", "사무용의자": "의자 회전식", "물정수기": "정수기", "책장": "책꽂이", "드럼": "드럼세탁기",
    "런닝머신": "러닝머신", "에어콘": "에어컨", "전자렌지": "전자레인지", "캐리어": "가방 캐리어", "돌침대": "침대(돌",
    "옷장": "장롱", "냉장": "냉장고", "식기세척": "식기세척기", "화장실변기": "변기",
}


def clean(s: str) -> str:
    s = re.sub(r"\s+", " ", s).strip()
    s = re.sub(r"\(\s+", "(", s)
    s = re.sub(r"\s+\)", ")", s)
    return s


def hwp_rows(hwp: Path) -> list[list[str]]:
    with tempfile.TemporaryDirectory() as d:
        subprocess.run(["hwp5html", "--output", d, str(hwp)], check=True, capture_output=True)
        doc = (Path(d) / "index.xhtml").read_text(encoding="utf8")
    out = []
    for table in re.findall(r"<table.*?</table>", doc, re.S):
        carry: dict[int, tuple[str, int]] = {}
        for tr in re.findall(r"<tr.*?</tr>", table, re.S):
            cells = []
            for attrs, inner in re.findall(r"<td(.*?)>(.*?)</td>", tr, re.S):
                txt = " ".join(html.unescape(re.sub(r"<[^>]+>", " ", inner)).split())
                rs = re.search(r'rowspan="(\d+)"', attrs)
                cs = re.search(r'colspan="(\d+)"', attrs)
                cells.append((txt, int(rs.group(1)) if rs else 1, int(cs.group(1)) if cs else 1))
            row: dict[int, str] = {}
            c = 0
            while cells or c in carry:
                if c in carry:
                    txt, left = carry[c]
                    row[c] = txt
                    if left > 1:
                        carry[c] = (txt, left - 1)
                    else:
                        del carry[c]
                    c += 1
                    continue
                txt, rs, cs = cells.pop(0)
                for k in range(cs):
                    row[c + k] = txt
                    if rs > 1:
                        carry[c + k] = (txt, rs - 1)
                c += cs
            out.append([row.get(i, "") for i in range(max(row) + 1)] if row else [])
    return out


def footnotes(hwp: Path) -> list[str]:
    txt = subprocess.run(["hwp5txt", str(hwp)], check=True, capture_output=True, text=True).stdout
    return [clean(l.lstrip("* ")) for l in txt.splitlines() if l.strip().startswith("*")]


def build(hwp: Path, checked_on: str) -> dict:
    rows, seen = [], set()
    for r in hwp_rows(hwp):
        if len(r) < 4 or r[0].replace(" ", "") == "품목류":
            continue
        category, name, spec, price, note = (clean(x) for x in (r + [""])[:5])
        category = category.replace(" ", "")
        name = re.sub(r"\(\s*(\S)", r"(\1", name)
        unit, label = "EA", "개"
        for pat, code, lab in UNIT_RULES:
            if re.search(pat, spec):
                unit, label = code, lab
                break
        rid = "f-" + hashlib.sha1(f"{category}|{name}|{spec}".encode()).hexdigest()[:10]
        if rid in seen:
            sys.exit(f"중복 행: {name} {spec}")
        seen.add(rid)
        review = next((why for (n, sp), why in REVIEW.items() if n.replace(" ", "") == name.replace(" ", "") and clean(sp).replace(" ", "") == spec.replace(" ", "")), None)
        rows.append({
            "id": rid, "category": category, "name": name, "spec": spec, "unit": unit, "unit_label": label,
            "price": int(price.replace(",", "")), "status": "REVIEW" if review else "CONFIRMED",
            **({"note": review} if review else {}), **({"remark": note} if note else {}),
            "source_row": " | ".join([category, name, spec, price, note]),
        })
    for fn in footnotes(hwp):
        m = re.search(r"(.+?)\s*:\s*20\s*ℓ\s*당\s*([\d,]+)\s*원", fn)
        if m:
            rows.append({"id": "f-bag20l", "category": "기타", "name": "종량제 봉투에 담기 어려운 폐기물", "spec": "20ℓ당",
                         "unit": "L20", "unit_label": "20ℓ당", "price": int(m.group(2).replace(",", "")), "status": "CONFIRMED",
                         "source_row": "* " + fn})
    missing = [k for k in REVIEW if not any(r["name"].replace(" ", "") == k[0].replace(" ", "") and r["status"] == "REVIEW" for r in rows)]
    if missing:
        sys.exit(f"검토 대상 행을 원문에서 찾지 못함: {missing}")
    return {
        "version_id": "byeolpyo1-" + hashlib.sha256(hwp.read_bytes()).hexdigest()[:8],
        "label": "[별표 1] 대형폐기물의 품목 및 수수료 기준",
        "source_file": hwp.name,
        "source_sha256": hashlib.sha256(hwp.read_bytes()).hexdigest(),
        "source_url": "https://www.donggu.kr/menu.es?mid=a10408010500",
        "source_page_updated": "2025-03-25",
        "source_checked_on": checked_on,
        "effective_from": None,
        "footnotes": footnotes(hwp),
        "rows": rows,
        "aliases": ALIASES,
    }


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("hwp", type=Path)
    ap.add_argument("--checked-on", required=True, help="자료 확인일 YYYY-MM-DD")
    ap.add_argument("--out", type=Path, default=Path("data/fee_table.json"))
    a = ap.parse_args()
    t = build(a.hwp, a.checked_on)
    a.out.write_text(json.dumps(t, ensure_ascii=False, indent=1) + "\n", encoding="utf8")
    n_rev = sum(r["status"] == "REVIEW" for r in t["rows"])
    units = sorted({r["unit_label"] for r in t["rows"]})
    print(f"{len(t['rows'])}행 (확인 필요 {n_rev}행), 단위 {len(units)}종: {', '.join(units)} → {a.out}")


if __name__ == "__main__":
    main()
