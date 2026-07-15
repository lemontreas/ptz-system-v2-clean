#!/usr/bin/env python3
"""
Build and use coordinate metadata for Hugin equirectangular panoramas.

The mapper is intentionally separate from production code. It reads Hugin's
final .pto, learns the relation between Hugin yaw/pitch and the physical PTZ
pan/tilt encoded in shot filenames, then converts panorama pixels to PTZ angles.
"""

from __future__ import annotations

import argparse
import json
import math
import os
import re
import shutil
import subprocess
import tempfile
from pathlib import Path

from pmap_utils import (
    PCAND_FLAG_CENTER_NEAREST,
    PCAND_RECORD_DTYPE,
    PMAP_INVALID_INT16,
    load_pmap,
    quantize_source_array,
    write_pcand,
    write_pmap,
)


NUM_RE = r"[-+]?(?:\d+(?:\.\d*)?|\.\d+)(?:[eE][-+]?\d+)?"
SHOT_RE = re.compile(r"shot_\d+_pan(" + NUM_RE + r")_tilt(" + NUM_RE + r")", re.I)


def read_json(path: Path):
    return json.loads(path.read_text(encoding="utf-8"))


def write_json(path: Path, data) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")


def command_exists(name: str) -> bool:
    return shutil.which(name) is not None


def resolve_hugin_command(name: str) -> str | None:
    env_name = "HUGIN_" + re.sub(r"[^A-Za-z0-9]+", "_", name).upper() + "_BIN"
    env_value = os.getenv(env_name)
    if env_value:
        return str(Path(env_value).expanduser())
    env_dir = os.getenv("HUGIN_BIN_DIR")
    if env_dir:
        candidate = Path(env_dir).expanduser() / name
        if candidate.exists():
            return str(candidate)
    return shutil.which(name)


def _param(line: str, key: str):
    match = re.search(r"(?:^|\s)" + re.escape(key) + r"=?(?P<value>" + NUM_RE + r")(?=\s|$)", line)
    return float(match.group("value")) if match else None


def _image_name(line: str) -> str | None:
    match = re.search(r'\sn"([^"]+)"', line)
    return match.group(1) if match else None


def _image_size(path: Path) -> tuple[int, int] | None:
    try:
        import cv2  # type: ignore
        import numpy as np  # type: ignore

        data = np.frombuffer(path.read_bytes(), dtype=np.uint8)
        img = cv2.imdecode(data, cv2.IMREAD_UNCHANGED)
        if img is not None:
            h, w = img.shape[:2]
            return int(w), int(h)
    except Exception:
        pass

    try:
        from PIL import Image  # type: ignore

        with Image.open(path) as img:
            return int(img.width), int(img.height)
    except Exception:
            return None


def _safe_imread(path: Path):
    import cv2  # type: ignore
    import numpy as np  # type: ignore

    data = np.frombuffer(path.read_bytes(), dtype=np.uint8)
    img = cv2.imdecode(data, cv2.IMREAD_UNCHANGED)
    if img is None:
        raise RuntimeError(f"unable to read image: {path}")
    return img


def _safe_imwrite(path: Path, img) -> None:
    import cv2  # type: ignore

    path.parent.mkdir(parents=True, exist_ok=True)
    ok, encoded = cv2.imencode(path.suffix or ".tif", img)
    if not ok:
        raise RuntimeError(f"unable to encode image: {path}")
    path.write_bytes(encoded.tobytes())


def _parse_rational_or_float(value) -> float | None:
    try:
        if isinstance(value, tuple) and len(value) == 2:
            return float(value[0]) / float(value[1])
        return float(value)
    except Exception:
        return None


def _tiff_layer_info(path: Path) -> dict:
    info = {"offset_x": 0, "offset_y": 0, "full_width": None, "full_height": None}

    if command_exists("exiftool"):
        proc = subprocess.run(
            ["exiftool", "-j", str(path)],
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            check=False,
        )
        if proc.returncode == 0 and proc.stdout.strip():
            try:
                rows = json.loads(proc.stdout)
                row = rows[0] if rows else {}
                x_pos = _parse_rational_or_float(row.get("XPosition"))
                y_pos = _parse_rational_or_float(row.get("YPosition"))
                x_res = _parse_rational_or_float(row.get("XResolution")) or 1.0
                y_res = _parse_rational_or_float(row.get("YResolution")) or 1.0
                if x_pos is not None:
                    info["offset_x"] = int(round(x_pos * x_res))
                if y_pos is not None:
                    info["offset_y"] = int(round(y_pos * y_res))
                if row.get("ImageFullWidth") is not None:
                    info["full_width"] = int(row["ImageFullWidth"])
                if row.get("ImageFullHeight") is not None:
                    info["full_height"] = int(row["ImageFullHeight"])
                return info
            except Exception:
                pass

    try:
        from PIL import Image  # type: ignore

        with Image.open(path) as img:
            tags = img.tag_v2
            x_pos = _parse_rational_or_float(tags.get(286))
            y_pos = _parse_rational_or_float(tags.get(287))
            x_res = _parse_rational_or_float(tags.get(282)) or 1.0
            y_res = _parse_rational_or_float(tags.get(283)) or 1.0
            if x_pos is not None:
                info["offset_x"] = int(round(x_pos * x_res))
            if y_pos is not None:
                info["offset_y"] = int(round(y_pos * y_res))
    except Exception:
        pass

    return info


def parse_size(value: str) -> tuple[int, int]:
    match = re.match(r"^\s*(\d+)\s*[xX,]\s*(\d+)\s*$", value or "")
    if not match:
        raise ValueError("size must look like WIDTHxHEIGHT, for example 3000x514")
    return int(match.group(1)), int(match.group(2))


def parse_pair(value: str, name: str) -> tuple[float, float]:
    pieces = str(value or "").split(",")
    if len(pieces) != 2:
        raise ValueError(f"{name} must look like x,y")
    return float(pieces[0]), float(pieces[1])


def parse_pto(pto_path: Path) -> dict:
    p_line = None
    image_lines = []
    for line in pto_path.read_text(encoding="utf-8", errors="replace").splitlines():
        if line.startswith("p "):
            p_line = line
        elif line.startswith("i "):
            image_lines.append(line)

    if not p_line:
        raise ValueError(f"no panorama p line found in {pto_path}")

    full_w = int(_param(p_line, "w") or 0)
    full_h = int(_param(p_line, "h") or 0)
    hfov = float(_param(p_line, "v") or 0.0)
    projection = int(_param(p_line, "f") or -1)
    crop_match = re.search(r"(?:^|\s)S(\d+),(\d+),(\d+),(\d+)(?=\s|$)", p_line)
    if crop_match:
        crop = [int(crop_match.group(i)) for i in range(1, 5)]
    else:
        crop = [0, full_w, 0, full_h]

    if full_w <= 0 or full_h <= 0 or hfov <= 0:
        raise ValueError(f"invalid panorama line in {pto_path}: {p_line}")

    shots = []
    for idx, line in enumerate(image_lines):
        name = _image_name(line)
        if not name:
            continue
        shot_match = SHOT_RE.search(name)
        if not shot_match:
            continue
        yaw = _param(line, "y")
        pitch = _param(line, "p")
        roll = _param(line, "r")
        hfov = _param(line, "v")
        if hfov is not None and hfov <= 0 and shots:
            hfov = shots[0].get("hugin_hfov")
        width = int(_param(line, "w") or 0)
        height = int(_param(line, "h") or 0)
        if yaw is None or pitch is None:
            continue
        shots.append(
            {
                "index": idx,
                "file": name,
                "image_width": width,
                "image_height": height,
                "hugin_hfov": float(hfov or 0.0),
                "ptz_pan": float(shot_match.group(1)),
                "ptz_tilt": float(shot_match.group(2)),
                "hugin_yaw": float(yaw),
                "hugin_pitch": float(pitch),
                "hugin_roll": float(roll or 0.0),
            }
        )

    if len(shots) < 2:
        raise ValueError(f"not enough filename pan/tilt samples in {pto_path}")

    px_per_deg = full_w / hfov
    vfov = full_h / px_per_deg

    return {
        "pto_path": str(pto_path),
        "projection_id": projection,
        "projection": "equirectangular" if projection == 2 else f"hugin_projection_{projection}",
        "full_canvas": [full_w, full_h],
        "crop": crop,
        "hugin_fov": [hfov, vfov],
        "hugin_px_per_deg": px_per_deg,
        "shots": shots,
    }


def load_session_info(session_path: Path | None) -> dict:
    if session_path is None:
        return {}
    session = read_json(session_path)
    session_dir = session_path.parent
    shot_by_name = {}
    for shot in session.get("shots", []):
        rel = shot.get("undistorted_path") or shot.get("raw_path")
        if not rel:
            continue
        name = Path(rel).name
        shot_by_name[name] = {
            "actual_pan": float(shot.get("actual_pan", shot.get("target_pan", 0.0))),
            "actual_tilt": float(shot.get("actual_tilt", shot.get("target_tilt", 0.0))),
            "path": str((session_dir / rel).resolve()),
        }
    camera = session.get("camera") or {}
    new_k = camera.get("new_camera_matrix")
    intrinsics = None
    if new_k:
        intrinsics = {
            "fx": float(new_k[0][0]),
            "fy": float(new_k[1][1]),
            "cx": float(new_k[0][2]),
            "cy": float(new_k[1][2]),
        }
    return {
        "session_path": str(session_path),
        "session_dir": str(session_dir),
        "shot_by_name": shot_by_name,
        "intrinsics": intrinsics,
        "camera": camera,
    }


def resolve_session_json(value: str | None) -> Path | None:
    if not value:
        return None
    path = Path(value).expanduser().resolve()
    if path.is_dir():
        path = path / "session.json"
    return path if path.exists() else None


def _linear_fit(xs: list[float], ys: list[float]) -> dict:
    n = len(xs)
    sx = sum(xs)
    sy = sum(ys)
    sxx = sum(x * x for x in xs)
    sxy = sum(x * y for x, y in zip(xs, ys))
    den = n * sxx - sx * sx
    if abs(den) < 1e-9:
        raise ValueError("linear fit is singular")
    a = (n * sxy - sx * sy) / den
    b = (sy - a * sx) / n
    errors = [a * x + b - y for x, y in zip(xs, ys)]
    rms = math.sqrt(sum(e * e for e in errors) / max(1, n))
    return {"type": "linear_1d", "x": "hugin_yaw", "coefficients": [a, b], "rms_error_deg": rms}


def _solve_3x3(a: list[list[float]], b: list[float]) -> list[float]:
    m = [row[:] + [rhs] for row, rhs in zip(a, b)]
    for col in range(3):
        pivot = max(range(col, 3), key=lambda r: abs(m[r][col]))
        if abs(m[pivot][col]) < 1e-9:
            raise ValueError("affine fit is singular")
        if pivot != col:
            m[col], m[pivot] = m[pivot], m[col]
        scale = m[col][col]
        for j in range(col, 4):
            m[col][j] /= scale
        for r in range(3):
            if r == col:
                continue
            factor = m[r][col]
            for j in range(col, 4):
                m[r][j] -= factor * m[col][j]
    return [m[i][3] for i in range(3)]


def _affine_fit(samples: list[dict], target_key: str) -> dict:
    xtx = [[0.0, 0.0, 0.0] for _ in range(3)]
    xty = [0.0, 0.0, 0.0]
    for sample in samples:
        row = [sample["hugin_yaw"], sample["hugin_pitch"], 1.0]
        y = sample[target_key]
        for i in range(3):
            xty[i] += row[i] * y
            for j in range(3):
                xtx[i][j] += row[i] * row[j]
    coeff = _solve_3x3(xtx, xty)
    errors = []
    for sample in samples:
        pred = coeff[0] * sample["hugin_yaw"] + coeff[1] * sample["hugin_pitch"] + coeff[2]
        errors.append(pred - sample[target_key])
    rms = math.sqrt(sum(e * e for e in errors) / max(1, len(errors)))
    return {
        "type": "affine_2d",
        "terms": ["hugin_yaw", "hugin_pitch", "constant"],
        "coefficients": coeff,
        "rms_error_deg": rms,
    }


def build_models(shots: list[dict]) -> dict:
    distinct_tilts = sorted({round(s["ptz_tilt"], 4) for s in shots})
    quality = {
        "sample_count": len(shots),
        "distinct_physical_tilts": distinct_tilts,
        "tilt_mapping_reliable": len(distinct_tilts) >= 2,
        "warnings": [],
    }

    models = {}
    if len(distinct_tilts) >= 2 and len(shots) >= 4:
        try:
            models["pan"] = _affine_fit(shots, "ptz_pan")
            models["tilt"] = _affine_fit(shots, "ptz_tilt")
        except ValueError as exc:
            quality["warnings"].append(f"2D affine fit failed: {exc}; falling back to 1D pan fit")

    if "pan" not in models:
        models["pan"] = _linear_fit([s["hugin_yaw"] for s in shots], [s["ptz_pan"] for s in shots])

    if "tilt" not in models:
        if len(distinct_tilts) < 2:
            tilt_value = sum(s["ptz_tilt"] for s in shots) / len(shots)
            models["tilt"] = {
                "type": "constant_unverified",
                "value": tilt_value,
                "rms_error_deg": 0.0,
            }
            quality["warnings"].append(
                "Only one physical tilt row was found. Tilt output is a constant placeholder; "
                "capture at least two tilt rows to validate vertical mapping."
            )
        else:
            models["tilt"] = _linear_fit([s["hugin_pitch"] for s in shots], [s["ptz_tilt"] for s in shots])

    return {"models": models, "quality": quality}


def build_metadata(pto_path: Path, final_image: Path | None, final_size: tuple[int, int] | None = None) -> dict:
    parsed = parse_pto(pto_path)
    if final_size is None and final_image:
        final_size = _image_size(final_image)

    crop_left, crop_right, crop_top, crop_bottom = parsed["crop"]
    crop_size = [crop_right - crop_left, crop_bottom - crop_top]
    if final_size is None:
        final_size = tuple(crop_size)

    fit = build_models(parsed["shots"])
    metadata = {
        "schema": "hugin_equirectangular_coordinate_v1",
        "projection": parsed["projection"],
        "projection_id": parsed["projection_id"],
        "pto_path": parsed["pto_path"],
        "final_image": str(final_image) if final_image else None,
        "final_size": [int(final_size[0]), int(final_size[1])],
        "full_canvas": parsed["full_canvas"],
        "crop": parsed["crop"],
        "crop_size": crop_size,
        "hugin_fov": parsed["hugin_fov"],
        "hugin_px_per_deg": parsed["hugin_px_per_deg"],
        "resize_scale": [
            float(final_size[0]) / float(crop_size[0]),
            float(final_size[1]) / float(crop_size[1]),
        ],
        "models": fit["models"],
        "quality": fit["quality"],
        "shots": parsed["shots"],
    }
    return metadata


def _eval_model(model: dict, yaw: float, pitch: float) -> float:
    if model["type"] == "affine_2d":
        a, b, c = model["coefficients"]
        return a * yaw + b * pitch + c
    if model["type"] == "linear_1d":
        a, b = model["coefficients"]
        return a * yaw + b
    if model["type"] == "constant_unverified":
        return float(model["value"])
    raise ValueError(f"unknown model type: {model.get('type')}")


def _base_model_ptz(metadata: dict, yaw: float, pitch: float) -> tuple[float, float]:
    pan = _eval_model(metadata["models"]["pan"], yaw, pitch)
    tilt = _eval_model(metadata["models"]["tilt"], yaw, pitch)
    return pan, tilt


def _linear_fit_coeff(xs: list[float], ys: list[float]) -> tuple[float, float, float]:
    fit = _linear_fit(xs, ys)
    a, b = fit["coefficients"]
    return float(a), float(b), float(fit["rms_error_deg"])


def _manual_calibrated_ptz(metadata: dict, yaw: float, pitch: float) -> tuple[float, float] | None:
    calib = metadata.get("manual_calibration")
    if not calib:
        return None
    base_pan, base_tilt = _base_model_ptz(metadata, yaw, pitch)
    pan = base_pan + float(calib.get("pan_offset_deg", 0.0))

    tilt_model = calib.get("tilt_model") or {}
    if tilt_model.get("type") == "hugin_pitch_linear":
        a, b = tilt_model.get("coefficients", [1.0, 0.0])
        tilt = float(a) * pitch + float(b)
    else:
        tilt = base_tilt + float(calib.get("tilt_offset_deg", 0.0))
    return pan, tilt


def rebuild_manual_calibration(metadata: dict) -> dict:
    anchors = metadata.get("manual_calibration", {}).get("anchors", [])
    if not anchors:
        metadata.pop("manual_calibration", None)
        return metadata

    pan_offsets = []
    pitch_values = []
    tilt_values = []
    enriched = []
    for anchor in anchors:
        px = float(anchor["px"])
        py = float(anchor["py"])
        actual_pan = float(anchor["actual_pan"])
        actual_tilt = float(anchor["actual_tilt"])
        yaw, pitch = pixel_to_hugin(metadata, px, py)
        base_pan, base_tilt = _base_model_ptz(metadata, yaw, pitch)
        pan_offsets.append(actual_pan - base_pan)
        pitch_values.append(pitch)
        tilt_values.append(actual_tilt)
        enriched.append(
            {
                "px": px,
                "py": py,
                "actual_pan": actual_pan,
                "actual_tilt": actual_tilt,
                "hugin_yaw": yaw,
                "hugin_pitch": pitch,
                "base_pan": base_pan,
                "base_tilt": base_tilt,
                "pan_residual_deg": actual_pan - base_pan,
                "tilt_residual_deg": actual_tilt - base_tilt,
            }
        )

    pan_offset = sum(pan_offsets) / len(pan_offsets)
    if len(pitch_values) >= 2 and max(pitch_values) - min(pitch_values) > 1e-6:
        tilt_a, tilt_b, tilt_rms = _linear_fit_coeff(pitch_values, tilt_values)
    else:
        tilt_a = 1.0
        tilt_b = tilt_values[0] - pitch_values[0]
        tilt_rms = 0.0

    metadata["manual_calibration"] = {
        "type": "hugin_equirectangular_anchor_v1",
        "pan_offset_deg": pan_offset,
        "tilt_model": {
            "type": "hugin_pitch_linear",
            "coefficients": [tilt_a, tilt_b],
            "rms_error_deg": tilt_rms,
        },
        "anchors": enriched,
        "note": (
            "Manual anchors calibrate panorama pixels to measured PTZ angles. "
            "Pan uses the base Hugin yaw model plus average residual; tilt uses "
            "a linear model from Hugin pitch to measured PTZ tilt."
        ),
    }
    return metadata


def _world_from_yaw_pitch(yaw_deg: float, pitch_deg: float) -> tuple[float, float, float]:
    yaw = math.radians(yaw_deg)
    pitch = math.radians(pitch_deg)
    return (
        math.sin(yaw) * math.cos(pitch),
        math.sin(pitch),
        math.cos(yaw) * math.cos(pitch),
    )


def _source_projection_from_hugin(yaw_deg: float, pitch_deg: float, shot: dict) -> dict | None:
    w = int(shot.get("image_width") or 0)
    h = int(shot.get("image_height") or 0)
    hfov = float(shot.get("hugin_hfov") or 0.0)
    if w <= 1 or h <= 1 or hfov <= 0:
        return None

    wx, wy, wz = _world_from_yaw_pitch(yaw_deg, pitch_deg)
    y0 = math.radians(float(shot["hugin_yaw"]))
    p0 = math.radians(float(shot["hugin_pitch"]))

    cos_y, sin_y = math.cos(y0), math.sin(y0)
    ax = wx * cos_y - wz * sin_y
    ay = wy
    az = wx * sin_y + wz * cos_y

    cos_p, sin_p = math.cos(p0), math.sin(p0)
    cx_ray = ax
    cy_ray = ay * cos_p - az * sin_p
    cz_ray = ay * sin_p + az * cos_p
    if cz_ray <= 1e-9:
        return None

    # Hugin roll is small in our captures, but accounting for it improves source
    # pixel selection near seams and tilted horizons.
    r0 = math.radians(float(shot.get("hugin_roll") or 0.0))
    cos_r, sin_r = math.cos(r0), math.sin(r0)
    ix_ray = cx_ray * cos_r + cy_ray * sin_r
    iy_ray = -cx_ray * sin_r + cy_ray * cos_r

    fx = (w / 2.0) / math.tan(math.radians(hfov) / 2.0)
    fy = fx
    cx = w / 2.0
    cy = h / 2.0
    src_x = cx + fx * (ix_ray / cz_ray)
    src_y = cy - fy * (iy_ray / cz_ray)
    in_bounds = 1 <= src_x < (w - 1) and 1 <= src_y < (h - 1)
    if not in_bounds:
        return None
    return {
        "source_px": src_x,
        "source_py": src_y,
        "center_weight": cz_ray,
        "radial_px": math.hypot(src_x - cx, src_y - cy),
        "fx": fx,
        "fy": fy,
        "cx": cx,
        "cy": cy,
    }


def _pixel_to_absolute_ptz_with_intrinsics(
    px: float,
    py: float,
    cx: float,
    cy: float,
    fx: float,
    fy: float,
    current_pan_deg: float,
    current_tilt_deg: float,
) -> tuple[float, float]:
    p = math.radians(current_pan_deg)
    t = math.radians(current_tilt_deg)

    rx = (px - cx) / fx
    ry = (cy - py) / fy
    rz = 1.0
    norm = math.sqrt(rx * rx + ry * ry + rz * rz)
    rx, ry, rz = rx / norm, ry / norm, rz / norm

    cos_t, sin_t = math.cos(t), math.sin(t)
    tx = rx
    ty = ry * cos_t + rz * sin_t
    tz = -ry * sin_t + rz * cos_t

    cos_p, sin_p = math.cos(p), math.sin(p)
    wx = tx * cos_p + tz * sin_p
    wy = ty
    wz = -tx * sin_p + tz * cos_p

    pan = math.degrees(math.atan2(wx, wz))
    if pan < 0:
        pan += 360.0
    tilt = math.degrees(math.atan2(wy, math.sqrt(wx * wx + wz * wz)))
    return pan, tilt


def _source_pixel_to_ptz(source: dict, shot: dict) -> tuple[float, float]:
    return _pixel_to_absolute_ptz_with_intrinsics(
        source["source_px"],
        source["source_py"],
        source["cx"],
        source["cy"],
        source["fx"],
        source["fy"],
        float(shot["ptz_pan"]),
        float(shot["ptz_tilt"]),
    )


def _single_image_ptz_from_source(
    source_x: float,
    source_y: float,
    shot_pan: float,
    shot_tilt: float,
    intrinsics: dict,
) -> tuple[float, float]:
    return _pixel_to_absolute_ptz_with_intrinsics(
        source_x,
        source_y,
        float(intrinsics["cx"]),
        float(intrinsics["cy"]),
        float(intrinsics["fx"]),
        float(intrinsics["fy"]),
        shot_pan,
        shot_tilt,
    )


def _source_pixel_solution(metadata: dict, yaw: float, pitch: float) -> dict | None:
    candidates = []
    for shot in metadata.get("shots", []):
        source = _source_projection_from_hugin(yaw, pitch, shot)
        if source is None:
            continue
        pan, tilt = _source_pixel_to_ptz(source, shot)
        candidates.append(
            {
                "shot_index": int(shot["index"]),
                "shot_file": shot["file"],
                "shot_ptz_pan": float(shot["ptz_pan"]),
                "shot_ptz_tilt": float(shot["ptz_tilt"]),
                "source_px": source["source_px"],
                "source_py": source["source_py"],
                "center_weight": source["center_weight"],
                "radial_px": source["radial_px"],
                "pan": pan,
                "tilt": tilt,
            }
        )
    if not candidates:
        return None
    candidates.sort(key=lambda item: (-item["center_weight"], item["radial_px"]))
    best = dict(candidates[0])
    best["candidate_count"] = len(candidates)
    best["candidates"] = [dict(item) for item in candidates[:3]]
    return best


def _replace_pto_image_paths(pto_text: str, replacement_paths: list[Path]) -> str:
    out = []
    image_idx = 0
    for line in pto_text.splitlines():
        if line.startswith("i "):
            if image_idx >= len(replacement_paths):
                raise ValueError("not enough replacement coordinate images for PTO image lines")
            repl = str(replacement_paths[image_idx]).replace("\\", "/")
            line = re.sub(r'\sn"[^"]+"', f' n"{repl}"', line)
            image_idx += 1
        out.append(line)
    if image_idx != len(replacement_paths):
        raise ValueError("replacement coordinate image count does not match PTO image lines")
    return "\n".join(out) + "\n"


def _single_image_coord_pto(pto_text: str, image_line_index: int, replacement_path: Path) -> str:
    out = []
    image_idx = 0
    found = False
    repl = str(replacement_path).replace("\\", "/")
    for line in pto_text.splitlines():
        if line.startswith("i "):
            if image_idx == image_line_index:
                out.append(re.sub(r'\sn"[^"]+"', f' n"{repl}"', line))
                found = True
            image_idx += 1
            continue
        if line.startswith("v ") or line.startswith("c "):
            continue
        out.append(line)
    if not found:
        raise ValueError(f"PTO image line {image_line_index} not found")
    return "\n".join(out) + "\n"


def _make_coord_image(path: Path, width: int, height: int) -> None:
    import numpy as np  # type: ignore

    xs = np.linspace(0, 65535, width, dtype=np.float32)
    ys = np.linspace(0, 65535, height, dtype=np.float32)
    x_img = np.tile(xs, (height, 1))
    y_img = np.tile(ys[:, None], (1, width))
    valid = np.full((height, width), 65535, dtype=np.float32)
    img = np.dstack([x_img, y_img, valid]).clip(0, 65535).astype(np.uint16)
    _safe_imwrite(path, img)


def _run_nona_coordinate_layers(pto_path: Path, shots: list[dict], work_dir: Path) -> list[tuple[int, Path]]:
    nona_bin = resolve_hugin_command("nona")
    if not nona_bin:
        raise RuntimeError("nona command not found; install Hugin/Panotools or run on the device")
    coord_dir = work_dir / "coord_sources"
    coord_dir.mkdir(parents=True, exist_ok=True)
    coord_paths = []
    for idx, shot in enumerate(shots):
        w = int(shot.get("image_width") or 0)
        h = int(shot.get("image_height") or 0)
        if w <= 1 or h <= 1:
            size = _image_size(Path(shot["file"]))
            if not size:
                raise ValueError(f"unable to determine source image size for {shot['file']}")
            w, h = size
        coord_path = coord_dir / f"coord_{idx:04d}.tif"
        _make_coord_image(coord_path, w, h)
        coord_paths.append(coord_path)

    coord_pto = work_dir / "coordinate_sources.pto"
    pto_text = pto_path.read_text(encoding="utf-8", errors="replace")
    coord_pto.write_text(_replace_pto_image_paths(pto_text, coord_paths), encoding="utf-8")
    prefix = work_dir / "coord_layer"
    before_files = {p.resolve() for p in work_dir.glob("*") if p.is_file()}
    image_suffixes = {".tif", ".tiff", ".png", ".jpg", ".jpeg"}
    proc_records = []
    layers = []
    all_after = []
    new_files = []
    for mode in ("TIFF", "TIFF_m"):
        for old in work_dir.glob(f"{prefix.name}*"):
            if old.is_file():
                old.unlink()
        before_files = {p.resolve() for p in work_dir.glob("*") if p.is_file()}
        proc = subprocess.run(
            [nona_bin, "-m", mode, "-o", str(prefix), str(coord_pto)],
            cwd=str(work_dir),
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            check=False,
        )
        proc_records.append((mode, proc))
        if proc.returncode != 0:
            continue
        all_after = sorted(p for p in work_dir.glob("*") if p.is_file())
        new_files = [p for p in all_after if p.resolve() not in before_files]
        layers = [
            p
            for p in all_after
            if p.name.startswith(prefix.name) and p.suffix.lower() in image_suffixes
        ]
        if len(layers) >= len(shots):
            return [(idx, layer) for idx, layer in enumerate(layers[: len(shots)])]

    if not proc_records or all(record[1].returncode != 0 for record in proc_records):
        details = []
        for mode, proc in proc_records:
            details.append(f"mode={mode}\nstdout:\n{proc.stdout}\nstderr:\n{proc.stderr}")
        raise RuntimeError("nona failed for all output modes:\n" + "\n\n".join(details))

    if len(layers) < len(shots):
        paired_layers = []
        single_errors = []
        for shot_idx, shot in enumerate(shots):
            prefix_single = work_dir / f"coord_single_{shot_idx:04d}"
            for old in work_dir.glob(f"{prefix_single.name}*"):
                if old.is_file():
                    old.unlink()
            single_pto = work_dir / f"coordinate_source_{shot_idx:04d}.pto"
            try:
                single_pto.write_text(
                    _single_image_coord_pto(
                        pto_text,
                        int(shot.get("index", shot_idx)),
                        coord_paths[shot_idx],
                    ),
                    encoding="utf-8",
                )
            except Exception as exc:
                single_errors.append(f"shot {shot_idx}: {exc}")
                continue
            single_layer = None
            single_records = []
            for mode in ("TIFF", "TIFF_m"):
                proc = subprocess.run(
                    [nona_bin, "-m", mode, "-o", str(prefix_single), str(single_pto)],
                    cwd=str(work_dir),
                    text=True,
                    stdout=subprocess.PIPE,
                    stderr=subprocess.PIPE,
                    check=False,
                )
                single_records.append((mode, proc))
                if proc.returncode != 0:
                    continue
                candidates = sorted(
                    p
                    for p in work_dir.glob(f"{prefix_single.name}*")
                    if p.is_file() and p.suffix.lower() in image_suffixes
                )
                if candidates:
                    single_layer = candidates[0]
                    break
            if single_layer is not None:
                paired_layers.append((shot_idx, single_layer))
            else:
                detail = "; ".join(
                    f"{mode}:rc={proc.returncode}" for mode, proc in single_records
                )
                single_errors.append(f"shot {shot_idx}: no layer ({detail})")
        if paired_layers:
            return paired_layers
        listing = "\n".join(str(p.name) for p in all_after) or "(no files)"
        new_listing = "\n".join(str(p.name) for p in new_files) or "(no new files)"
        proc_listing = "\n\n".join(
            f"mode={mode}, returncode={proc.returncode}\nstdout:\n{proc.stdout}\nstderr:\n{proc.stderr}"
            for mode, proc in proc_records
        )
        raise RuntimeError(
            f"nona produced {len(layers)} coordinate layers for {len(shots)} shots.\n"
            f"{proc_listing}\n"
            f"single-shot fallback errors:\n" + "\n".join(single_errors) + "\n"
            f"new files:\n{new_listing}\n"
            f"all work-dir files:\n{listing}"
        )
    return [(idx, layer) for idx, layer in enumerate(layers[: len(shots)])]


def build_coordinate_map(
    metadata: dict,
    session_info: dict,
    out_path: Path,
    work_dir: Path | None = None,
) -> dict:
    import numpy as np  # type: ignore

    pto_path = Path(metadata["pto_path"]).expanduser()
    if not pto_path.exists():
        raise FileNotFoundError(f"PTO not found: {pto_path}")
    shots = [dict(item) for item in metadata.get("shots", [])]
    shot_by_name = session_info.get("shot_by_name") or {}
    for shot in shots:
        session_shot = shot_by_name.get(Path(shot["file"]).name)
        if session_shot:
            shot["ptz_pan"] = session_shot["actual_pan"]
            shot["ptz_tilt"] = session_shot["actual_tilt"]
            shot["file"] = session_shot.get("path") or shot["file"]

    intrinsics = session_info.get("intrinsics")
    if intrinsics is None:
        first = shots[0]
        w = int(first.get("image_width") or 1920)
        h = int(first.get("image_height") or 1080)
        hfov = float(first.get("hugin_hfov") or 50.0)
        fx = (w / 2.0) / math.tan(math.radians(hfov) / 2.0)
        intrinsics = {"fx": fx, "fy": fx, "cx": w / 2.0, "cy": h / 2.0}

    cleanup = False
    if work_dir is None:
        work_dir = Path(tempfile.mkdtemp(prefix="hugin_coord_map_"))
        cleanup = True
    else:
        work_dir.mkdir(parents=True, exist_ok=True)

    try:
        layer_items = _run_nona_coordinate_layers(pto_path, shots, work_dir)
        full_w = int(metadata.get("full_canvas", [0, 0])[0] or 0)
        full_h = int(metadata.get("full_canvas", [0, 0])[1] or 0)
        layer_infos = {}
        for _shot_idx, layer_path in layer_items:
            info = _tiff_layer_info(layer_path)
            layer_infos[str(layer_path)] = info
            if info.get("full_width"):
                full_w = max(full_w, int(info["full_width"]))
            if info.get("full_height"):
                full_h = max(full_h, int(info["full_height"]))
        if full_w <= 0 or full_h <= 0:
            raise RuntimeError("unable to determine full panorama canvas size for coordinate map")

        owner_full = np.full((full_h, full_w), -1, dtype=np.int16)
        source_x_full = np.full((full_h, full_w), np.nan, dtype=np.float32)
        source_y_full = np.full((full_h, full_w), np.nan, dtype=np.float32)
        best_score_full = np.full((full_h, full_w), np.inf, dtype=np.float32)
        coverage_count_full = np.zeros((full_h, full_w), dtype=np.uint16)

        for idx, layer_path in layer_items:
            shot = shots[idx]
            layer = _safe_imread(layer_path)
            if layer.ndim == 2:
                continue
            if layer.shape[2] < 3:
                continue
            h, w = layer.shape[:2]
            info = layer_infos.get(str(layer_path), {})
            off_x = int(info.get("offset_x") or 0)
            off_y = int(info.get("offset_y") or 0)
            dst_x0 = max(0, off_x)
            dst_y0 = max(0, off_y)
            src_x0 = max(0, -off_x)
            src_y0 = max(0, -off_y)
            dst_x1 = min(full_w, off_x + w)
            dst_y1 = min(full_h, off_y + h)
            if dst_x1 <= dst_x0 or dst_y1 <= dst_y0:
                continue
            src_x1 = src_x0 + (dst_x1 - dst_x0)
            src_y1 = src_y0 + (dst_y1 - dst_y0)
            layer_roi = layer[src_y0:src_y1, src_x0:src_x1]

            sw = int(shot.get("image_width") or 0)
            sh = int(shot.get("image_height") or 0)
            if sw <= 1 or sh <= 1:
                size = _image_size(Path(shot["file"]))
                if not size:
                    continue
                sw, sh = size

            arr = layer_roi.astype(np.float32)
            if arr.shape[2] >= 4:
                valid = arr[:, :, 3] > 1000.0
            else:
                valid = arr[:, :, 2] > 1000.0
            sx = arr[:, :, 0] / 65535.0 * float(sw - 1)
            sy = arr[:, :, 1] / 65535.0 * float(sh - 1)
            radial = (sx - float(intrinsics["cx"])) ** 2 + (sy - float(intrinsics["cy"])) ** 2
            coverage_roi = coverage_count_full[dst_y0:dst_y1, dst_x0:dst_x1]
            coverage_roi[valid] = np.minimum(
                coverage_roi[valid].astype(np.uint32) + 1,
                np.iinfo(np.uint16).max,
            ).astype(np.uint16)
            best_roi = best_score_full[dst_y0:dst_y1, dst_x0:dst_x1]
            update = valid & (radial < best_roi)
            owner_roi = owner_full[dst_y0:dst_y1, dst_x0:dst_x1]
            sx_roi = source_x_full[dst_y0:dst_y1, dst_x0:dst_x1]
            sy_roi = source_y_full[dst_y0:dst_y1, dst_x0:dst_x1]
            owner_roi[update] = idx
            sx_roi[update] = sx[update]
            sy_roi[update] = sy[update]
            best_roi[update] = radial[update]

        crop_left, crop_right, crop_top, crop_bottom = [int(v) for v in metadata.get("crop", [0, full_w, 0, full_h])]
        crop_left = max(0, min(full_w, crop_left))
        crop_right = max(crop_left, min(full_w, crop_right))
        crop_top = max(0, min(full_h, crop_top))
        crop_bottom = max(crop_top, min(full_h, crop_bottom))
        owner = owner_full[crop_top:crop_bottom, crop_left:crop_right]
        source_x = source_x_full[crop_top:crop_bottom, crop_left:crop_right]
        source_y = source_y_full[crop_top:crop_bottom, crop_left:crop_right]
        coverage_count = coverage_count_full[crop_top:crop_bottom, crop_left:crop_right]

        final_w, final_h = [int(v) for v in metadata.get("final_size", [owner.shape[1], owner.shape[0]])]
        if final_w > 0 and final_h > 0 and (owner.shape[1] != final_w or owner.shape[0] != final_h):
            import cv2  # type: ignore

            owner = cv2.resize(owner, (final_w, final_h), interpolation=cv2.INTER_NEAREST)
            source_x = cv2.resize(source_x, (final_w, final_h), interpolation=cv2.INTER_NEAREST)
            source_y = cv2.resize(source_y, (final_w, final_h), interpolation=cv2.INTER_NEAREST)
            coverage_count = cv2.resize(coverage_count, (final_w, final_h), interpolation=cv2.INTER_NEAREST)

        rendered_shot_indices = [int(idx) for idx, _layer_path in layer_items]
        shot_pan = np.array([float(s["ptz_pan"]) for s in shots], dtype=np.float32)
        shot_tilt = np.array([float(s["ptz_tilt"]) for s in shots], dtype=np.float32)
        shot_files = np.array([str(s["file"]) for s in shots])
        shot_width = np.array([int(s.get("image_width") or 0) for s in shots], dtype=np.int32)
        shot_height = np.array([int(s.get("image_height") or 0) for s in shots], dtype=np.int32)
        source_crop = (session_info.get("camera") or {}).get("source_crop") or {}
        source_crop_width_ratio = float(source_crop.get("width_ratio") or 1.0)
        source_crop_height_ratio = float(source_crop.get("height_ratio") or 1.0)
        trusted_core_half_ratio_original = float(
            source_crop.get("trusted_core_half_ratio_original") or 0.20
        )
        out_path.parent.mkdir(parents=True, exist_ok=True)
        np.savez_compressed(
            out_path,
            owner=owner,
            source_x=source_x,
            source_y=source_y,
            coverage_count=coverage_count,
            shot_pan=shot_pan,
            shot_tilt=shot_tilt,
            shot_files=shot_files,
            shot_width=shot_width,
            shot_height=shot_height,
            source_crop_width_ratio=np.array([source_crop_width_ratio], dtype=np.float32),
            source_crop_height_ratio=np.array([source_crop_height_ratio], dtype=np.float32),
            trusted_core_half_ratio_original=np.array(
                [trusted_core_half_ratio_original],
                dtype=np.float32,
            ),
            intrinsics=np.array(
                [intrinsics["fx"], intrinsics["fy"], intrinsics["cx"], intrinsics["cy"]],
                dtype=np.float64,
            ),
            metadata_json=json.dumps(
                {
                    "schema": "hugin_source_coordinate_map_v1",
                    "meta_path": metadata.get("meta_path"),
                    "pto_path": metadata.get("pto_path"),
                    "shape": [int(owner.shape[1]), int(owner.shape[0])],
                    "valid_pixels": int((owner >= 0).sum()),
                    "rendered_layer_count": len(layer_items),
                    "rendered_shot_indices": rendered_shot_indices,
                    "skipped_shot_indices": [
                        idx for idx in range(len(shots)) if idx not in set(rendered_shot_indices)
                    ],
                },
                ensure_ascii=False,
            ),
        )
        return {
            "map": str(out_path),
            "shape": [int(owner.shape[1]), int(owner.shape[0])],
            "valid_pixels": int((owner >= 0).sum()),
            "shots": len(shots),
            "rendered_layers": len(layer_items),
            "rendered_shot_indices": rendered_shot_indices,
        }
    finally:
        if cleanup:
            shutil.rmtree(work_dir, ignore_errors=True)


def convert_with_coordinate_map(map_path: Path, points: list[tuple[float, float]]) -> dict:
    import numpy as np  # type: ignore

    data = np.load(str(map_path), allow_pickle=False)
    owner = data["owner"]
    source_x = data["source_x"]
    source_y = data["source_y"]
    shot_pan = data["shot_pan"]
    shot_tilt = data["shot_tilt"]
    shot_files = data["shot_files"]
    fx, fy, cx, cy = [float(v) for v in data["intrinsics"]]
    h, w = owner.shape
    results = []
    for px, py in points:
        ix = int(round(px))
        iy = int(round(py))
        in_frame = 0 <= ix < w and 0 <= iy < h
        if not in_frame or int(owner[iy, ix]) < 0:
            results.append({"px": px, "py": py, "in_frame": in_frame, "has_source": False})
            continue
        idx = int(owner[iy, ix])
        sx = float(source_x[iy, ix])
        sy = float(source_y[iy, ix])
        pan, tilt = _pixel_to_absolute_ptz_with_intrinsics(
            sx,
            sy,
            cx,
            cy,
            fx,
            fy,
            float(shot_pan[idx]),
            float(shot_tilt[idx]),
        )
        results.append(
            {
                "输入类型": "全景图像素",
                "px": px,
                "py": py,
                "method": "coordinate_map_source_pixel",
                "pan": pan,
                "tilt": tilt,
                "in_frame": True,
                "has_source": True,
                "source": {
                    "owner_index": idx,
                    "shot_file": str(shot_files[idx]),
                    "shot_pan": float(shot_pan[idx]),
                    "shot_tilt": float(shot_tilt[idx]),
                    "source_px": sx,
                    "source_py": sy,
                },
                "中文摘要": (
                    f"全景像素({px:.1f},{py:.1f}) -> pan={pan:.2f}, tilt={tilt:.2f}; "
                    f"来源图: {Path(str(shot_files[idx])).name}; "
                    f"来源图像素=({sx:.1f},{sy:.1f}); "
                    f"来源拍摄角度=({float(shot_pan[idx]):.2f},{float(shot_tilt[idx]):.2f})"
                ),
            }
        )
    return {"points": results}


def _find_session_shot(session_info: dict, image: str) -> dict | None:
    image_path = Path(image).expanduser()
    shot_by_name = session_info.get("shot_by_name") or {}
    shot = shot_by_name.get(image_path.name)
    if shot:
        return dict(shot)

    shot_index = None
    index_match = re.fullmatch(r"(?:shot[_-]?)?0*(\d+)(?:\..*)?", image_path.name, re.I)
    if index_match:
        shot_index = int(index_match.group(1))
    if shot_index is not None:
        for name, candidate in shot_by_name.items():
            match = re.search(r"shot[_-]?0*(\d+)", name, re.I)
            if match and int(match.group(1)) == shot_index:
                return dict(candidate)

    image_resolved = str(image_path.resolve()) if image_path.exists() else str(image_path)
    for candidate in shot_by_name.values():
        if str(candidate.get("path")) == image_resolved:
            return dict(candidate)
    return None


def convert_single_image_pixels(
    session_path: Path,
    image: str,
    points: list[tuple[float, float]],
) -> dict:
    session_info = load_session_info(session_path)
    intrinsics = session_info.get("intrinsics")
    if not intrinsics:
        raise RuntimeError(f"session.json does not contain camera.new_camera_matrix: {session_path}")

    shot = _find_session_shot(session_info, image)
    if not shot:
        match = SHOT_RE.search(Path(image).name)
        if not match:
            raise RuntimeError(
                "unable to find this image in session.json, and filename does not contain pan/tilt: "
                f"{image}"
            )
        shot = {
            "actual_pan": float(match.group(1)),
            "actual_tilt": float(match.group(2)),
            "path": str(Path(image).expanduser()),
        }

    image_path = Path(str(shot.get("path") or image)).expanduser()
    image_size = _image_size(image_path) if image_path.exists() else None
    results = []
    for px, py in points:
        pan, tilt = _single_image_ptz_from_source(
            px,
            py,
            float(shot["actual_pan"]),
            float(shot["actual_tilt"]),
            intrinsics,
        )
        in_frame = True
        if image_size:
            in_frame = 0 <= px < image_size[0] and 0 <= py < image_size[1]
        results.append(
            {
                "输入类型": "单张原图像素",
                "px": px,
                "py": py,
                "method": "single_image_source_pixel",
                "pan": pan,
                "tilt": tilt,
                "in_frame": in_frame,
                "image": str(image_path),
                "image_size": list(image_size) if image_size else None,
                "shot_pan": float(shot["actual_pan"]),
                "shot_tilt": float(shot["actual_tilt"]),
                "intrinsics": {
                    "fx": float(intrinsics["fx"]),
                    "fy": float(intrinsics["fy"]),
                    "cx": float(intrinsics["cx"]),
                    "cy": float(intrinsics["cy"]),
                },
                "中文摘要": (
                    f"单图像素({px:.1f},{py:.1f}) -> pan={pan:.2f}, tilt={tilt:.2f}; "
                    f"单图: {image_path.name}; "
                    f"拍摄角度=({float(shot['actual_pan']):.2f},{float(shot['actual_tilt']):.2f})"
                ),
            }
        )
    return {"points": results}


def pixel_to_hugin(metadata: dict, px: float, py: float) -> tuple[float, float]:
    final_w, final_h = metadata["final_size"]
    full_w, full_h = metadata["full_canvas"]
    crop_left, crop_right, crop_top, crop_bottom = metadata["crop"]
    crop_w = crop_right - crop_left
    crop_h = crop_bottom - crop_top
    px_per_deg = float(metadata["hugin_px_per_deg"])

    x_full = crop_left + (float(px) + 0.5) * crop_w / final_w
    y_full = crop_top + (float(py) + 0.5) * crop_h / final_h
    yaw = (x_full - full_w / 2.0) / px_per_deg
    pitch = (full_h / 2.0 - y_full) / px_per_deg
    return yaw, pitch


def pixel_to_ptz(metadata: dict, px: float, py: float, method: str = "auto") -> dict:
    yaw, pitch = pixel_to_hugin(metadata, px, py)
    source_solution = None
    calibrated = _manual_calibrated_ptz(metadata, yaw, pitch)
    if method == "source":
        source_solution = _source_pixel_solution(metadata, yaw, pitch)
    if method == "source" and source_solution is not None:
        pan = source_solution["pan"]
        tilt = source_solution["tilt"]
        method = "source_pixel"
    elif method in ("auto", "calibrated") and calibrated is not None:
        pan, tilt = calibrated
        method = "manual_calibrated"
    else:
        pan, tilt = _base_model_ptz(metadata, yaw, pitch)
        method = "affine_fallback"
    final_w, final_h = metadata["final_size"]
    result = {
        "px": float(px),
        "py": float(py),
        "hugin_yaw": yaw,
        "hugin_pitch": pitch,
        "method": method,
        "pan": pan,
        "tilt": tilt,
        "in_frame": 0 <= px < final_w and 0 <= py < final_h,
        "tilt_mapping_reliable": bool(metadata.get("quality", {}).get("tilt_mapping_reliable")),
    }
    if source_solution is not None:
        result["source"] = {
            key: value
            for key, value in source_solution.items()
            if key != "candidates"
        }
        result["source_candidates"] = source_solution.get("candidates", [])
    return result


def parse_points(values: list[str]) -> list[tuple[float, float]]:
    points = []
    for value in values:
        for part in re.split(r"\s+", value.strip()):
            if not part:
                continue
            pieces = part.split(",")
            if len(pieces) != 2:
                raise ValueError(f"point must look like x,y: {part}")
            points.append((float(pieces[0]), float(pieces[1])))
    return points


def cmd_build(args) -> int:
    final_size = parse_size(args.final_size) if args.final_size else None
    metadata = build_metadata(
        pto_path=Path(args.pto).expanduser().resolve(),
        final_image=Path(args.final_image).expanduser().resolve() if args.final_image else None,
        final_size=final_size,
    )
    out = Path(args.out).expanduser().resolve()
    write_json(out, metadata)
    print(json.dumps({"metadata": str(out), "quality": metadata["quality"]}, ensure_ascii=False, indent=2))
    return 0


def cmd_convert(args) -> int:
    metadata = read_json(Path(args.meta).expanduser().resolve())
    results = [pixel_to_ptz(metadata, x, y, method=args.method) for x, y in parse_points(args.point)]
    print(json.dumps({"points": results, "quality": metadata.get("quality", {})}, ensure_ascii=False, indent=2))
    return 0


def cmd_calibrate(args) -> int:
    meta_path = Path(args.meta).expanduser().resolve()
    metadata = read_json(meta_path)
    px, py = parse_pair(args.point, "--point")
    actual_pan, actual_tilt = parse_pair(args.actual, "--actual")
    existing = metadata.get("manual_calibration", {}).get("anchors", [])
    anchors = [
        {
            "px": float(item["px"]),
            "py": float(item["py"]),
            "actual_pan": float(item["actual_pan"]),
            "actual_tilt": float(item["actual_tilt"]),
        }
        for item in existing
    ]
    anchors.append({"px": px, "py": py, "actual_pan": actual_pan, "actual_tilt": actual_tilt})
    metadata["manual_calibration"] = {"anchors": anchors}
    metadata = rebuild_manual_calibration(metadata)
    out = Path(args.out).expanduser().resolve() if args.out else meta_path
    write_json(out, metadata)
    check = pixel_to_ptz(metadata, px, py, method="calibrated")
    print(
        json.dumps(
            {
                "metadata": str(out),
                "manual_calibration": metadata.get("manual_calibration"),
                "check_point": check,
            },
            ensure_ascii=False,
            indent=2,
        )
    )
    return 0


def cmd_build_map(args) -> int:
    meta_path = Path(args.meta).expanduser().resolve()
    metadata = read_json(meta_path)
    metadata["meta_path"] = str(meta_path)
    session_path = resolve_session_json(args.session)
    session_info = load_session_info(session_path)
    out = Path(args.out).expanduser().resolve()
    work_dir = Path(args.work_dir).expanduser().resolve() if args.work_dir else None
    result = build_coordinate_map(metadata, session_info, out, work_dir=work_dir)
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0


def cmd_convert_map(args) -> int:
    result = convert_with_coordinate_map(
        Path(args.map).expanduser().resolve(),
        parse_points(args.point),
    )
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0


def cmd_convert_image(args) -> int:
    session_path = resolve_session_json(args.session)
    if session_path is None:
        raise RuntimeError(f"session.json not found from --session: {args.session}")
    result = convert_single_image_pixels(
        session_path,
        args.image,
        parse_points(args.point),
    )
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0


def parse_args(argv=None):
    parser = argparse.ArgumentParser(
        description="Build/use Hugin equirectangular coordinate metadata.",
        formatter_class=argparse.ArgumentDefaultsHelpFormatter,
    )
    sub = parser.add_subparsers(dest="command", required=True)

    build = sub.add_parser("build", help="Build coordinate metadata from a final Hugin .pto")
    build.add_argument("--pto", required=True, help="Final Hugin .pto, usually 05_*.pto")
    build.add_argument("--final-image", help="Final image shown to the user")
    build.add_argument("--final-size", help="Final image size if it cannot be read, for example 3000x514")
    build.add_argument("--out", required=True, help="Output metadata JSON")
    build.set_defaults(func=cmd_build)

    convert = sub.add_parser("convert", help="Convert panorama pixels to PTZ angles")
    convert.add_argument("--meta", required=True, help="Metadata JSON generated by the build command")
    convert.add_argument(
        "--point",
        action="append",
        required=True,
        help='Pixel point as "x,y". Repeat or pass a space-separated list.',
    )
    convert.add_argument(
        "--method",
        choices=["auto", "calibrated", "source", "affine"],
        default="auto",
        help="Conversion method. auto uses manual calibration when present; source is experimental.",
    )
    convert.set_defaults(func=cmd_convert)

    calibrate = sub.add_parser("calibrate", help="Add a measured pixel->PTZ anchor to metadata")
    calibrate.add_argument("--meta", required=True, help="Metadata JSON to update")
    calibrate.add_argument("--point", required=True, help='Panorama pixel as "x,y"')
    calibrate.add_argument("--actual", required=True, help='Measured PTZ angle as "pan,tilt"')
    calibrate.add_argument("--out", help="Output metadata JSON; default overwrites --meta")
    calibrate.set_defaults(func=cmd_calibrate)

    build_map = sub.add_parser("build-map", help="Build source-pixel coordinate_map.npz via Hugin/nona")
    build_map.add_argument("--meta", required=True, help="Metadata JSON generated by build/Hugin experiment")
    build_map.add_argument("--session", required=True, help="Session directory or session.json")
    build_map.add_argument("--out", required=True, help="Output coordinate_map.npz")
    build_map.add_argument("--work-dir", help="Keep intermediate coordinate layers in this directory")
    build_map.set_defaults(func=cmd_build_map)

    convert_map = sub.add_parser("convert-map", help="Convert pixels using coordinate_map.npz")
    convert_map.add_argument("--map", required=True, help="coordinate_map.npz generated by build-map")
    convert_map.add_argument(
        "--point",
        action="append",
        required=True,
        help='Pixel point as "x,y". Repeat or pass a space-separated list.',
    )
    convert_map.set_defaults(func=cmd_convert_map)

    convert_image = sub.add_parser("convert-image", help="Convert a single source image pixel to PTZ")
    convert_image.add_argument("--session", required=True, help="Session directory or session.json")
    convert_image.add_argument("--image", required=True, help="Single source image path or basename")
    convert_image.add_argument(
        "--point",
        action="append",
        required=True,
        help='Pixel point in the single image as "x,y". Repeat or pass a space-separated list.',
    )
    convert_image.set_defaults(func=cmd_convert_image)
    return parser.parse_args(argv)


def main(argv=None) -> int:
    args = parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    raise SystemExit(main())
