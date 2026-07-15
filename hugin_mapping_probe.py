#!/usr/bin/env python3
"""Inspect a Hugin panorama coordinate-map bundle.

Typical use inside an exported bundle:
  python hugin_mapping_probe.py info --bundle .
  python hugin_mapping_probe.py pano --bundle . 2600.7,268.5
  python hugin_mapping_probe.py source --bundle . 4 694.6,449.8
"""

from __future__ import annotations

import argparse
import json
import math
import re
from pathlib import Path

import numpy as np


def read_json(path: Path) -> dict:
    return json.loads(path.read_text(encoding="utf-8"))


def parse_point(value: str) -> tuple[float, float]:
    pieces = str(value).split(",")
    if len(pieces) != 2:
        raise argparse.ArgumentTypeError(f"point must look like x,y: {value}")
    return float(pieces[0]), float(pieces[1])


def resolve_bundle(path: str | None) -> Path:
    bundle = Path(path or ".").expanduser().resolve()
    if not bundle.exists():
        raise RuntimeError(f"bundle directory does not exist: {bundle}")
    return bundle


def bundle_file(bundle: Path, name: str) -> Path:
    candidate = bundle / name
    if candidate.exists():
        return candidate
    if name == "coordinate_map.npz":
        matches = sorted(bundle.glob("*_coordinate_map.npz"))
        if matches:
            return matches[-1]
    if name == "coordinate_meta.json":
        matches = sorted(bundle.glob("*_coordinate_meta.json"))
        if matches:
            return matches[-1]
    manifest_path = bundle / "manifest.json"
    if manifest_path.exists():
        manifest = read_json(manifest_path)
        files = manifest.get("files") or {}
        key = Path(name).stem
        value = files.get(key) or files.get(name)
        if isinstance(value, str) and Path(value).exists():
            return Path(value)
    raise RuntimeError(f"unable to find {name} in bundle: {bundle}")


def load_map(bundle: Path):
    return np.load(str(bundle_file(bundle, "coordinate_map.npz")), allow_pickle=False)


def load_meta(bundle: Path) -> dict:
    try:
        return read_json(bundle_file(bundle, "coordinate_meta.json"))
    except Exception:
        return {}


def point_result(data, point: tuple[float, float]) -> dict:
    owner = data["owner"]
    source_x = data["source_x"]
    source_y = data["source_y"]
    shot_files = [str(v) for v in data["shot_files"]]
    shot_pan = data["shot_pan"]
    shot_tilt = data["shot_tilt"]
    coverage = data["coverage_count"] if "coverage_count" in data.files else None
    h, w = owner.shape
    px, py = point
    ix = int(round(px))
    iy = int(round(py))
    in_frame = 0 <= ix < w and 0 <= iy < h
    if not in_frame:
        return {"px": px, "py": py, "in_frame": False, "has_source": False}
    idx = int(owner[iy, ix])
    if idx < 0:
        return {"px": px, "py": py, "in_frame": True, "has_source": False}
    result = {
        "px": px,
        "py": py,
        "rounded_px": ix,
        "rounded_py": iy,
        "in_frame": True,
        "has_source": True,
        "owner_index": idx,
        "shot_file": shot_files[idx],
        "shot_pan": float(shot_pan[idx]),
        "shot_tilt": float(shot_tilt[idx]),
        "source_px": float(source_x[iy, ix]),
        "source_py": float(source_y[iy, ix]),
    }
    if coverage is not None:
        result["coverage_count"] = int(coverage[iy, ix])
    return result


def resolve_owner_index(data, image: str) -> int:
    shot_files = [str(v) for v in data["shot_files"]]
    text = str(image).strip()
    if re.fullmatch(r"\d+", text):
        number = int(text)
        for idx, shot_file in enumerate(shot_files):
            match = re.search(r"shot[_-]?0*(\d+)", Path(shot_file).name, re.I)
            if match and int(match.group(1)) == number:
                return idx
        if 0 <= number < len(shot_files):
            return number
        if 1 <= number <= len(shot_files):
            return number - 1
    target = Path(text).name
    for idx, shot_file in enumerate(shot_files):
        if shot_file == text or Path(shot_file).name == target:
            return idx
    raise RuntimeError(f"source image not found in map: {image}")


def locate_source_pixel(data, owner_index: int, point: tuple[float, float], limit: int) -> list[dict]:
    owner = data["owner"]
    source_x = data["source_x"]
    source_y = data["source_y"]
    sx, sy = point
    mask = owner == owner_index
    if not np.any(mask):
        return []
    dist2 = np.full(owner.shape, np.inf, dtype=np.float32)
    dist2[mask] = (source_x[mask] - sx) ** 2 + (source_y[mask] - sy) ** 2
    count = min(max(1, limit), dist2.size)
    flat = np.argpartition(dist2.ravel(), count - 1)[:count]
    flat = flat[np.argsort(dist2.ravel()[flat])]
    h, w = owner.shape
    results = []
    for flat_idx in flat:
        y, x = divmod(int(flat_idx), w)
        if not np.isfinite(float(dist2[y, x])):
            continue
        results.append(
            {
                "pano_px": float(x),
                "pano_py": float(y),
                "mapped_source_px": float(source_x[y, x]),
                "mapped_source_py": float(source_y[y, x]),
                "source_error_px": float(math.sqrt(float(dist2[y, x]))),
            }
        )
    return results


def cmd_info(args) -> int:
    bundle = resolve_bundle(args.bundle)
    data = load_map(bundle)
    meta = load_meta(bundle)
    owner = data["owner"]
    shot_files = [str(v) for v in data["shot_files"]]
    result = {
        "bundle": str(bundle),
        "map_shape": [int(owner.shape[1]), int(owner.shape[0])],
        "valid_pixels": int((owner >= 0).sum()),
        "shot_count": len(shot_files),
        "final_size": meta.get("final_size"),
        "full_canvas": meta.get("full_canvas"),
        "crop": meta.get("crop"),
        "projection": meta.get("projection"),
        "shots": [{"index": idx, "file": file} for idx, file in enumerate(shot_files)],
    }
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0


def cmd_pano(args) -> int:
    data = load_map(resolve_bundle(args.bundle))
    results = [point_result(data, point) for point in args.points]
    print(json.dumps({"points": results}, ensure_ascii=False, indent=2))
    return 0


def cmd_source(args) -> int:
    data = load_map(resolve_bundle(args.bundle))
    owner_index = resolve_owner_index(data, args.image)
    results = locate_source_pixel(data, owner_index, args.point, args.limit)
    shot_files = [str(v) for v in data["shot_files"]]
    print(
        json.dumps(
            {
                "owner_index": owner_index,
                "shot_file": shot_files[owner_index],
                "source_point": [args.point[0], args.point[1]],
                "matches": results,
            },
            ensure_ascii=False,
            indent=2,
        )
    )
    return 0


def parse_args(argv=None):
    parser = argparse.ArgumentParser(description="Probe Hugin coordinate-map bundles.")
    sub = parser.add_subparsers(dest="command", required=True)

    info = sub.add_parser("info", help="Show bundle and coordinate-map summary")
    info.add_argument("--bundle", default=".", help="Exported mapping bundle directory")
    info.set_defaults(func=cmd_info)

    pano = sub.add_parser("pano", help="Map panorama pixel(s) to source image pixels")
    pano.add_argument("points", nargs="+", type=parse_point)
    pano.add_argument("--bundle", default=".", help="Exported mapping bundle directory")
    pano.set_defaults(func=cmd_pano)

    source = sub.add_parser("source", help="Find panorama pixels near a source image pixel")
    source.add_argument("image", help="Source image basename, shot number, or owner index")
    source.add_argument("point", type=parse_point)
    source.add_argument("--bundle", default=".", help="Exported mapping bundle directory")
    source.add_argument("--limit", type=int, default=5)
    source.set_defaults(func=cmd_source)
    return parser.parse_args(argv)


def main(argv=None) -> int:
    args = parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    raise SystemExit(main())
