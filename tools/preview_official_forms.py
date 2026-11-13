"""검토용 가상 월보/수불대장 생성. 운영 DB를 열지 않는다."""
import argparse
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parent.parent
sys.path[:0] = [str(ROOT / 'tests'), str(ROOT / 'pc')]
from test_label_auto import LabelAutoTests
from dgpc import official_forms as F


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--output-dir', required=True, type=Path)
    parser.add_argument('--rows', type=int, default=35)
    args = parser.parse_args()
    args.output_dir.mkdir(parents=True, exist_ok=True)
    fixture = LabelAutoTests('test_fresh_schema_and_dong_stock_isolation')
    fixture.setUp()
    try:
        fixture.stock('200001', '200200', day='2026-06-01')
        for index in range(args.rows):
            qty = 3 if index % 4 == 0 else 1
            request, line = fixture.request(qty=qty)
            address = f'가상시 동구 예시로 {index + 1} (가상 검증 주소)'
            if index % 7 == 0:
                address += ' 가상공동주택 관리동 옆 지정 수거장소'
            fixture.con.execute('UPDATE request SET name=?,phone=?,address=? WHERE id=?',
                                (f'가상주민{index + 1:02d}', f'0100000{index + 1:04d}', address, request['id']))
            fixture.collect(request, line, qty, '2026-06-30')
        dong_id = fixture.dongs['충장동']
        monthly = F.monthly_data(fixture.con, dong_id, '2026-06')
        daily = F.daily_data(fixture.con, dong_id, '2026-06-30')
        (args.output_dir / '월보_가상.xlsx').write_bytes(F.xlsx_file(monthly))
        (args.output_dir / '수불대장_가상.odt').write_bytes(F.odt_file(daily))
        import json
        (args.output_dir / 'expected.json').write_text(json.dumps({'monthly': monthly['totals'], 'daily': daily['totals'], 'rows': args.rows}, ensure_ascii=False, indent=2), encoding='utf-8')
        print(json.dumps({'directory': str(args.output_dir), 'rows': args.rows, 'totals': monthly['totals']}, ensure_ascii=False))
    finally:
        fixture.tearDown()


if __name__ == '__main__':
    main()
