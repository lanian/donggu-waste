# 인쇄용 광주 도로 바탕지도

`print_streets.json.gz`는 © OpenStreetMap contributors의 자료를 가공한 데이터베이스입니다. 이 파일은 [Open Database License 1.0](https://opendatacommons.org/licenses/odbl/1-0/)으로 제공합니다. [OpenStreetMap 저작자 표시](https://www.openstreetmap.org/copyright)

## 원본

Geofabrik 대한민국 추출본의 OSM 기준 시각은 2026-11-01 21:21:36 UTC입니다. 광주권 선택 범위는 동경 126.55~127.22°, 북위 34.94~35.43°이며 경계를 지나는 도로는 전체 선을 포함할 수 있습니다. [Geofabrik 원본 PBF](https://download.geofabrik.de/asia/south-korea/south-korea-261101.osm.pbf)

광주권 중간 파일은 동봉 도로 편집 키트의 `base.osm.pbf`입니다. SHA-256은 `cfd9b54ff28d14e1e358aab88abfcea7d2bc27ecf36c41a3edf1ac6030498e89`입니다. 원본 대한민국 PBF에서 이를 추출하는 조건과 OSM 기준일은 함께 제공되는 키트의 `SOURCES.md`에도 기록되어 있습니다.

## 생성

프로젝트 루트에서 Python과 Osmium Tool 1.19.1로 아래 명령을 실행합니다. 완성한 파일은 PC 실행 프로그램에 포함됩니다.

```console
python tools/build_print_streets.py build/osrm-edit-kit/base.osm.pbf
```

자료의 `roads` 각 행은 도로 분류, 이름, WGS84 경도·위도 좌표열입니다. 이름은 한국어 이름과 현지 이름 순으로 선택합니다. 차량 도로와 보행자 전용 거리를 함께 수록하며, 통행 허용 방향과 차량 진입 가능 여부는 바탕지도의 표시 대상 선정에 사용하지 않습니다. 인쇄 축척에 맞춰 좌표를 소수점 다섯 자리까지 보관합니다.
