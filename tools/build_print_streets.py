"""광주 OSM 도로 추출본에서 PC 배차표에 쓸 오프라인 도로 바탕지도를 생성한다."""
import argparse
import gzip
import json
import os
from pathlib import Path
import subprocess
from tempfile import TemporaryDirectory


ROOT = Path(__file__).resolve().parents[1]
MAJOR = {"motorway", "trunk", "primary", "secondary", "tertiary"}
STREET = {"unclassified", "residential", "living_street", "road"}
MINOR = {"service", "pedestrian"}


def print_road(feature):
    tags = feature["properties"]
    highway = tags.get("highway", "").removesuffix("_link")
    if tags.get("area") == "yes" or feature["geometry"]["type"] != "LineString":
        return None
    if highway in MAJOR:
        category = "major"
    elif highway in STREET:
        category = "street"
    elif highway in MINOR:
        category = "minor"
    else:
        return None
    points = []
    for lon, lat in feature["geometry"]["coordinates"]:
        pair = (round(lon, 5), round(lat, 5))
        if not points or pair != points[-1]:
            points.append(pair)
    if len(points) >= 2:
        return [category, tags.get("name:ko") or tags.get("name") or "", points]
    return None


def generate(source, osmium, destination):
    with TemporaryDirectory(dir=destination.parent) as directory:
        features = Path(directory) / "roads.geojsonseq"
        subprocess.run([osmium, "export", source, "--geometry-types=linestring",
                        "-f", "geojsonseq", "-o", features], check=True)
        packed = Path(directory) / "print_streets.json.gz"
        count = 0
        with features.open(encoding="utf-8") as rows, packed.open("wb") as raw, \
                gzip.GzipFile(filename="", fileobj=raw, mode="wb", mtime=0) as out:
            out.write(b'{"schema":1,"roads":[')
            for line in rows:
                road = print_road(json.loads(line.lstrip("\x1e")))
                if road is not None:
                    if count:
                        out.write(b",")
                    out.write(json.dumps(road, ensure_ascii=False, separators=(",", ":")).encode("utf-8"))
                    count += 1
            out.write(b"]}")
        if not count:
            raise ValueError("원본에서 인쇄용 도로를 찾을 수 없습니다.")
        os.replace(packed, destination)
    return count


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("source", type=Path, help="광주권 OSM 도로 추출본 (.osm.pbf)")
    parser.add_argument("--osmium", default="osmium", help="자료 생성에만 사용하는 Osmium Tool 경로")
    parser.add_argument("--output", type=Path, default=ROOT / "data/print_streets.json.gz")
    args = parser.parse_args()
    print(f"{generate(args.source, args.osmium, args.output):,}개 도로 · {args.output}")
