#!/usr/bin/env python3
"""Inspect and verify a pure-nona panorama pixel map.

Expected CSV columns:
  pano_x,pano_y,image_index,source_x,source_y

Comment metadata lines starting with "#" are allowed before the header.
"""

from __future__ import annotations

import argparse
import csv
import json
import math
import random
from collections import defaultdict
from pathlib import Path


REQUIRED_COLUMNS = ("pano_x", "pano_y", "image_index", "source_x", "source_y")


def parse_point(value: str) -> tuple[float, float]:
    pieces = str(value).split(",")
    if len(pieces) != 2:
        raise argparse.ArgumentTypeError(f"point must look like x,y: {value}")
    return float(pieces[0]), float(pieces[1])


def read_metadata(path: Path) -> dict[str, str]:
    meta: dict[str, str] = {}
    with path.open("r", encoding="utf-8", errors="replace", newline="") as fh:
        for line in fh:
            if not line.startswith("#"):
                break
            text = line[1:].strip()
            if "=" in text:
                key, value = text.split("=", 1)
                meta[key.strip()] = value.strip()
    return meta


def read_coordinate_meta(path: str | None) -> dict | None:
    if not path:
        return None
    return json.loads(Path(path).expanduser().read_text(encoding="utf-8"))


def final_to_pixel_map_point(meta: dict, point: tuple[float, float]) -> tuple[float, float]:
    final_w, final_h = [float(v) for v in meta["final_size"]]
    crop_left, crop_right, crop_top, crop_bottom = [float(v) for v in meta["crop"]]
    crop_w = crop_right - crop_left
    crop_h = crop_bottom - crop_top
    x, y = point
    return (
        crop_left + (float(x) + 0.5) * crop_w / final_w - 0.5,
        crop_top + (float(y) + 0.5) * crop_h / final_h - 0.5,
    )


def normalize_query_points(args) -> tuple[list[tuple[float, float]], list[tuple[float, float]]]:
    original = list(args.points)
    if getattr(args, "coords", "pixel-map") != "final":
        return original, original
    meta = read_coordinate_meta(getattr(args, "coordinate_meta", None))
    if meta is None:
        raise RuntimeError("--coordinate-meta is required when --coords final is used")
    return original, [final_to_pixel_map_point(meta, point) for point in original]


def open_csv(path: Path):
    fh = path.open("r", encoding="utf-8", errors="replace", newline="")
    while True:
        pos = fh.tell()
        line = fh.readline()
        if not line:
            raise RuntimeError(f"CSV header not found: {path}")
        if not line.startswith("#"):
            fh.seek(pos)
            break
    reader = csv.DictReader(fh)
    missing = [name for name in REQUIRED_COLUMNS if name not in (reader.fieldnames or [])]
    if missing:
        raise RuntimeError(f"missing CSV columns {missing}; got {reader.fieldnames}")
    return fh, reader


def iter_rows(path: Path):
    fh, reader = open_csv(path)
    try:
        for row in reader:
            if not row:
                continue
            yield {
                "pano_x": int(round(float(row["pano_x"]))),
                "pano_y": int(round(float(row["pano_y"]))),
                "image_index": int(row["image_index"]),
                "source_x": float(row["source_x"]),
                "source_y": float(row["source_y"]),
            }
    finally:
        fh.close()


def cmd_info(args) -> int:
    path = Path(args.map).expanduser()
    count = 0
    min_x = min_y = None
    max_x = max_y = None
    images: set[int] = set()
    duplicate_probe: dict[tuple[int, int], int] = defaultdict(int)

    for row in iter_rows(path):
        count += 1
        x = row["pano_x"]
        y = row["pano_y"]
        min_x = x if min_x is None else min(min_x, x)
        max_x = x if max_x is None else max(max_x, x)
        min_y = y if min_y is None else min(min_y, y)
        max_y = y if max_y is None else max(max_y, y)
        images.add(row["image_index"])
        if len(duplicate_probe) < args.duplicate_probe_limit or (x, y) in duplicate_probe:
            duplicate_probe[(x, y)] += 1

    overlap_pixels_in_probe = sum(1 for value in duplicate_probe.values() if value > 1)
    result = {
        "map": str(path),
        "metadata": read_metadata(path),
        "rows": count,
        "pano_bounds": {
            "min_x": min_x,
            "max_x": max_x,
            "min_y": min_y,
            "max_y": max_y,
        },
        "image_indices": sorted(images),
        "image_count": len(images),
        "overlap_pixels_in_probe": overlap_pixels_in_probe,
        "note": "CSV can contain multiple rows for one panorama pixel in overlap areas.",
    }
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0


def cmd_pano(args) -> int:
    path = Path(args.map).expanduser()
    original_points, lookup_points = normalize_query_points(args)
    wanted = [(int(round(x)), int(round(y))) for x, y in lookup_points]
    by_point: dict[tuple[int, int], list[dict]] = {point: [] for point in wanted}

    for row in iter_rows(path):
        key = (row["pano_x"], row["pano_y"])
        if key in by_point:
            by_point[key].append(row)

    results = []
    for original, lookup, rounded in zip(original_points, lookup_points, wanted):
        candidates = by_point[rounded]
        results.append(
            {
                "point": [original[0], original[1]],
                "coords": args.coords,
                "lookup_point": [lookup[0], lookup[1]],
                "rounded": [rounded[0], rounded[1]],
                "candidate_count": len(candidates),
                "candidates": candidates[: args.limit],
            }
        )
    print(json.dumps({"points": results}, ensure_ascii=False, indent=2))
    return 0


def cmd_source(args) -> int:
    path = Path(args.map).expanduser()
    image_index = int(args.image_index)
    sx, sy = args.point
    best: list[tuple[float, dict]] = []

    for row in iter_rows(path):
        if row["image_index"] != image_index:
            continue
        dist2 = (row["source_x"] - sx) ** 2 + (row["source_y"] - sy) ** 2
        if len(best) < args.limit:
            best.append((dist2, row))
            best.sort(key=lambda item: item[0])
            continue
        if dist2 < best[-1][0]:
            best[-1] = (dist2, row)
            best.sort(key=lambda item: item[0])

    matches = []
    for dist2, row in best:
        item = dict(row)
        item["source_error_px"] = math.sqrt(dist2)
        matches.append(item)
    print(
        json.dumps(
            {
                "image_index": image_index,
                "source_point": [sx, sy],
                "matches": matches,
            },
            ensure_ascii=False,
            indent=2,
        )
    )
    return 0


def cmd_sample(args) -> int:
    path = Path(args.map).expanduser()
    rng = random.Random(args.seed)
    sample: list[dict] = []
    count = 0

    for row in iter_rows(path):
        count += 1
        if len(sample) < args.limit:
            sample.append(row)
        else:
            idx = rng.randrange(count)
            if idx < args.limit:
                sample[idx] = row

    sample.sort(key=lambda item: (item["pano_y"], item["pano_x"], item["image_index"]))
    print(
        json.dumps(
            {
                "rows_seen": count,
                "sample_count": len(sample),
                "sample": sample,
            },
            ensure_ascii=False,
            indent=2,
        )
    )
    return 0


def choose_owner(rows: list[dict], source_sizes: dict[int, tuple[float, float]] | None = None) -> dict:
    if len(rows) == 1:
        return rows[0]
    if source_sizes:
        def score(row: dict) -> float:
            size = source_sizes.get(row["image_index"])
            if not size:
                return float("inf")
            w, h = size
            return (row["source_x"] - w / 2.0) ** 2 + (row["source_y"] - h / 2.0) ** 2

        return min(rows, key=score)
    return rows[0]


def cmd_to_npz(args) -> int:
    import numpy as np

    path = Path(args.map).expanduser()
    out = Path(args.out).expanduser()
    rows_by_pixel: dict[tuple[int, int], list[dict]] = defaultdict(list)
    max_x = max_y = -1
    for row in iter_rows(path):
        key = (row["pano_x"], row["pano_y"])
        rows_by_pixel[key].append(row)
        max_x = max(max_x, row["pano_x"])
        max_y = max(max_y, row["pano_y"])
    if max_x < 0 or max_y < 0:
        raise RuntimeError(f"no rows found in {path}")

    owner = np.full((max_y + 1, max_x + 1), -1, dtype=np.int16)
    source_x = np.full((max_y + 1, max_x + 1), np.nan, dtype=np.float32)
    source_y = np.full((max_y + 1, max_x + 1), np.nan, dtype=np.float32)
    coverage_count = np.zeros((max_y + 1, max_x + 1), dtype=np.uint16)

    for (x, y), rows in rows_by_pixel.items():
        chosen = choose_owner(rows)
        owner[y, x] = chosen["image_index"]
        source_x[y, x] = chosen["source_x"]
        source_y[y, x] = chosen["source_y"]
        coverage_count[y, x] = min(len(rows), np.iinfo(np.uint16).max)

    out.parent.mkdir(parents=True, exist_ok=True)
    np.savez_compressed(
        out,
        owner=owner,
        source_x=source_x,
        source_y=source_y,
        coverage_count=coverage_count,
        metadata_json=json.dumps(
            {
                "schema": "pure_nona_pixel_map_v1",
                "source_csv": str(path),
                "shape": [int(owner.shape[1]), int(owner.shape[0])],
                "valid_pixels": int((owner >= 0).sum()),
                "note": "Owner is the first candidate when multiple rows exist.",
            },
            ensure_ascii=False,
        ),
    )
    print(
        json.dumps(
            {
                "out": str(out),
                "shape": [int(owner.shape[1]), int(owner.shape[0])],
                "valid_pixels": int((owner >= 0).sum()),
            },
            ensure_ascii=False,
            indent=2,
        )
    )
    return 0


def parse_args(argv=None):
    parser = argparse.ArgumentParser(description="Inspect pure-nona pixel map CSV output.")
    sub = parser.add_subparsers(dest="command", required=True)

    info = sub.add_parser("info", help="Show map summary")
    info.add_argument("map", help="pixel_map.csv")
    info.add_argument("--duplicate-probe-limit", type=int, default=200000)
    info.set_defaults(func=cmd_info)

    pano = sub.add_parser("pano", help="Lookup panorama pixel candidates")
    pano.add_argument("map", help="pixel_map.csv")
    pano.add_argument("points", nargs="+", type=parse_point)
    pano.add_argument("--limit", type=int, default=20)
    pano.add_argument(
        "--coords",
        choices=["pixel-map", "final"],
        default="pixel-map",
        help="Coordinate system of the input point. Use final for the displayed stitched image.",
    )
    pano.add_argument(
        "--coordinate-meta",
        help="coordinate_meta.json used to translate final image pixels to nona pixel-map coordinates.",
    )
    pano.set_defaults(func=cmd_pano)

    source = sub.add_parser("source", help="Reverse lookup nearest panorama pixels")
    source.add_argument("map", help="pixel_map.csv")
    source.add_argument("image_index", help="zero-based source image index")
    source.add_argument("point", type=parse_point, help="source point x,y")
    source.add_argument("--limit", type=int, default=10)
    source.set_defaults(func=cmd_source)

    sample = sub.add_parser("sample", help="Print deterministic random sample")
    sample.add_argument("map", help="pixel_map.csv")
    sample.add_argument("--limit", type=int, default=20)
    sample.add_argument("--seed", type=int, default=1)
    sample.set_defaults(func=cmd_sample)

    to_npz = sub.add_parser("to-npz", help="Convert CSV to compact owner/source arrays")
    to_npz.add_argument("map", help="pixel_map.csv")
    to_npz.add_argument("out", help="pixel_map.npz")
    to_npz.set_defaults(func=cmd_to_npz)

    return parser.parse_args(argv)


def main(argv=None) -> int:
    args = parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    raise SystemExit(main())
