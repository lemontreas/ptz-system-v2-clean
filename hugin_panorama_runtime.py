from __future__ import annotations

import json
import math
import subprocess
import time
import os
import re
import shutil
import threading
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import cv2
import numpy as np

import hugin_coordinate_mapper as mapper
from pmap_utils import PMAP_INVALID_INT16, load_pmap, read_pmap_header

VISUAL_MATCH_RELIABLE_SCORE = 0.85
DEFAULT_TRUSTED_CORE_HALF_RATIO = 0.20
DEFAULT_SOURCE_STITCH_OVERLAP = 0.30
DEFAULT_SOURCE_CROP_WIDTH_RATIO = 0.55
DEFAULT_SOURCE_CROP_HEIGHT_RATIO = 1.0
DEFAULT_CORRECTION_GRID_STEP = 24
DEFAULT_CORRECTION_PATCH = 41
DEFAULT_CORRECTION_RADIUS = 64
DEFAULT_CORRECTION_FALLBACK_RADIUS = 240
DEFAULT_CORRECTION_MAX_SHIFT = 240
DEFAULT_CORRECTION_MAX_POINTS = 12000
DEFAULT_CORRECTION_WORKERS = 4
DEFAULT_CORRECTION_SCORE = 0.88
DEFAULT_CORRECTION_MIN_TEXTURE_STD = 10.0
DEFAULT_CORRECTION_LOOKUP_RADIUS_CELLS = 1
DEFAULT_CORRECTION_ALLOW_CROSS_SOURCE = False
DEFAULT_CORRECTION_OVERLAP_ONLY = False
DEFAULT_CORRECTION_ADAPTIVE_REFINEMENT = True
DEFAULT_CORRECTION_ADAPTIVE_EDGE_NORM = 0.75
DEFAULT_CORRECTION_PYRAMID_SCALE = 0.5
DEFAULT_CORRECTION_COARSE_PATCH = 161
DEFAULT_CORRECTION_FINE_RADIUS = 48
DEFAULT_VISUAL_REFINE_RADIUS = 96
DEFAULT_VISUAL_REFINE_PATCH = 41

_PMAP_CONTEXT_CACHE: dict[tuple[str, int, int, str, int, int], "_HuginPmapBatchContext"] = {}
_PMAP_CONTEXT_CACHE_MAX = 4
PMAP_REVERSE_BUCKET_SIZE = 16


def resolve_command(name: str) -> str | None:
    env_name = "HUGIN_" + re.sub(r"[^A-Za-z0-9]+", "_", name).upper() + "_BIN"
    env_value = os.getenv(env_name)
    if env_value:
        candidate = Path(env_value).expanduser()
        if candidate.exists() and os.access(candidate, os.X_OK):
            return str(candidate)
        return str(candidate)

    search_dirs = []
    env_dir = os.getenv("HUGIN_BIN_DIR")
    if env_dir:
        search_dirs.append(Path(env_dir).expanduser())
    if name == "nona":
        custom_nona = Path("/home/ultiwill/bin/nona-pixelmap").expanduser()
        if custom_nona.exists() and os.access(custom_nona, os.X_OK):
            return str(custom_nona)
    search_dirs.extend(
        [
            Path("/usr/bin"),
            Path("/usr/local/bin"),
            Path("/snap/bin"),
            Path("/opt/homebrew/bin"),
        ]
    )
    for directory in search_dirs:
        candidate = directory / name
        if candidate.exists() and os.access(candidate, os.X_OK):
            return str(candidate)

    path = os.getenv("PATH", "")
    for directory in path.split(os.pathsep):
        if not directory:
            continue
        candidate = Path(directory) / name
        if candidate.exists() and os.access(candidate, os.X_OK):
            return str(candidate)
    return None


def require_commands(names: list[str]) -> dict[str, str]:
    resolved = {}
    missing = []
    for name in names:
        command = resolve_command(name)
        if command:
            resolved[name] = command
        else:
            missing.append(name)
    if missing:
        raise RuntimeError(
            "missing Hugin commands: "
            + ", ".join(missing)
            + f"; PATH={os.getenv('PATH', '')}; HUGIN_BIN_DIR={os.getenv('HUGIN_BIN_DIR', '')}"
        )
    return resolved


def prepend_command_dirs_to_path(commands: dict[str, str]) -> None:
    dirs = []
    for command in commands.values():
        directory = str(Path(command).parent)
        if directory not in dirs:
            dirs.append(directory)
    current = os.getenv("PATH", "")
    existing = current.split(os.pathsep) if current else []
    merged = dirs + [item for item in existing if item not in dirs]
    os.environ["PATH"] = os.pathsep.join(merged)


def _run(cmd: list[str], log_path: Path) -> None:
    log_path.parent.mkdir(parents=True, exist_ok=True)
    proc = subprocess.run(
        cmd,
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        check=False,
    )
    log_path.write_text(
        "COMMAND: " + " ".join(cmd) + "\n\nSTDOUT:\n" + proc.stdout + "\n\nSTDERR:\n" + proc.stderr,
        encoding="utf-8",
    )
    if proc.returncode != 0:
        raise RuntimeError(f"Hugin command failed ({proc.returncode}): {' '.join(cmd)}; see {log_path}")


def _command_help_contains(command: str, needle: str) -> bool:
    try:
        proc = subprocess.run(
            [command, "--help"],
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            check=False,
            timeout=10,
        )
    except Exception:
        return False
    return needle in (proc.stdout + proc.stderr)


def _build_pmap_enabled() -> bool:
    raw = os.getenv("HUGIN_BUILD_PMAP")
    if raw is not None:
        return str(raw).strip().lower() not in {"0", "false", "no", "off"}
    return _env_bool("HUGIN_BUILD_NONA_PIXEL_MAP", True)


def _build_coordinate_map_enabled() -> bool:
    raw = os.getenv("HUGIN_BUILD_COORDINATE_MAP")
    if raw is not None:
        return str(raw).strip().lower() not in {"0", "false", "no", "off"}
    if _env_bool("HUGIN_PMAP_ONLY", False):
        return False
    return False


def _run_nona_pmap(
    nona_bin: str,
    pto_path: Path,
    out_pmap: Path,
    work_dir: Path,
    log_path: Path,
) -> dict:
    if not _build_pmap_enabled():
        return {"enabled": False, "skipped": True, "reason": "disabled_by_env"}

    if not _command_help_contains(nona_bin, "--pixel-map"):
        message = f"nona does not advertise --pixel-map: {nona_bin}"
        if _env_bool("HUGIN_REQUIRE_NONA_PIXEL_MAP", True):
            raise RuntimeError(message)
        return {
            "enabled": True,
            "skipped": True,
            "reason": "unsupported_nona",
            "nona": nona_bin,
        }

    work_dir.mkdir(parents=True, exist_ok=True)
    out_pmap.parent.mkdir(parents=True, exist_ok=True)
    prefix = work_dir / "layer"
    _run(
        [nona_bin, "--pixel-map", str(out_pmap), "-m", "TIFF_m", "-o", str(prefix), str(pto_path)],
        log_path,
    )

    if not out_pmap.exists() or out_pmap.stat().st_size <= 0:
        raise RuntimeError(f"nona PMAP was not created: {out_pmap}")
    header = read_pmap_header(out_pmap)

    if not _env_bool("HUGIN_KEEP_PMAP_LAYERS", _env_bool("HUGIN_KEEP_NONA_PIXEL_MAP_LAYERS", False)):
        for layer in work_dir.glob("layer*"):
            if layer.is_file() and layer != out_pmap:
                layer.unlink(missing_ok=True)

    return {
        "enabled": True,
        "skipped": False,
        "path": str(out_pmap),
        "nona": nona_bin,
        "size_bytes": int(out_pmap.stat().st_size),
        "schema": header.get("metadata", {}).get("schema"),
        "shape": [int(header["width"]), int(header["height"])],
        "metadata": header.get("metadata", {}),
    }


def _write_image(path: Path, image) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    ok = cv2.imwrite(str(path), image)
    if not ok:
        raise RuntimeError(f"failed to write image: {path}")


def _read_image(path: Path):
    data = np.frombuffer(path.read_bytes(), dtype=np.uint8)
    img = cv2.imdecode(data, cv2.IMREAD_COLOR)
    if img is None:
        raise RuntimeError(f"failed to read image: {path}")
    return img


def _source_crop_settings() -> dict:
    raw_core = os.getenv("PANORAMA_TRUSTED_CORE_HALF_RATIO") or os.getenv("HUGIN_TRUSTED_CORE_HALF_RATIO")
    try:
        default_core_half = float(raw_core) if raw_core is not None else DEFAULT_TRUSTED_CORE_HALF_RATIO
    except Exception:
        default_core_half = DEFAULT_TRUSTED_CORE_HALF_RATIO
    raw_overlap = os.getenv("PANORAMA_TRUSTED_OVERLAP")
    try:
        default_overlap = float(raw_overlap) if raw_overlap is not None else DEFAULT_SOURCE_STITCH_OVERLAP
    except Exception:
        default_overlap = DEFAULT_SOURCE_STITCH_OVERLAP
    core_half = _env_float(
        "HUGIN_SOURCE_TRUSTED_CORE_HALF_RATIO",
        default_core_half,
        0.01,
        0.5,
    )
    overlap = _env_float(
        "HUGIN_SOURCE_STITCH_OVERLAP",
        default_overlap,
        0.0,
        0.9,
    )
    width_ratio = _env_float("HUGIN_SOURCE_CROP_WIDTH_RATIO", DEFAULT_SOURCE_CROP_WIDTH_RATIO, 0.05, 1.0)
    height_ratio = _env_float(
        "HUGIN_SOURCE_CROP_HEIGHT_RATIO",
        DEFAULT_SOURCE_CROP_HEIGHT_RATIO,
        0.05,
        1.0,
    )
    return {
        "trusted_core_half_ratio": core_half,
        "stitch_overlap": overlap,
        "width_ratio": width_ratio,
        "height_ratio": height_ratio,
    }


def _center_crop_for_stitch(image, camera_matrix, settings: dict):
    h, w = image.shape[:2]
    crop_w = max(1, min(w, int(round(w * float(settings["width_ratio"])))))
    crop_h = max(1, min(h, int(round(h * float(settings["height_ratio"])))))
    x0 = max(0, (w - crop_w) // 2)
    y0 = max(0, (h - crop_h) // 2)
    x1 = x0 + crop_w
    y1 = y0 + crop_h
    cropped = image[y0:y1, x0:x1].copy()
    adjusted_k = np.asarray(camera_matrix, dtype=float).copy()
    adjusted_k[0, 2] -= float(x0)
    adjusted_k[1, 2] -= float(y0)
    return cropped, adjusted_k, {
        "x": int(x0),
        "y": int(y0),
        "width": int(crop_w),
        "height": int(crop_h),
        "original_width": int(w),
        "original_height": int(h),
        "width_ratio": float(crop_w / max(1, w)),
        "height_ratio": float(crop_h / max(1, h)),
        "trusted_core_half_ratio_original": float(settings["trusted_core_half_ratio"]),
        "stitch_overlap": float(settings["stitch_overlap"]),
    }


def _find_hugin_output(work_dir: Path, prefix: Path) -> Path:
    candidates = []
    for pattern in (f"{prefix.name}*", "*.tif", "*.tiff", "*.jpg", "*.jpeg", "*.png"):
        candidates.extend(p for p in work_dir.glob(pattern) if p.is_file())
    if not candidates:
        raise RuntimeError(f"Hugin did not produce an output image in {work_dir}")
    return max(candidates, key=lambda p: p.stat().st_mtime)


def _copy_hugin_output_to_jpg(found: Path, final_path: Path) -> None:
    img = _read_image(found)
    _write_image(final_path, img)


def _env_bool(name: str, default: bool) -> bool:
    raw = os.getenv(name)
    if raw is None:
        return default
    return str(raw).strip().lower() not in {"0", "false", "no", "off"}


def _env_int(name: str, default: int, min_value: int, max_value: int) -> int:
    try:
        value = int(os.getenv(name, str(default)))
    except Exception:
        value = default
    return int(np.clip(value, min_value, max_value))


def _env_float(name: str, default: float, min_value: float, max_value: float) -> float:
    try:
        value = float(os.getenv(name, str(default)))
    except Exception:
        value = default
    return float(np.clip(value, min_value, max_value))


def _odd_at_least(value: int, minimum: int = 3) -> int:
    value = max(int(value), int(minimum))
    if value % 2 == 0:
        value += 1
    return value


def _autooptimiser_args() -> list[str]:
    raw = os.getenv("HUGIN_AUTOOPTIMISER_ARGS")
    if raw:
        args = [item for item in raw.split() if item]
        if args:
            return args
    return ["-a", "-m"]


def _hugin_geometry_mode() -> str:
    mode = os.getenv("HUGIN_GEOMETRY_MODE", "optimized").strip().lower()
    if mode in {"locked", "ptz_locked", "seeded", "ptz"}:
        return "ptz_locked"
    return "optimized"


def _correction_path_from_map(map_path: Path) -> Path:
    suffix = "_coordinate_map.npz"
    if map_path.name.endswith(suffix):
        return map_path.with_name(map_path.name[: -len(suffix)] + "_correction_map.npz")
    return map_path.with_name(map_path.stem + "_correction_map.npz")


def _mapping_export_dir() -> Path | None:
    raw = os.getenv("HUGIN_MAPPING_EXPORT_DIR")
    if raw is None:
        if os.name == "nt":
            return None
        raw = "/home/ultiwill/hugin"
    if str(raw).strip().lower() in {"", "0", "false", "no", "off"}:
        return None
    return Path(raw).expanduser()


def _copy_bundle_file(src: Path, dst: Path) -> str | None:
    if not src or not src.exists():
        return None
    dst.parent.mkdir(parents=True, exist_ok=True)
    shutil.copy2(src, dst)
    return str(dst)


def _export_mapping_bundle(
    final_image: Path,
    coordinate_meta: Path,
    map_path: Path | None,
    correction_path: Path | None,
    pmap_path: Path | None,
    session_path: Path,
    pto_path: Path,
    hugin_output: Path,
    image_paths: list[Path],
    coordinate_metadata: dict,
    run_meta: dict,
) -> dict | None:
    export_root = _mapping_export_dir()
    if export_root is None:
        return None

    bundle_dir = export_root / final_image.stem
    files: dict[str, object] = {}
    files["panorama_image"] = _copy_bundle_file(final_image, bundle_dir / "panorama.jpg")
    if map_path is not None and map_path.exists():
        files["coordinate_map"] = _copy_bundle_file(map_path, bundle_dir / "coordinate_map.npz")
    else:
        files["coordinate_map"] = None
    files["coordinate_meta"] = _copy_bundle_file(coordinate_meta, bundle_dir / "coordinate_meta.json")
    if correction_path is not None and correction_path.exists():
        files["correction_map"] = _copy_bundle_file(correction_path, bundle_dir / "correction_map.npz")
    else:
        files["correction_map"] = None
    if pmap_path is not None and pmap_path.exists():
        files["pmap"] = _copy_bundle_file(pmap_path, bundle_dir / "pixel_map.pmap")
    else:
        files["pmap"] = None
    files["session_json"] = _copy_bundle_file(session_path, bundle_dir / "session.json")
    files["final_pto"] = _copy_bundle_file(pto_path, bundle_dir / "pto" / pto_path.name)
    files["hugin_output"] = _copy_bundle_file(hugin_output, bundle_dir / "hugin_output" / hugin_output.name)

    source_files = []
    for src in image_paths:
        copied = _copy_bundle_file(src, bundle_dir / "sources" / src.name)
        if copied:
            source_files.append(copied)
    files["source_images"] = source_files

    probe_script = Path(__file__).with_name("hugin_mapping_probe.py")
    files["probe_script"] = _copy_bundle_file(probe_script, bundle_dir / "hugin_mapping_probe.py")
    pmap_probe_script = Path(__file__).parent / "hugin" / "hugin_pto_math_mapper.py"
    files["pmap_probe_script"] = _copy_bundle_file(pmap_probe_script, bundle_dir / "hugin_pto_math_mapper.py")
    pmap_utils_script = Path(__file__).with_name("pmap_utils.py")
    files["pmap_utils"] = _copy_bundle_file(pmap_utils_script, bundle_dir / "pmap_utils.py")
    readme = Path(__file__).with_name("HUGIN_MAPPING_BUNDLE_README.md")
    files["readme"] = _copy_bundle_file(readme, bundle_dir / "README.md")

    run_meta_path = bundle_dir / "panorama_meta.json"
    run_meta_path.write_text(json.dumps(run_meta, ensure_ascii=False, indent=2), encoding="utf-8")
    files["panorama_meta"] = str(run_meta_path)

    manifest = {
        "schema": "hugin_mapping_bundle_v1",
        "created_at": time.strftime("%Y-%m-%d %H:%M:%S", time.localtime()),
        "bundle_dir": str(bundle_dir),
        "source_session_dir": str(session_path.parent),
        "files": files,
        "coordinate_summary": {
            "final_size": coordinate_metadata.get("final_size"),
            "full_canvas": coordinate_metadata.get("full_canvas"),
            "crop": coordinate_metadata.get("crop"),
            "crop_size": coordinate_metadata.get("crop_size"),
            "resize_scale": coordinate_metadata.get("resize_scale"),
            "projection": coordinate_metadata.get("projection"),
            "projection_id": coordinate_metadata.get("projection_id"),
        },
        "usage": [
            "cd " + str(bundle_dir),
            "python hugin_mapping_probe.py info --bundle .",
            "python hugin_mapping_probe.py pano --bundle . 2600.7,268.5",
            "python hugin_mapping_probe.py source --bundle . 4 694.6,449.8",
            "python hugin_pto_math_mapper.py --root . --pto pto/05_output.pto --panorama panorama.jpg point 2600,268 --mode pmap --pixel-map pixel_map.pmap",
        ],
    }
    manifest_path = bundle_dir / "manifest.json"
    manifest_path.write_text(json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8")
    return {
        "bundle_dir": str(bundle_dir),
        "manifest": str(manifest_path),
        "probe_script": files.get("probe_script"),
    }


def _replace_pto_param(line: str, key: str, value: float) -> str:
    token = f"{key}{value:.10f}".rstrip("0").rstrip(".")
    pattern = rf"(?<!\S){re.escape(key)}[-+]?(?:\d+(?:\.\d*)?|\.\d+)(?:[eE][-+]?\d+)?"
    if re.search(pattern, line):
        return re.sub(pattern, token, line, count=1)
    return line + " " + token


def _pto_image_width(line: str) -> int | None:
    match = re.search(r"(?<!\S)w(?P<value>\d+)(?=\s|$)", line)
    if not match:
        return None
    try:
        return int(match.group("value"))
    except Exception:
        return None


def _seed_pto_with_ptz_angles(
    src_path: Path,
    dst_path: Path,
    shots_meta: list[dict],
    camera_matrix,
    pan_range: tuple[float, float],
) -> None:
    yaw_center = (float(pan_range[0]) + float(pan_range[1])) / 2.0
    k = np.asarray(camera_matrix, dtype=float)
    fx = float(k[0, 0])
    image_idx = 0
    out_lines = []
    for line in src_path.read_text(encoding="utf-8", errors="replace").splitlines():
        if line.startswith("i ") and image_idx < len(shots_meta):
            shot = shots_meta[image_idx]
            width = _pto_image_width(line)
            if width and fx > 1e-6:
                hfov = math.degrees(2.0 * math.atan(float(width) / (2.0 * fx)))
                line = _replace_pto_param(line, "v", hfov)
            yaw = float(shot["pan"]) - yaw_center
            pitch = float(shot["tilt"])
            line = _replace_pto_param(line, "y", yaw)
            line = _replace_pto_param(line, "p", pitch)
            line = _replace_pto_param(line, "r", 0.0)
            image_idx += 1
        out_lines.append(line)
    dst_path.write_text("\n".join(out_lines) + "\n", encoding="utf-8")
    if image_idx != len(shots_meta):
        raise RuntimeError(f"seeded {image_idx} Hugin image lines, expected {len(shots_meta)}")


def build_hugin_panorama_from_shots(
    shots_meta: list[dict],
    new_camera_matrix,
    captures_dir: Path,
    pan_range: tuple[float, float],
    tilt_range: tuple[float, float],
    range_source=None,
) -> dict:
    commands = require_commands(["pto_gen", "cpfind", "autooptimiser", "pano_modify", "hugin_executor", "nona"])
    prepend_command_dirs_to_path(commands)
    timings = {}
    total_t0 = time.perf_counter()

    def timed(label: str, func, *args, **kwargs):
        t0 = time.perf_counter()
        try:
            return func(*args, **kwargs)
        finally:
            timings[label] = round(time.perf_counter() - t0, 3)

    ts = time.strftime("%Y%m%d_%H%M%S", time.localtime())
    session_dir = captures_dir / f"hugin_pano_{ts}"
    undistorted_dir = session_dir / "undistorted"
    hugin_dir = session_dir / "hugin"
    pto_dir = hugin_dir / "pto"
    log_dir = hugin_dir / "logs"
    final_dir = session_dir / "final"
    for path in (undistorted_dir, pto_dir, log_dir, final_dir):
        path.mkdir(parents=True, exist_ok=True)

    source_crop_settings = _source_crop_settings()
    adjusted_camera_matrix = np.asarray(new_camera_matrix, dtype=float)
    source_crop = None
    session_shots = []
    image_paths = []
    for idx, shot in enumerate(shots_meta, start=1):
        pan = float(shot["pan"])
        tilt = float(shot["tilt"])
        image_path = undistorted_dir / f"shot_{idx:03d}_pan{pan:.2f}_tilt{tilt:.2f}.jpg"
        cropped_img, cropped_k, crop_info = _center_crop_for_stitch(
            shot["img"],
            new_camera_matrix,
            source_crop_settings,
        )
        if source_crop is None:
            source_crop = crop_info
            adjusted_camera_matrix = cropped_k
        timed(f"write_shot_{idx:03d}", _write_image, image_path, cropped_img)
        image_paths.append(image_path)
        session_shots.append(
            {
                "index": idx,
                "target_pan": pan,
                "target_tilt": tilt,
                "actual_pan": pan,
                "actual_tilt": tilt,
                "undistorted_path": str(image_path.relative_to(session_dir)),
                "image_width": int(cropped_img.shape[1]),
                "image_height": int(cropped_img.shape[0]),
            }
        )

    session_json = {
        "created_at": ts,
        "image_set": "undistorted",
        "camera": {
            "new_camera_matrix": np.asarray(adjusted_camera_matrix, dtype=float).tolist(),
            "original_new_camera_matrix": np.asarray(new_camera_matrix, dtype=float).tolist(),
            "source_crop": source_crop,
        },
        "shots": session_shots,
    }
    session_path = session_dir / "session.json"
    session_path.write_text(json.dumps(session_json, ensure_ascii=False, indent=2), encoding="utf-8")

    pto_01 = pto_dir / "01_initial.pto"
    pto_01_seeded = pto_dir / "01_seeded_angles.pto"
    pto_02 = pto_dir / "02_control_points.pto"
    pto_03 = pto_dir / "03_cleaned.pto"
    pto_04 = pto_dir / "04_optimized.pto"
    pto_05 = pto_dir / "05_output.pto"
    prefix = hugin_dir / "hugin_result"

    timed("01_pto_gen", _run, [commands["pto_gen"], "-o", str(pto_01), *[str(p) for p in image_paths]], log_dir / "01_pto_gen.log")
    timed("01_seed_ptz_angles", _seed_pto_with_ptz_angles, pto_01, pto_01_seeded, shots_meta, adjusted_camera_matrix, pan_range)
    geometry_mode = _hugin_geometry_mode()
    optimiser_args = []
    pano_project_input = pto_01_seeded
    if geometry_mode == "optimized":
        timed("02_cpfind", _run, [commands["cpfind"], "--multirow", "-o", str(pto_02), str(pto_01_seeded)], log_dir / "02_cpfind.log")
        optimise_input = pto_02
        cpclean = resolve_command("cpclean")
        if cpclean:
            timed("03_cpclean", _run, [cpclean, "-o", str(pto_03), str(pto_02)], log_dir / "03_cpclean.log")
            optimise_input = pto_03
        optimiser_args = _autooptimiser_args()
        timed("04_autooptimiser", _run, [commands["autooptimiser"], *optimiser_args, "-o", str(pto_04), str(optimise_input)], log_dir / "04_autooptimiser.log")
        pano_project_input = pto_04
    timed(
        "05_pano_modify",
        _run,
        [commands["pano_modify"], "--projection", "2", "--canvas", "AUTO", "--crop", "AUTO", "-o", str(pto_05), str(pano_project_input)],
        log_dir / "05_pano_modify.log",
    )
    timed("06_hugin_executor", _run, [commands["hugin_executor"], "--stitching", "--prefix", str(prefix), str(pto_05)], log_dir / "06_hugin_executor.log")

    pmap_path = final_dir / "pixel_map.pmap"
    pmap_summary = None
    pmap_error = None
    try:
        pmap_summary = timed(
            "07_nona_pmap",
            _run_nona_pmap,
            commands["nona"],
            pto_05,
            pmap_path,
            hugin_dir / "pmap",
            log_dir / "07_nona_pmap.log",
        )
    except Exception as exc:
        pmap_error = str(exc)
        if _env_bool("HUGIN_REQUIRE_NONA_PIXEL_MAP", True):
            raise

    hugin_output = timed("find_hugin_output", _find_hugin_output, hugin_dir, prefix)
    jpg_name = f"panorama_hugin_pan{pan_range[0]:.2f}-{pan_range[1]:.2f}_tilt{tilt_range[0]:.2f}-{tilt_range[1]:.2f}_{ts}.jpg"
    final_image = final_dir / jpg_name
    image_rel = final_image.relative_to(captures_dir).as_posix()
    timed("copy_hugin_output_to_jpg", _copy_hugin_output_to_jpg, hugin_output, final_image)

    coordinate_meta = final_image.with_name(final_image.stem + "_coordinate_meta.json")
    metadata = timed("build_coordinate_metadata", mapper.build_metadata, pto_05.resolve(), final_image.resolve())
    timed("write_coordinate_metadata", mapper.write_json, coordinate_meta, metadata)

    coordinate_map_enabled = _build_coordinate_map_enabled()
    map_path = final_image.with_name(final_image.stem + "_coordinate_map.npz") if coordinate_map_enabled else None
    coordinate_map_summary = None
    correction_path = _correction_path_from_map(map_path) if map_path is not None else None
    correction_summary = None
    correction_error = None
    if coordinate_map_enabled and map_path is not None:
        session_info = mapper.load_session_info(session_path)
        coordinate_map_summary = timed("build_coordinate_map", mapper.build_coordinate_map, metadata, session_info, map_path)
    else:
        coordinate_map_summary = {
            "enabled": False,
            "skipped": True,
            "reason": "disabled_by_env",
        }
    if map_path is not None and _env_bool("HUGIN_BUILD_CORRECTION_MAP", True):
        try:
            correction_summary = timed("build_correction_map", build_correction_map, map_path, correction_path)
        except Exception as exc:
            correction_error = str(exc)

    img = timed("read_final_image", _read_image, final_image)
    canvas_h, canvas_w = img.shape[:2]
    timings["total"] = round(time.perf_counter() - total_t0, 3)
    meta = {
        "backend": "hugin",
        "pan_range": [float(pan_range[0]), float(pan_range[1])],
        "tilt_range": [float(tilt_range[0]), float(tilt_range[1])],
        "canvas_size": [int(canvas_w), int(canvas_h)],
        "range_source": range_source,
        "shot_count": len(shots_meta),
        "source_crop": source_crop,
        "geometry_mode": geometry_mode,
        "autooptimiser_args": optimiser_args,
        "session_dir": str(session_dir),
        "session_json": str(session_path),
        "image_path": str(final_image),
        "coordinate_map_enabled": bool(coordinate_map_enabled),
        "map_path": str(map_path) if map_path is not None and map_path.exists() else None,
        "coordinate_map_summary": coordinate_map_summary,
        "pmap_path": str(pmap_path) if pmap_path.exists() else None,
        "pmap_summary": pmap_summary,
        "pmap_error": pmap_error,
        "correction_map_path": str(correction_path) if correction_path is not None and correction_path.exists() else None,
        "correction_summary": correction_summary,
        "correction_error": correction_error,
        "coordinate_metadata": str(coordinate_meta),
        "hugin_output": str(hugin_output),
        "timings": timings,
    }
    mapping_bundle = None
    mapping_bundle_error = None
    try:
        mapping_bundle = timed(
            "export_mapping_bundle",
            _export_mapping_bundle,
            final_image,
            coordinate_meta,
            map_path,
            correction_path,
            pmap_path if pmap_path.exists() else None,
            session_path,
            pto_05,
            hugin_output,
            image_paths,
            metadata,
            meta,
        )
    except Exception as exc:
        mapping_bundle_error = str(exc)
    meta["mapping_bundle"] = mapping_bundle
    meta["mapping_bundle_error"] = mapping_bundle_error

    final_meta_path = final_image.with_name(final_image.stem + "_meta.json")
    final_meta_path.write_text(
        json.dumps(meta, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    if mapping_bundle:
        bundle_meta_path = Path(mapping_bundle["bundle_dir"]) / "panorama_meta.json"
        bundle_meta_path.write_text(json.dumps(meta, ensure_ascii=False, indent=2), encoding="utf-8")
    return {"image_name": image_rel, "image_path": final_image, "metadata": meta}


def _crop_center(img, cx: float, cy: float, size: int):
    half = size // 2
    x = int(round(cx))
    y = int(round(cy))
    h, w = img.shape[:2]
    x0 = max(0, x - half)
    y0 = max(0, y - half)
    x1 = min(w, x + half + 1)
    y1 = min(h, y + half + 1)
    crop = img[y0:y1, x0:x1]
    if crop.shape[:2] != (size, size):
        return None
    return crop


def _normalize_gray_patch(img):
    gray = cv2.cvtColor(img, cv2.COLOR_BGR2GRAY).astype(np.float32)
    gray -= float(gray.mean())
    std = float(gray.std())
    if std > 1e-6:
        gray /= std
    return gray


def _match_patch_in_window(template_bgr, search_bgr, x0: int, y0: int, margin: int) -> dict:
    template = _normalize_gray_patch(template_bgr)
    search = _normalize_gray_patch(search_bgr)
    match = cv2.matchTemplate(search, template, cv2.TM_CCOEFF_NORMED)
    _, score, _, max_loc = cv2.minMaxLoc(match)
    return {
        "x": float(x0 + max_loc[0] + margin),
        "y": float(y0 + max_loc[1] + margin),
        "score": float(score),
    }


def _trusted_core_half_ratio(value: float | None = None) -> float:
    if value is None:
        raw = os.getenv("HUGIN_TRUSTED_CORE_HALF_RATIO", str(DEFAULT_TRUSTED_CORE_HALF_RATIO))
        try:
            value = float(raw)
        except Exception:
            value = DEFAULT_TRUSTED_CORE_HALF_RATIO
    return float(np.clip(value, 0.0, 0.5))


def _source_core_info(
    ctx: "_HuginVisualBatchContext",
    sx: float,
    sy: float,
    src_img=None,
    source_size: tuple[int, int] | None = None,
    half_ratio: float | None = None,
) -> dict:
    ratio = _trusted_core_half_ratio(half_ratio)
    ratio_x = ratio
    ratio_y = ratio
    if half_ratio is None and ctx is not None:
        ratio_x = float(
            np.clip(
                float(ctx.trusted_core_half_ratio_original)
                / max(1e-6, float(ctx.source_crop_width_ratio)),
                0.01,
                0.5,
            )
        )
        ratio_y = float(
            np.clip(
                float(ctx.trusted_core_half_ratio_original)
                / max(1e-6, float(ctx.source_crop_height_ratio)),
                0.01,
                0.5,
            )
        )
    if source_size is not None:
        w, h = source_size
    elif src_img is not None:
        h, w = src_img.shape[:2]
    else:
        w = max(1.0, float(ctx.cx) * 2.0)
        h = max(1.0, float(ctx.cy) * 2.0)
    half_w = max(1e-6, ratio_x * float(w))
    half_h = max(1e-6, ratio_y * float(h))
    dx = abs(float(sx) - float(ctx.cx))
    dy = abs(float(sy) - float(ctx.cy))
    norm = max(dx / half_w, dy / half_h)
    return {
        "trusted_core": bool(norm <= 1.0),
        "trusted_core_half_ratio": ratio,
        "trusted_core_half_ratio_x": ratio_x,
        "trusted_core_half_ratio_y": ratio_y,
        "center_distance_px": float(math.hypot(dx, dy)),
        "center_norm": float(norm),
        "core_bounds": {
            "x_min": float(ctx.cx - half_w),
            "x_max": float(ctx.cx + half_w),
            "y_min": float(ctx.cy - half_h),
            "y_max": float(ctx.cy + half_h),
        },
    }


def _final_image_from_map(map_path: Path) -> Path:
    suffix = "_coordinate_map.npz"
    if map_path.name.endswith(suffix):
        image = map_path.with_name(map_path.name[: -len(suffix)] + ".jpg")
        if image.exists():
            return image
    raise RuntimeError(f"final panorama image not found near {map_path}")


class _HuginVisualBatchContext:
    def __init__(self, map_path: Path):
        self.map_path = map_path
        loaded = np.load(str(map_path), allow_pickle=False)
        try:
            self.data = {name: loaded[name] for name in loaded.files}
        finally:
            loaded.close()
        self.shot_pan = self.data["shot_pan"]
        self.shot_tilt = self.data["shot_tilt"]
        self.shot_files = [str(v) for v in self.data["shot_files"]]
        self.shot_width = self.data.get("shot_width")
        self.shot_height = self.data.get("shot_height")
        self.coverage_count = self.data.get("coverage_count")
        self.source_crop_width_ratio = self._scalar("source_crop_width_ratio", 1.0)
        self.source_crop_height_ratio = self._scalar("source_crop_height_ratio", 1.0)
        self.trusted_core_half_ratio_original = self._scalar(
            "trusted_core_half_ratio_original",
            DEFAULT_TRUSTED_CORE_HALF_RATIO,
        )
        self.fx, self.fy, self.cx, self.cy = [float(v) for v in self.data["intrinsics"]]
        self.final_image = _final_image_from_map(map_path)
        self.pano_img = _read_image(self.final_image)
        self.correction = self._load_correction()
        self._src_image_cache = {}
        self._session_cache = {}

    def _scalar(self, name: str, default: float) -> float:
        value = self.data.get(name)
        if value is None:
            return float(default)
        try:
            return float(np.asarray(value).reshape(-1)[0])
        except Exception:
            return float(default)

    def _load_correction(self) -> dict | None:
        correction_path = _correction_path_from_map(self.map_path)
        if not correction_path.exists():
            return None
        loaded = np.load(str(correction_path), allow_pickle=False)
        try:
            data = {name: loaded[name] for name in loaded.files}
        finally:
            loaded.close()
        required = {"dx", "dy", "score", "valid", "grid_step", "origin"}
        if not required.issubset(data):
            return None
        return data

    def source_image(self, shot_file: str):
        key = str(shot_file)
        img = self._src_image_cache.get(key)
        if img is None:
            img = _read_image(Path(key))
            self._src_image_cache[key] = img
        return img

    def source_size(self, idx: int, shot_file: str) -> tuple[int, int]:
        if self.shot_width is not None and self.shot_height is not None:
            w = int(self.shot_width[idx])
            h = int(self.shot_height[idx])
            if w > 0 and h > 0:
                return w, h
        img = self.source_image(shot_file)
        h, w = img.shape[:2]
        return w, h

    def refined_single_image_ptz(self, src_path: Path, px: float, py: float) -> dict:
        session_path = src_path.parent.parent / "session.json"
        session_key = str(session_path)
        session_info = self._session_cache.get(session_key)
        if session_info is None:
            session_info = mapper.load_session_info(session_path)
            self._session_cache[session_key] = session_info
        intrinsics = session_info.get("intrinsics")
        shot = mapper._find_session_shot(session_info, str(src_path))
        if not intrinsics or not shot:
            return mapper.convert_single_image_pixels(session_path, str(src_path), [(px, py)])["points"][0]
        pan, tilt = mapper._single_image_ptz_from_source(
            px,
            py,
            float(shot["actual_pan"]),
            float(shot["actual_tilt"]),
            intrinsics,
        )
        return {"pan": pan, "tilt": tilt}

    def correction_at(self, px: float, py: float) -> dict | None:
        base_owner = None
        owner = self.data.get("owner")
        if owner is not None:
            ix = int(round(px))
            iy = int(round(py))
            if 0 <= iy < owner.shape[0] and 0 <= ix < owner.shape[1]:
                base_owner = int(owner[iy, ix])
        corr = self.correction
        if not corr:
            return None
        max_shift = _env_int(
            "HUGIN_CORRECTION_MAX_SHIFT",
            DEFAULT_CORRECTION_MAX_SHIFT,
            1,
            1000,
        )
        step = float(np.asarray(corr["grid_step"]).reshape(-1)[0])
        origin = np.asarray(corr["origin"], dtype=float).reshape(-1)
        if step <= 0 or origin.size < 2:
            return None
        allow_cross_source = _env_bool(
            "HUGIN_CORRECTION_ALLOW_CROSS_SOURCE",
            DEFAULT_CORRECTION_ALLOW_CROSS_SOURCE,
        )

        def source_allowed(source_owner: int) -> bool:
            return allow_cross_source or base_owner is None or source_owner < 0 or source_owner == base_owner

        gx_f = (float(px) - float(origin[0])) / step
        gy_f = (float(py) - float(origin[1])) / step
        valid = corr["valid"]

        def point_out(gx: int, gy: int, method: str) -> dict | None:
            dx = float(corr["dx"][gy, gx])
            dy = float(corr["dy"][gy, gx])
            distance = float(math.hypot(dx, dy))
            if distance > float(max_shift):
                return None
            out = {
                "dx": dx,
                "dy": dy,
                "distance": distance,
                "score": float(corr["score"][gy, gx]),
                "grid_px": float(origin[0] + gx * step),
                "grid_py": float(origin[1] + gy * step),
                "grid_step": step,
                "lookup_method": method,
            }
            if {"source_owner", "source_px", "source_py"}.issubset(corr):
                source_owner = int(corr["source_owner"][gy, gx])
                source_px = float(corr["source_px"][gy, gx])
                source_py = float(corr["source_py"][gy, gx])
                if source_owner >= 0 and np.isfinite(source_px) and np.isfinite(source_py):
                    if not source_allowed(source_owner):
                        return None
                    out.update(
                        {
                            "source_owner": source_owner,
                            "source_px": source_px,
                            "source_py": source_py,
                        }
                    )
            return out

        gx0 = int(math.floor(gx_f))
        gy0 = int(math.floor(gy_f))
        gx1 = gx0 + 1
        gy1 = gy0 + 1
        if 0 <= gy0 and gy1 < valid.shape[0] and 0 <= gx0 and gx1 < valid.shape[1]:
            corners = [(gx0, gy0), (gx1, gy0), (gx0, gy1), (gx1, gy1)]
            if all(bool(valid[cy, cx]) for cx, cy in corners):
                tx = float(np.clip(gx_f - gx0, 0.0, 1.0))
                ty = float(np.clip(gy_f - gy0, 0.0, 1.0))
                weights = np.array(
                    [
                        (1.0 - tx) * (1.0 - ty),
                        tx * (1.0 - ty),
                        (1.0 - tx) * ty,
                        tx * ty,
                    ],
                    dtype=np.float64,
                )
                xs = np.array([gx0, gx1, gx0, gx1], dtype=np.int32)
                ys = np.array([gy0, gy0, gy1, gy1], dtype=np.int32)
                dx = float(np.sum(corr["dx"][ys, xs] * weights))
                dy = float(np.sum(corr["dy"][ys, xs] * weights))
                distance = float(math.hypot(dx, dy))
                if distance <= float(max_shift):
                    out = {
                        "dx": dx,
                        "dy": dy,
                        "distance": distance,
                        "score": float(np.sum(corr["score"][ys, xs] * weights)),
                        "grid_px": float(px),
                        "grid_py": float(py),
                        "grid_step": step,
                        "lookup_method": "bilinear_cell",
                        "cell": {
                            "x0": int(gx0),
                            "y0": int(gy0),
                            "x1": int(gx1),
                            "y1": int(gy1),
                            "tx": tx,
                            "ty": ty,
                        },
                    }
                    if {"source_owner", "source_px", "source_py"}.issubset(corr):
                        owners = corr["source_owner"][ys, xs].astype(np.int32)
                        if np.all(owners == owners[0]) and int(owners[0]) >= 0:
                            if not source_allowed(int(owners[0])):
                                return None
                            spx = corr["source_px"][ys, xs].astype(np.float64)
                            spy = corr["source_py"][ys, xs].astype(np.float64)
                            if np.all(np.isfinite(spx)) and np.all(np.isfinite(spy)):
                                out.update(
                                    {
                                        "source_owner": int(owners[0]),
                                        "source_px": float(np.sum(spx * weights)),
                                        "source_py": float(np.sum(spy * weights)),
                                    }
                                )
                    return out

        gx = int(round(gx_f))
        gy = int(round(gy_f))
        if gy < 0 or gx < 0 or gy >= valid.shape[0] or gx >= valid.shape[1]:
            return None
        if not bool(valid[gy, gx]):
            search = _env_int(
                "HUGIN_CORRECTION_LOOKUP_RADIUS_CELLS",
                DEFAULT_CORRECTION_LOOKUP_RADIUS_CELLS,
                0,
                8,
            )
            y0 = max(0, gy - search)
            y1 = min(valid.shape[0], gy + search + 1)
            x0 = max(0, gx - search)
            x1 = min(valid.shape[1], gx + search + 1)
            nearby = valid[y0:y1, x0:x1]
            if not np.any(nearby):
                return None
            ys, xs = np.where(nearby)
            abs_y = ys + y0
            abs_x = xs + x0
            dist = (abs_x - gx) ** 2 + (abs_y - gy) ** 2
            best = int(np.argmin(dist))
            gy = int(abs_y[best])
            gx = int(abs_x[best])
        return point_out(gx, gy, "nearest_grid")


class _HuginPmapBatchContext:
    def __init__(self, pmap_path: Path, session_path: Path):
        self.pmap_path = pmap_path
        self.pmap = load_pmap(pmap_path)
        self.owner = self.pmap["owner"]
        self.source_x = self.pmap["source_x"]
        self.source_y = self.pmap["source_y"]
        self.coverage_count = self.pmap.get("coverage_count")
        self.metadata = self.pmap.get("metadata") or {}

        self.session_path = session_path
        session = json.loads(session_path.read_text(encoding="utf-8"))
        session_dir = session_path.parent
        intrinsics = (session.get("camera") or {}).get("new_camera_matrix")
        if not intrinsics:
            raise RuntimeError(f"session is missing camera.new_camera_matrix: {session_path}")
        self.fx = float(intrinsics[0][0])
        self.fy = float(intrinsics[1][1])
        self.cx = float(intrinsics[0][2])
        self.cy = float(intrinsics[1][2])

        shot_files = []
        shot_pan = []
        shot_tilt = []
        shot_width = []
        shot_height = []
        for shot in session.get("shots", []):
            rel = shot.get("undistorted_path") or shot.get("raw_path")
            if not rel:
                continue
            shot_files.append(str((session_dir / rel).resolve()))
            shot_pan.append(float(shot.get("actual_pan", shot.get("target_pan", 0.0))))
            shot_tilt.append(float(shot.get("actual_tilt", shot.get("target_tilt", 0.0))))
            shot_width.append(int(shot.get("image_width") or 0))
            shot_height.append(int(shot.get("image_height") or 0))
        if not shot_files:
            raise RuntimeError(f"session has no shots: {session_path}")
        self.shot_files = shot_files
        self.shot_pan = np.asarray(shot_pan, dtype=np.float32)
        self.shot_tilt = np.asarray(shot_tilt, dtype=np.float32)
        self.shot_width = np.asarray(shot_width, dtype=np.int32)
        self.shot_height = np.asarray(shot_height, dtype=np.int32)
        source_crop = ((session.get("camera") or {}).get("source_crop") or {})
        self.source_crop_width_ratio = float(source_crop.get("width_ratio") or 1.0)
        self.source_crop_height_ratio = float(source_crop.get("height_ratio") or 1.0)
        self.trusted_core_half_ratio_original = float(
            source_crop.get("trusted_core_half_ratio_original") or DEFAULT_TRUSTED_CORE_HALF_RATIO
        )
        self._src_image_cache = {}
        self._reverse_bucket_index = None
        self._reverse_bucket_index_lock = threading.Lock()

    def source_image(self, shot_file: str):
        key = str(shot_file)
        img = self._src_image_cache.get(key)
        if img is None:
            img = _read_image(Path(key))
            self._src_image_cache[key] = img
        return img

    def source_size(self, idx: int, shot_file: str) -> tuple[int, int]:
        if 0 <= idx < len(self.shot_width):
            w = int(self.shot_width[idx])
            h = int(self.shot_height[idx])
            if w > 0 and h > 0:
                return w, h
        img = self.source_image(shot_file)
        h, w = img.shape[:2]
        return w, h


def _pmap_context_cache_key(pmap_path: Path, session_path: Path) -> tuple[str, int, int, str, int, int]:
    resolved_pmap = pmap_path.expanduser().resolve()
    resolved_session = session_path.expanduser().resolve()
    pmap_stat = resolved_pmap.stat()
    session_stat = resolved_session.stat()
    return (
        str(resolved_pmap),
        int(pmap_stat.st_mtime_ns),
        int(pmap_stat.st_size),
        str(resolved_session),
        int(session_stat.st_mtime_ns),
        int(session_stat.st_size),
    )


def _get_pmap_context(pmap_path: Path, session_path: Path) -> _HuginPmapBatchContext:
    key = _pmap_context_cache_key(pmap_path, session_path)
    cached = _PMAP_CONTEXT_CACHE.get(key)
    if cached is not None:
        return cached
    ctx = _HuginPmapBatchContext(pmap_path.expanduser().resolve(), session_path.expanduser().resolve())
    _PMAP_CONTEXT_CACHE[key] = ctx
    while len(_PMAP_CONTEXT_CACHE) > _PMAP_CONTEXT_CACHE_MAX:
        _PMAP_CONTEXT_CACHE.pop(next(iter(_PMAP_CONTEXT_CACHE)))
    return ctx


def _pmap_bucket_id(bx: int, by: int) -> int:
    return (int(by) << 32) | (int(bx) & 0xFFFFFFFF)


def _build_owner_bucket_index(
    sx: np.ndarray,
    sy: np.ndarray,
    pano_x: np.ndarray,
    pano_y: np.ndarray,
    bucket_size: int,
) -> dict:
    bx = np.floor_divide(sx.astype(np.int32), int(bucket_size))
    by = np.floor_divide(sy.astype(np.int32), int(bucket_size))
    bucket_ids = (by.astype(np.int64) << 32) | (bx.astype(np.int64) & 0xFFFFFFFF)
    order = np.argsort(bucket_ids, kind="mergesort")
    bucket_ids = bucket_ids[order]
    sx = sx[order].astype(np.int16, copy=False)
    sy = sy[order].astype(np.int16, copy=False)
    pano_x = pano_x[order].astype(np.uint32, copy=False)
    pano_y = pano_y[order].astype(np.uint32, copy=False)
    unique_ids, starts = np.unique(bucket_ids, return_index=True)
    ends = np.r_[starts[1:], len(bucket_ids)]
    bucket_ranges = {
        int(bucket_id): (int(start), int(end))
        for bucket_id, start, end in zip(unique_ids, starts, ends)
    }
    bucket_bx = (unique_ids & 0xFFFFFFFF).astype(np.int64)
    bucket_by = (unique_ids >> 32).astype(np.int64)
    return {
        "source_x": sx,
        "source_y": sy,
        "pano_x": pano_x,
        "pano_y": pano_y,
        "bucket_ranges": bucket_ranges,
        "bucket_bx": bucket_bx,
        "bucket_by": bucket_by,
        "bucket_ids": unique_ids.astype(np.int64, copy=False),
        "bucket_size": int(bucket_size),
    }


def _build_pmap_reverse_bucket_index(
    ctx: _HuginPmapBatchContext,
    bucket_size: int = PMAP_REVERSE_BUCKET_SIZE,
) -> dict:
    valid = (
        (ctx.owner >= 0)
        & (ctx.source_x != PMAP_INVALID_INT16)
        & (ctx.source_y != PMAP_INVALID_INT16)
    )
    pano_y, pano_x = np.nonzero(valid)
    if pano_x.size == 0:
        return {"owners": {}, "bucket_size": int(bucket_size), "valid_pixels": 0}
    owners = ctx.owner[valid].astype(np.int32, copy=False)
    sx = ctx.source_x[valid].astype(np.int16, copy=False)
    sy = ctx.source_y[valid].astype(np.int16, copy=False)
    owner_indexes = {}
    for owner in np.unique(owners):
        owner_int = int(owner)
        if owner_int < 0 or owner_int >= len(ctx.shot_files):
            continue
        mask = owners == owner_int
        owner_indexes[owner_int] = _build_owner_bucket_index(
            sx[mask],
            sy[mask],
            pano_x[mask],
            pano_y[mask],
            int(bucket_size),
        )
    return {
        "owners": owner_indexes,
        "bucket_size": int(bucket_size),
        "valid_pixels": int(pano_x.size),
    }


def _get_pmap_reverse_bucket_index(ctx: _HuginPmapBatchContext) -> dict:
    cached = ctx._reverse_bucket_index
    if cached is not None:
        return cached
    with ctx._reverse_bucket_index_lock:
        cached = ctx._reverse_bucket_index
        if cached is None:
            cached = _build_pmap_reverse_bucket_index(ctx)
            ctx._reverse_bucket_index = cached
        return cached


def _best_owner_bucket_candidate(
    owner_index: dict,
    sx: float,
    sy: float,
    bucket_radius: int,
) -> dict | None:
    bucket_size = int(owner_index["bucket_size"])
    bx0 = int(math.floor(float(sx) / bucket_size))
    by0 = int(math.floor(float(sy) / bucket_size))
    ranges = owner_index["bucket_ranges"]
    best = None
    best_dist = float("inf")
    candidate_count = 0
    for by in range(by0 - bucket_radius, by0 + bucket_radius + 1):
        for bx in range(bx0 - bucket_radius, bx0 + bucket_radius + 1):
            span = ranges.get(_pmap_bucket_id(bx, by))
            if span is None:
                continue
            start, end = span
            if end <= start:
                continue
            cand_sx = owner_index["source_x"][start:end].astype(np.float32)
            cand_sy = owner_index["source_y"][start:end].astype(np.float32)
            dist = (cand_sx - float(sx)) ** 2 + (cand_sy - float(sy)) ** 2
            local_best = int(np.argmin(dist))
            local_dist = float(dist[local_best])
            candidate_count += int(end - start)
            if local_dist < best_dist:
                best_dist = local_dist
                idx = int(start + local_best)
                best = {
                    "px": float(owner_index["pano_x"][idx]),
                    "py": float(owner_index["pano_y"][idx]),
                    "source_error_px": float(math.sqrt(local_dist)),
                    "candidate_count": candidate_count,
                    "bucket_radius": int(bucket_radius),
                    "bucket_size": bucket_size,
                    "fallback_nearest_bucket": False,
                }
    if best is not None:
        best["candidate_count"] = candidate_count
    return best


def _nearest_owner_bucket_candidate(owner_index: dict, sx: float, sy: float) -> dict | None:
    bucket_bx = owner_index["bucket_bx"]
    bucket_by = owner_index["bucket_by"]
    bucket_ids = owner_index["bucket_ids"]
    if bucket_ids.size == 0:
        return None
    bucket_size = int(owner_index["bucket_size"])
    bx0 = int(math.floor(float(sx) / bucket_size))
    by0 = int(math.floor(float(sy) / bucket_size))
    dist = (bucket_bx - bx0) ** 2 + (bucket_by - by0) ** 2
    nearest = int(np.argmin(dist))
    span = owner_index["bucket_ranges"].get(int(bucket_ids[nearest]))
    if span is None:
        return None
    start, end = span
    cand_sx = owner_index["source_x"][start:end].astype(np.float32)
    cand_sy = owner_index["source_y"][start:end].astype(np.float32)
    point_dist = (cand_sx - float(sx)) ** 2 + (cand_sy - float(sy)) ** 2
    local_best = int(np.argmin(point_dist))
    idx = int(start + local_best)
    return {
        "px": float(owner_index["pano_x"][idx]),
        "py": float(owner_index["pano_y"][idx]),
        "source_error_px": float(math.sqrt(float(point_dist[local_best]))),
        "candidate_count": int(end - start),
        "bucket_radius": None,
        "bucket_size": bucket_size,
        "fallback_nearest_bucket": True,
    }


def _pmap_point_from_context(
    ctx: _HuginPmapBatchContext,
    point: tuple[float, float],
    trusted_core_half_ratio: float | None = None,
) -> dict:
    px, py = point
    ix = int(round(px))
    iy = int(round(py))
    h, w = ctx.owner.shape
    in_frame = 0 <= ix < w and 0 <= iy < h
    if not in_frame:
        return {"px": px, "py": py, "in_frame": False, "has_source": False, "method": "pmap_out_of_frame"}

    owner = int(ctx.owner[iy, ix])
    sx = float(ctx.source_x[iy, ix])
    sy = float(ctx.source_y[iy, ix])
    if owner == PMAP_INVALID_INT16 or owner < 0 or owner >= len(ctx.shot_files):
        return {"px": px, "py": py, "in_frame": True, "has_source": False, "method": "pmap_no_source"}
    if sx == PMAP_INVALID_INT16 or sy == PMAP_INVALID_INT16:
        return {"px": px, "py": py, "in_frame": True, "has_source": False, "method": "pmap_no_source"}

    shot_file = str(ctx.shot_files[owner])
    core = _source_core_info(
        ctx,
        sx,
        sy,
        source_size=ctx.source_size(owner, shot_file),
        half_ratio=trusted_core_half_ratio,
    )
    coverage = int(ctx.coverage_count[iy, ix]) if ctx.coverage_count is not None else None
    pan, tilt = mapper._pixel_to_absolute_ptz_with_intrinsics(
        sx,
        sy,
        ctx.cx,
        ctx.cy,
        ctx.fx,
        ctx.fy,
        float(ctx.shot_pan[owner]),
        float(ctx.shot_tilt[owner]),
    )
    reliable = bool(core["trusted_core"] and (coverage is None or coverage <= 1))
    return {
        "px": px,
        "py": py,
        "method": "pmap_source_pixel",
        "pan": pan,
        "tilt": tilt,
        "in_frame": True,
        "has_source": True,
        "reliable": reliable,
        "match_score": None,
        "visual_shift_px": None,
        "source": {
            "owner_index": owner,
            "base_owner_index": owner,
            "shot_file": shot_file,
            "shot_pan": float(ctx.shot_pan[owner]),
            "shot_tilt": float(ctx.shot_tilt[owner]),
            "source_px": sx,
            "source_py": sy,
            "base_source_px": sx,
            "base_source_py": sy,
            "trusted_core": core["trusted_core"],
            "center_distance_px": core["center_distance_px"],
            "center_norm": core["center_norm"],
        },
        "trusted_core": core["trusted_core"],
        "trusted_core_half_ratio": core["trusted_core_half_ratio"],
        "center_norm": core["center_norm"],
        "core_bounds": core["core_bounds"],
        "coverage_count": coverage,
        "pmap": {
            "path": str(ctx.pmap_path),
            "metadata": ctx.metadata,
        },
    }


def pixels_to_angles_pmap(
    pmap_path: Path,
    session_path: Path,
    points: list[tuple[float, float]],
    trusted_core_half_ratio: float | None = None,
) -> list[dict]:
    ctx = _get_pmap_context(pmap_path, session_path)
    return [
        _pmap_point_from_context(
            ctx,
            point,
            trusted_core_half_ratio=trusted_core_half_ratio,
        )
        for point in points
    ]


def _locate_source_pixel_in_pmap(
    ctx: _HuginPmapBatchContext,
    owner_index: int,
    source_point: tuple[float, float],
) -> dict | None:
    sx, sy = source_point
    reverse_index = _get_pmap_reverse_bucket_index(ctx)
    owner_bucket = reverse_index["owners"].get(int(owner_index))
    if owner_bucket is None:
        return None
    for radius in (0, 1, 2):
        candidate = _best_owner_bucket_candidate(owner_bucket, sx, sy, radius)
        if candidate is not None:
            return candidate
    return _nearest_owner_bucket_candidate(owner_bucket, sx, sy)


def _pmap_angle_to_pixel_from_context(
    ctx: _HuginPmapBatchContext,
    angle: tuple[float, float],
    trusted_core_half_ratio: float | None = None,
) -> dict:
    pan, tilt = angle
    candidates = []
    for idx, shot_file in enumerate(ctx.shot_files):
        source_size = ctx.source_size(idx, shot_file)
        source = _absolute_ptz_to_pixel(
            pan,
            tilt,
            float(ctx.shot_pan[idx]),
            float(ctx.shot_tilt[idx]),
            ctx.cx,
            ctx.cy,
            ctx.fx,
            ctx.fy,
            source_size[0],
            source_size[1],
        )
        if source is None:
            continue
        geo = _locate_source_pixel_in_pmap(ctx, idx, source)
        if geo is None:
            continue
        core = _source_core_info(
            ctx,
            source[0],
            source[1],
            source_size=source_size,
            half_ratio=trusted_core_half_ratio,
        )
        candidates.append(
            {
                "owner_index": int(idx),
                "shot_file": shot_file,
                "shot_pan": float(ctx.shot_pan[idx]),
                "shot_tilt": float(ctx.shot_tilt[idx]),
                "source_px": float(source[0]),
                "source_py": float(source[1]),
                "source_error_px": float(geo["source_error_px"]),
                "source_center_distance_px": core["center_distance_px"],
                "center_norm": core["center_norm"],
                "core_bounds": core["core_bounds"],
                "trusted_core": core["trusted_core"],
                "geometric": geo,
            }
        )

    if not candidates:
        return {
            "pan": float(pan),
            "tilt": float(tilt),
            "in_frame": False,
            "reliable": False,
            "method": "pmap_angle_no_source",
            "pmap": {
                "path": str(ctx.pmap_path),
                "metadata": ctx.metadata,
            },
        }

    candidates.sort(
        key=lambda item: (
            float(item["source_error_px"]),
            float(item["center_norm"]),
            not bool(item["trusted_core"]),
            float(item["source_center_distance_px"]),
        )
    )
    best = candidates[0]
    px = float(best["geometric"]["px"])
    py = float(best["geometric"]["py"])
    h, w = ctx.owner.shape
    ix = int(round(px))
    iy = int(round(py))
    coverage = int(ctx.coverage_count[iy, ix]) if ctx.coverage_count is not None and 0 <= iy < h and 0 <= ix < w else None
    fallback_nearest_bucket = bool(best["geometric"].get("fallback_nearest_bucket", False))
    reliable = bool(best["trusted_core"] and (coverage is None or coverage <= 1) and not fallback_nearest_bucket)
    return {
        "pan": float(pan),
        "tilt": float(tilt),
        "px": px,
        "py": py,
        "in_frame": 0 <= ix < w and 0 <= iy < h,
        "method": "pmap_angle_source_pixel",
        "reliable": reliable,
        "match_score": None,
        "source": {
            "owner_index": int(best["owner_index"]),
            "shot_file": best["shot_file"],
            "shot_pan": float(best["shot_pan"]),
            "shot_tilt": float(best["shot_tilt"]),
            "source_px": float(best["source_px"]),
            "source_py": float(best["source_py"]),
            "source_error_px": float(best["source_error_px"]),
            "trusted_core": bool(best["trusted_core"]),
            "center_distance_px": float(best["source_center_distance_px"]),
            "center_norm": float(best["center_norm"]),
            "bucket_radius": best["geometric"].get("bucket_radius"),
            "bucket_size": best["geometric"].get("bucket_size"),
            "bucket_candidate_count": best["geometric"].get("candidate_count"),
            "fallback_nearest_bucket": fallback_nearest_bucket,
        },
        "geometric": best["geometric"],
        "trusted_core": bool(best["trusted_core"]),
        "trusted_core_half_ratio": _trusted_core_half_ratio(trusted_core_half_ratio),
        "center_norm": float(best["center_norm"]),
        "core_bounds": best["core_bounds"],
        "coverage_count": coverage,
        "candidate_count": len(candidates),
        "pmap": {
            "path": str(ctx.pmap_path),
            "metadata": ctx.metadata,
        },
    }


def angles_to_pixels_pmap(
    pmap_path: Path,
    session_path: Path,
    angles: list[tuple[float, float]],
    trusted_core_half_ratio: float | None = None,
) -> list[dict]:
    ctx = _get_pmap_context(pmap_path, session_path)
    return [
        _pmap_angle_to_pixel_from_context(
            ctx,
            angle,
            trusted_core_half_ratio=trusted_core_half_ratio,
        )
        for angle in angles
    ]


def build_correction_map(map_path: Path, out_path: Path | None = None) -> dict:
    started = time.perf_counter()
    out_path = out_path or _correction_path_from_map(map_path)
    grid_step = _env_int("HUGIN_CORRECTION_GRID_STEP", DEFAULT_CORRECTION_GRID_STEP, 16, 256)
    patch = _odd_at_least(_env_int("HUGIN_CORRECTION_PATCH", DEFAULT_CORRECTION_PATCH, 21, 121), 21)
    radius = _env_int("HUGIN_CORRECTION_RADIUS", DEFAULT_CORRECTION_RADIUS, 20, 800)
    fallback_radius = _env_int(
        "HUGIN_CORRECTION_FALLBACK_RADIUS",
        DEFAULT_CORRECTION_FALLBACK_RADIUS,
        radius,
        1000,
    )
    max_shift = _env_int("HUGIN_CORRECTION_MAX_SHIFT", DEFAULT_CORRECTION_MAX_SHIFT, 1, 1000)
    max_points = _env_int("HUGIN_CORRECTION_MAX_POINTS", DEFAULT_CORRECTION_MAX_POINTS, 1, 20000)
    workers = _env_int("HUGIN_CORRECTION_WORKERS", DEFAULT_CORRECTION_WORKERS, 1, 8)
    score_threshold = float(os.getenv("HUGIN_CORRECTION_SCORE", str(DEFAULT_CORRECTION_SCORE)))
    allow_cross_source = _env_bool(
        "HUGIN_CORRECTION_ALLOW_CROSS_SOURCE",
        DEFAULT_CORRECTION_ALLOW_CROSS_SOURCE,
    )
    overlap_only = _env_bool("HUGIN_CORRECTION_OVERLAP_ONLY", DEFAULT_CORRECTION_OVERLAP_ONLY)
    adaptive_refinement = _env_bool(
        "HUGIN_CORRECTION_ADAPTIVE_REFINEMENT",
        DEFAULT_CORRECTION_ADAPTIVE_REFINEMENT,
    )
    adaptive_edge_norm = _env_float(
        "HUGIN_CORRECTION_ADAPTIVE_EDGE_NORM",
        DEFAULT_CORRECTION_ADAPTIVE_EDGE_NORM,
        0.0,
        2.0,
    )
    min_texture_std = _env_float(
        "HUGIN_CORRECTION_MIN_TEXTURE_STD",
        DEFAULT_CORRECTION_MIN_TEXTURE_STD,
        0.0,
        64.0,
    )
    pyramid_scale = _env_float(
        "HUGIN_CORRECTION_PYRAMID_SCALE",
        DEFAULT_CORRECTION_PYRAMID_SCALE,
        0.1,
        1.0,
    )
    coarse_patch = _odd_at_least(
        _env_int("HUGIN_CORRECTION_COARSE_PATCH", DEFAULT_CORRECTION_COARSE_PATCH, 21, 401),
        21,
    )
    fine_radius = _env_int(
        "HUGIN_CORRECTION_FINE_RADIUS",
        DEFAULT_CORRECTION_FINE_RADIUS,
        4,
        max(4, fallback_radius),
    )

    ctx = _HuginVisualBatchContext(map_path)
    owner = ctx.data["owner"]
    source_x = ctx.data["source_x"]
    source_y = ctx.data["source_y"]
    coverage = ctx.data.get("coverage_count")
    if coverage is None:
        coverage = np.where(owner >= 0, 1, 0).astype(np.uint16)

    h, w = owner.shape
    base_grid_step = grid_step
    refine_factor = 2 if adaptive_refinement and grid_step >= 24 else 1
    grid_step = max(8, int(round(grid_step / refine_factor)))
    origin_x = base_grid_step // 2
    origin_y = base_grid_step // 2
    xs = list(range(origin_x, w, grid_step))
    ys = list(range(origin_y, h, grid_step))
    dx_grid = np.zeros((len(ys), len(xs)), dtype=np.float32)
    dy_grid = np.zeros((len(ys), len(xs)), dtype=np.float32)
    score_grid = np.zeros((len(ys), len(xs)), dtype=np.float32)
    valid_grid = np.zeros((len(ys), len(xs)), dtype=bool)
    source_owner_grid = np.full((len(ys), len(xs)), -1, dtype=np.int16)
    source_px_grid = np.full((len(ys), len(xs)), np.nan, dtype=np.float32)
    source_py_grid = np.full((len(ys), len(xs)), np.nan, dtype=np.float32)

    def near_owner_boundary(x: int, y: int, idx: int) -> bool:
        pad = 2
        y0 = max(0, y - pad)
        y1 = min(h, y + pad + 1)
        x0 = max(0, x - pad)
        x1 = min(w, x + pad + 1)
        local = owner[y0:y1, x0:x1]
        valid_local = local[local >= 0]
        if valid_local.size <= 0:
            return False
        return bool(np.any(valid_local != idx))

    def source_edge_risk(idx: int, sx: float, sy: float) -> bool:
        shot_file = str(ctx.shot_files[idx])
        try:
            core = _source_core_info(
                ctx,
                sx,
                sy,
                source_size=ctx.source_size(idx, shot_file),
            )
            return bool(float(core["center_norm"]) >= adaptive_edge_norm)
        except Exception:
            return False

    base_candidates = []
    adaptive_candidates = []
    for gy, y in enumerate(ys):
        for gx, x in enumerate(xs):
            idx = int(owner[y, x])
            if idx < 0:
                continue
            if overlap_only and int(coverage[y, x]) <= 1:
                continue
            if not np.isfinite(float(source_x[y, x])) or not np.isfinite(float(source_y[y, x])):
                continue
            is_base = (gx % refine_factor == 0 and gy % refine_factor == 0)
            candidate = (gx, gy, x, y, idx)
            if is_base:
                base_candidates.append(candidate)
                continue
            if not adaptive_refinement:
                continue
            sx = float(source_x[y, x])
            sy = float(source_y[y, x])
            if (
                int(coverage[y, x]) > 1
                or near_owner_boundary(x, y, idx)
                or source_edge_risk(idx, sx, sy)
            ):
                adaptive_candidates.append(candidate)

    total_base_candidates = len(base_candidates)
    total_adaptive_candidates = len(adaptive_candidates)
    total_candidates = total_base_candidates + total_adaptive_candidates
    if len(base_candidates) >= max_points:
        stride = int(math.ceil(len(base_candidates) / max_points))
        candidates = base_candidates[::stride]
        sampled_base_candidates = len(candidates)
        sampled_adaptive_candidates = 0
    else:
        remaining = max_points - len(base_candidates)
        if len(adaptive_candidates) > remaining:
            stride = int(math.ceil(len(adaptive_candidates) / max(1, remaining)))
            adaptive_candidates = adaptive_candidates[::stride]
        candidates = base_candidates + adaptive_candidates
        sampled_base_candidates = len(base_candidates)
        sampled_adaptive_candidates = len(adaptive_candidates)

    use_pyramid = pyramid_scale < 0.999
    pano_small = None
    small_source_cache = {}
    if use_pyramid:
        pano_small = cv2.resize(
            ctx.pano_img,
            (
                max(1, int(round(ctx.pano_img.shape[1] * pyramid_scale))),
                max(1, int(round(ctx.pano_img.shape[0] * pyramid_scale))),
            ),
            interpolation=cv2.INTER_AREA,
        )

    for idx in sorted({item[4] for item in candidates}):
        src_img = ctx.source_image(str(ctx.shot_files[idx]))
        if use_pyramid:
            small_source_cache[idx] = cv2.resize(
                src_img,
                (
                    max(1, int(round(src_img.shape[1] * pyramid_scale))),
                    max(1, int(round(src_img.shape[0] * pyramid_scale))),
                ),
                interpolation=cv2.INTER_AREA,
            )

    def match_candidate(candidate):
        gx, gy, x, y, idx = candidate
        pano_patch = _crop_center(ctx.pano_img, x, y, patch)
        if pano_patch is None:
            return None
        if min_texture_std > 0.0:
            texture_std = float(cv2.cvtColor(pano_patch, cv2.COLOR_BGR2GRAY).std())
            if texture_std < min_texture_std:
                return None
        shot_file = str(ctx.shot_files[idx])
        src_img = ctx.source_image(shot_file)
        base_sx = float(source_x[y, x])
        base_sy = float(source_y[y, x])
        margin = patch // 2
        coarse_patch_scaled = _odd_at_least(int(round(coarse_patch * pyramid_scale)), 21)
        coarse_margin = coarse_patch_scaled // 2
        fine_margin = patch // 2

        def direct_match(
            template,
            candidate_img,
            cand_sx: float,
            cand_sy: float,
            search_radius: int,
            template_margin: int,
        ):
            sx0 = int(max(0, math.floor(cand_sx - search_radius - template_margin)))
            sy0 = int(max(0, math.floor(cand_sy - search_radius - template_margin)))
            sx1 = int(min(candidate_img.shape[1], math.ceil(cand_sx + search_radius + template_margin + 1)))
            sy1 = int(min(candidate_img.shape[0], math.ceil(cand_sy + search_radius + template_margin + 1)))
            window = candidate_img[sy0:sy1, sx0:sx1]
            if window.shape[0] < template.shape[0] or window.shape[1] < template.shape[1]:
                return None
            return _match_patch_in_window(template, window, sx0, sy0, template_margin)

        def match_source(search_radius: int, candidate_idx: int, candidate_img, cand_sx: float, cand_sy: float):
            matched = None
            coarse_score = None
            coarse_source = None
            match_stage = "direct"
            if use_pyramid and pano_small is not None:
                pano_patch_small = _crop_center(
                    pano_small,
                    float(x) * pyramid_scale,
                    float(y) * pyramid_scale,
                    coarse_patch_scaled,
                )
                candidate_small = small_source_cache.get(candidate_idx)
                if pano_patch_small is not None and candidate_small is not None:
                    coarse = direct_match(
                        pano_patch_small,
                        candidate_small,
                        cand_sx * pyramid_scale,
                        cand_sy * pyramid_scale,
                        max(4, int(round(search_radius * pyramid_scale))),
                        coarse_margin,
                    )
                    if coarse is not None:
                        coarse_score = float(coarse["score"])
                        coarse_source = (
                            float(coarse["x"]) / pyramid_scale,
                            float(coarse["y"]) / pyramid_scale,
                        )
                        matched = direct_match(
                            pano_patch,
                            candidate_img,
                            coarse_source[0],
                            coarse_source[1],
                            fine_radius,
                            fine_margin,
                        )
                        match_stage = "pyramid"
            if matched is None:
                matched = direct_match(pano_patch, candidate_img, cand_sx, cand_sy, search_radius, margin)
                match_stage = "direct_fallback" if use_pyramid else "direct"
            if matched is None:
                return None
            score = float(matched["score"])
            dx = float(matched["x"] - cand_sx)
            dy = float(matched["y"] - cand_sy)
            return {
                "dx": float(matched["x"] - base_sx) if candidate_idx == idx else dx,
                "dy": float(matched["y"] - base_sy) if candidate_idx == idx else dy,
                "distance": float(math.hypot(dx, dy)),
                "score": score,
                "coarse_score": coarse_score,
                "coarse_source_px": coarse_source[0] if coarse_source else None,
                "coarse_source_py": coarse_source[1] if coarse_source else None,
                "match_stage": match_stage,
                "radius": int(search_radius),
                "owner_index": int(candidate_idx),
                "source_px": float(matched["x"]),
                "source_py": float(matched["y"]),
            }

        primary = match_source(radius, idx, src_img, base_sx, base_sy)
        fallback = None
        primary_near_edge = bool(primary and float(primary["distance"]) >= float(radius) * 0.80)
        need_fallback = (
            fallback_radius > radius
            and (
                primary is None
                or float(primary["score"]) < score_threshold
                or primary_near_edge
                or int(coverage[y, x]) >= 3
            )
        )
        if need_fallback:
            fallback_candidates = []
            owner_fallback = match_source(fallback_radius, idx, src_img, base_sx, base_sy)
            if owner_fallback is not None:
                fallback_candidates.append(owner_fallback)
            if allow_cross_source:
                try:
                    base_pan, base_tilt = mapper._pixel_to_absolute_ptz_with_intrinsics(
                        base_sx,
                        base_sy,
                        ctx.cx,
                        ctx.cy,
                        ctx.fx,
                        ctx.fy,
                        float(ctx.shot_pan[idx]),
                        float(ctx.shot_tilt[idx]),
                    )
                    for alt_idx, alt_file in enumerate(ctx.shot_files):
                        if alt_idx == idx:
                            continue
                        alt_img = ctx.source_image(str(alt_file))
                        projected = _absolute_ptz_to_pixel(
                            base_pan,
                            base_tilt,
                            float(ctx.shot_pan[alt_idx]),
                            float(ctx.shot_tilt[alt_idx]),
                            ctx.cx,
                            ctx.cy,
                            ctx.fx,
                            ctx.fy,
                            alt_img.shape[1],
                            alt_img.shape[0],
                        )
                        if projected is None:
                            continue
                        alt_match = match_source(fallback_radius, alt_idx, alt_img, projected[0], projected[1])
                        if alt_match is not None:
                            fallback_candidates.append(alt_match)
                except Exception:
                    pass
            if fallback_candidates:
                fallback = max(fallback_candidates, key=lambda item: float(item["score"]))

        best = primary
        stage = "primary"
        if fallback is not None and (
            best is None or float(fallback["score"]) > float(best["score"])
        ):
            best = fallback
            stage = "fallback"
        if best is not None:
            best["stage"] = stage
        if (
            best is None
            or float(best["score"]) < score_threshold
            or float(best["distance"]) > float(max_shift)
        ):
            return (gx, gy, idx, None, primary, fallback)
        return (gx, gy, idx, best, primary, fallback)

    attempted = 0
    accepted = 0
    primary_attempted = 0
    primary_accepted = 0
    fallback_attempted = 0
    fallback_accepted = 0
    fallback_cross_source_selected = 0
    rejected_large_shift = 0
    pyramid_selected = 0
    direct_selected = 0
    if workers <= 1 or len(candidates) <= 1:
        results = [match_candidate(candidate) for candidate in candidates]
    else:
        with ThreadPoolExecutor(max_workers=workers) as executor:
            results = list(executor.map(match_candidate, candidates))

    for result in results:
        if result is None:
            continue
        attempted += 1
        gx, gy, base_idx, match, primary, fallback = result
        if primary is not None:
            primary_attempted += 1
            if float(primary["score"]) >= score_threshold and float(primary["distance"]) <= float(max_shift):
                primary_accepted += 1
        if fallback is not None:
            fallback_attempted += 1
            if float(fallback["score"]) >= score_threshold and float(fallback["distance"]) <= float(max_shift):
                fallback_accepted += 1
        if match is None:
            best_observed = fallback if (
                fallback is not None
                and (primary is None or float(fallback["score"]) > float(primary["score"]))
            ) else primary
            if (
                best_observed is not None
                and float(best_observed["score"]) >= score_threshold
                and float(best_observed["distance"]) > float(max_shift)
            ):
                rejected_large_shift += 1
        if match is None:
            continue
        dx_grid[gy, gx] = match["dx"]
        dy_grid[gy, gx] = match["dy"]
        score_grid[gy, gx] = match["score"]
        source_owner_grid[gy, gx] = int(match.get("owner_index", base_idx))
        source_px_grid[gy, gx] = float(match["source_px"])
        source_py_grid[gy, gx] = float(match["source_py"])
        valid_grid[gy, gx] = True
        if str(match.get("match_stage", "")).startswith("pyramid"):
            pyramid_selected += 1
        else:
            direct_selected += 1
        if int(match.get("owner_index", base_idx)) != base_idx:
            fallback_cross_source_selected += 1
        accepted += 1

    out_path.parent.mkdir(parents=True, exist_ok=True)
    summary = {
        "schema": "hugin_visual_correction_map_v1",
        "map_path": str(map_path),
        "grid_step": grid_step,
        "base_grid_step": base_grid_step,
        "adaptive_refinement": adaptive_refinement,
        "adaptive_edge_norm": adaptive_edge_norm,
        "origin": [origin_x, origin_y],
        "patch": patch,
        "radius": radius,
        "fallback_radius": fallback_radius,
        "max_shift": max_shift,
        "workers": workers,
        "score_threshold": score_threshold,
        "allow_cross_source": allow_cross_source,
        "overlap_only": overlap_only,
        "min_texture_std": min_texture_std,
        "pyramid_scale": pyramid_scale,
        "coarse_patch": coarse_patch,
        "coarse_patch_scaled": int(_odd_at_least(int(round(coarse_patch * pyramid_scale)), 21)),
        "fine_radius": fine_radius,
        "base_candidate_count": total_base_candidates,
        "adaptive_candidate_count": total_adaptive_candidates,
        "sampled_base_candidate_count": sampled_base_candidates,
        "sampled_adaptive_candidate_count": sampled_adaptive_candidates,
        "total_candidates": total_candidates,
        "sampled_candidates": len(candidates),
        "attempted_points": attempted,
        "accepted_points": accepted,
        "primary_attempted_points": primary_attempted,
        "primary_accepted_points": primary_accepted,
        "fallback_attempted_points": fallback_attempted,
        "fallback_accepted_points": fallback_accepted,
        "fallback_cross_source_selected_points": fallback_cross_source_selected,
        "rejected_large_shift_points": rejected_large_shift,
        "pyramid_selected_points": pyramid_selected,
        "direct_selected_points": direct_selected,
        "elapsed_seconds": round(time.perf_counter() - started, 3),
    }
    np.savez_compressed(
        out_path,
        dx=dx_grid,
        dy=dy_grid,
        score=score_grid,
        valid=valid_grid,
        source_owner=source_owner_grid,
        source_px=source_px_grid,
        source_py=source_py_grid,
        grid_step=np.array([grid_step], dtype=np.int32),
        origin=np.array([origin_x, origin_y], dtype=np.float32),
        metadata_json=json.dumps(summary, ensure_ascii=False),
    )
    return {**summary, "path": str(out_path)}


def _coordinate_map_point_from_context(
    ctx: _HuginVisualBatchContext,
    point: tuple[float, float],
    trusted_core_half_ratio: float | None = None,
    apply_correction: bool = True,
) -> dict:
    owner = ctx.data["owner"]
    source_x = ctx.data["source_x"]
    source_y = ctx.data["source_y"]
    h, w = owner.shape
    px, py = point
    ix = int(round(px))
    iy = int(round(py))
    in_frame = 0 <= ix < w and 0 <= iy < h
    if not in_frame or int(owner[iy, ix]) < 0:
        return {"px": px, "py": py, "in_frame": in_frame, "has_source": False}
    idx = int(owner[iy, ix])
    sx = float(source_x[iy, ix])
    sy = float(source_y[iy, ix])
    correction = ctx.correction_at(px, py) if apply_correction else None
    source_idx = idx
    if correction and correction.get("source_owner") is not None:
        corr_idx = int(correction["source_owner"])
        if 0 <= corr_idx < len(ctx.shot_files):
            source_idx = corr_idx
            sx_used = float(correction["source_px"])
            sy_used = float(correction["source_py"])
        else:
            sx_used = sx + float(correction["dx"])
            sy_used = sy + float(correction["dy"])
    elif correction:
        sx_used = sx + float(correction["dx"])
        sy_used = sy + float(correction["dy"])
    else:
        sx_used = sx
        sy_used = sy
    shot_file = str(ctx.shot_files[source_idx])
    core = _source_core_info(
        ctx,
        sx_used,
        sy_used,
        source_size=ctx.source_size(source_idx, shot_file),
        half_ratio=trusted_core_half_ratio,
    )
    coverage = None
    if ctx.coverage_count is not None:
        coverage = int(ctx.coverage_count[iy, ix])
    pan, tilt = mapper._pixel_to_absolute_ptz_with_intrinsics(
        sx_used,
        sy_used,
        ctx.cx,
        ctx.cy,
        ctx.fx,
        ctx.fy,
        float(ctx.shot_pan[source_idx]),
        float(ctx.shot_tilt[source_idx]),
    )
    return {
        "px": px,
        "py": py,
        "method": "coordinate_map_source_pixel",
        "pan": pan,
        "tilt": tilt,
        "in_frame": True,
        "has_source": True,
        "source": {
            "owner_index": source_idx,
            "base_owner_index": idx,
            "shot_file": shot_file,
            "shot_pan": float(ctx.shot_pan[source_idx]),
            "shot_tilt": float(ctx.shot_tilt[source_idx]),
            "source_px": sx_used,
            "source_py": sy_used,
            "base_source_px": sx,
            "base_source_py": sy,
            "correction": correction,
            "trusted_core": core["trusted_core"],
            "center_distance_px": core["center_distance_px"],
            "center_norm": core["center_norm"],
        },
        "trusted_core": core["trusted_core"],
        "trusted_core_half_ratio": core["trusted_core_half_ratio"],
        "center_norm": core["center_norm"],
        "core_bounds": core["core_bounds"],
        "coverage_count": coverage,
        "correction": correction,
    }


def _visual_refine_pano_pixel_with_context(
    ctx: _HuginVisualBatchContext,
    point: tuple[float, float],
    radius: int = DEFAULT_VISUAL_REFINE_RADIUS,
    patch: int = DEFAULT_VISUAL_REFINE_PATCH,
    trusted_core_half_ratio: float | None = None,
    use_visual_match: bool = False,
) -> dict:
    base = _coordinate_map_point_from_context(
        ctx,
        point,
        trusted_core_half_ratio=trusted_core_half_ratio,
        apply_correction=not use_visual_match,
    )
    source = base.get("source") or {}
    if not source:
        return {**base, "reliable": False, "method": "hugin_map_no_source"}
    if base.get("correction") and not use_visual_match:
        return {
            **base,
            "method": "hugin_correction_geometric",
            "reliable": True,
            "match_score": base["correction"]["score"],
            "visual_shift_px": {
                "dx": base["correction"]["dx"],
                "dy": base["correction"]["dy"],
                "distance": float(math.hypot(base["correction"]["dx"], base["correction"]["dy"])),
            },
        }
    coverage_count = int(base.get("coverage_count") or 0)
    if base.get("trusted_core") and not use_visual_match and coverage_count <= 1:
        return {
            **base,
            "method": "hugin_core_geometric",
            "reliable": True,
            "match_score": None,
            "visual_shift_px": None,
        }
    if not use_visual_match:
        return {
            **base,
            "method": "hugin_overlap_uncorrected" if coverage_count > 1 else "hugin_map_geometric_fallback",
            "reliable": False,
            "match_score": None,
            "visual_shift_px": None,
        }

    correction_hint = ctx.correction_at(point[0], point[1])
    source_owner = source.get("owner_index")
    if (
        correction_hint
        and correction_hint.get("source_owner") is not None
        and 0 <= int(correction_hint["source_owner"]) < len(ctx.shot_files)
    ):
        source_owner = int(correction_hint["source_owner"])
        src_path = Path(str(ctx.shot_files[source_owner]))
    else:
        src_path = Path(str(source["shot_file"]))
    pano_img = ctx.pano_img
    src_img = ctx.source_image(str(src_path))
    pano_patch = _crop_center(pano_img, point[0], point[1], patch)
    if pano_patch is None:
        return {**base, "reliable": False, "method": "hugin_visual_edge_fallback"}

    base_sx = float(source["source_px"])
    base_sy = float(source["source_py"])
    predicted_sx = base_sx
    predicted_sy = base_sy
    if correction_hint:
        if correction_hint.get("source_px") is not None and correction_hint.get("source_py") is not None:
            predicted_sx = float(correction_hint["source_px"])
            predicted_sy = float(correction_hint["source_py"])
        elif source_owner == source.get("owner_index"):
            predicted_sx = base_sx + float(correction_hint["dx"])
            predicted_sy = base_sy + float(correction_hint["dy"])
    margin = patch // 2
    sx0 = int(max(0, math.floor(predicted_sx - radius - margin)))
    sy0 = int(max(0, math.floor(predicted_sy - radius - margin)))
    sx1 = int(min(src_img.shape[1], math.ceil(predicted_sx + radius + margin + 1)))
    sy1 = int(min(src_img.shape[0], math.ceil(predicted_sy + radius + margin + 1)))
    window = src_img[sy0:sy1, sx0:sx1]
    if window.shape[0] < patch or window.shape[1] < patch:
        return {**base, "reliable": False, "method": "hugin_visual_window_fallback"}

    matched = _match_patch_in_window(pano_patch, window, sx0, sy0, margin)
    visual_shift_x = matched["x"] - base_sx
    visual_shift_y = matched["y"] - base_sy
    local_shift_x = matched["x"] - predicted_sx
    local_shift_y = matched["y"] - predicted_sy
    refined = ctx.refined_single_image_ptz(src_path, matched["x"], matched["y"])
    return {
        "px": float(point[0]),
        "py": float(point[1]),
        "pan": float(refined["pan"]),
        "tilt": float(refined["tilt"]),
        "in_frame": True,
        "method": "hugin_pano_visual",
        "reliable": matched["score"] >= VISUAL_MATCH_RELIABLE_SCORE,
        "match_score": matched["score"],
        "base_angle": {
            "pan": float(base.get("pan")) if base.get("pan") is not None else None,
            "tilt": float(base.get("tilt")) if base.get("tilt") is not None else None,
        },
        "trusted_core": bool(base.get("trusted_core", False)),
        "trusted_core_half_ratio": base.get("trusted_core_half_ratio"),
        "center_norm": base.get("center_norm"),
        "core_bounds": base.get("core_bounds"),
        "coverage_count": base.get("coverage_count"),
        "visual_shift_px": {
            "dx": float(visual_shift_x),
            "dy": float(visual_shift_y),
            "distance": float(math.hypot(visual_shift_x, visual_shift_y)),
            "local_dx": float(local_shift_x),
            "local_dy": float(local_shift_y),
            "local_distance": float(math.hypot(local_shift_x, local_shift_y)),
        },
        "correction_hint": correction_hint,
        "source": {
            "shot_file": str(src_path),
            "shot_pan": float(ctx.shot_pan[int(source_owner)]) if source_owner is not None else float(source["shot_pan"]),
            "shot_tilt": float(ctx.shot_tilt[int(source_owner)]) if source_owner is not None else float(source["shot_tilt"]),
            "source_px": matched["x"],
            "source_py": matched["y"],
            "base_source_px": base_sx,
            "base_source_py": base_sy,
            "predicted_source_px": predicted_sx,
            "predicted_source_py": predicted_sy,
        },
    }


def _visual_refine_pano_pixel(
    map_path: Path,
    point: tuple[float, float],
    radius: int = DEFAULT_VISUAL_REFINE_RADIUS,
    patch: int = DEFAULT_VISUAL_REFINE_PATCH,
    trusted_core_half_ratio: float | None = None,
    use_visual_match: bool = False,
) -> dict:
    ctx = _HuginVisualBatchContext(map_path)
    return _visual_refine_pano_pixel_with_context(
        ctx,
        point,
        radius=radius,
        patch=patch,
        trusted_core_half_ratio=trusted_core_half_ratio,
        use_visual_match=use_visual_match,
    )


def pixels_to_angles_visual(
    map_path: Path,
    points: list[tuple[float, float]],
    radius: int = DEFAULT_VISUAL_REFINE_RADIUS,
    patch: int = DEFAULT_VISUAL_REFINE_PATCH,
    trusted_core_half_ratio: float | None = None,
    use_visual_match: bool = False,
) -> list[dict]:
    ctx = _HuginVisualBatchContext(map_path)
    return [
        _visual_refine_pano_pixel_with_context(
            ctx,
            point,
            radius=radius,
            patch=patch,
            trusted_core_half_ratio=trusted_core_half_ratio,
            use_visual_match=use_visual_match,
        )
        for point in points
    ]


def _absolute_ptz_to_pixel(pan, tilt, pan0, tilt0, cx, cy, fx, fy, img_w, img_h):
    p_t, t_t = math.radians(pan), math.radians(tilt)
    p0, t0 = math.radians(pan0), math.radians(tilt0)
    wx = math.sin(p_t) * math.cos(t_t)
    wy = math.sin(t_t)
    wz = math.cos(p_t) * math.cos(t_t)
    tx = wx * math.cos(p0) - wz * math.sin(p0)
    ty = wy
    tz = wx * math.sin(p0) + wz * math.cos(p0)
    rx = tx
    ry = ty * math.cos(t0) - tz * math.sin(t0)
    rz = ty * math.sin(t0) + tz * math.cos(t0)
    if rz <= 0:
        return None
    px = cx + fx * (rx / rz)
    py = cy - fy * (ry / rz)
    if not (0 <= px < img_w and 0 <= py < img_h):
        return None
    return float(px), float(py)


def _locate_source_pixel(data, owner_index: int, source_point: tuple[float, float]) -> dict | None:
    owner = data["owner"]
    source_x = data["source_x"]
    source_y = data["source_y"]
    sx, sy = source_point
    mask = owner == owner_index
    if not np.any(mask):
        return None
    dist = np.full(owner.shape, np.inf, dtype=np.float32)
    dist[mask] = (source_x[mask] - sx) ** 2 + (source_y[mask] - sy) ** 2
    flat = int(np.argmin(dist))
    y, x = divmod(flat, owner.shape[1])
    if not np.isfinite(float(dist[y, x])):
        return None
    return {"px": float(x), "py": float(y), "source_error_px": float(np.sqrt(dist[y, x]))}


def _angle_to_pixel_visual_with_context(
    ctx: _HuginVisualBatchContext,
    pan: float,
    tilt: float,
    radius: int = DEFAULT_VISUAL_REFINE_RADIUS,
    patch: int = DEFAULT_VISUAL_REFINE_PATCH,
    trusted_core_half_ratio: float | None = None,
    use_visual_match: bool = False,
) -> dict:
    shot_pan = ctx.shot_pan
    shot_tilt = ctx.shot_tilt
    shot_files = ctx.shot_files
    fx, fy, cx, cy = ctx.fx, ctx.fy, ctx.cx, ctx.cy
    pano_img = ctx.pano_img

    candidates = []
    for idx, shot_file in enumerate(shot_files):
        src_img = ctx.source_image(shot_file)
        source = _absolute_ptz_to_pixel(
            pan, tilt, float(shot_pan[idx]), float(shot_tilt[idx]), cx, cy, fx, fy, src_img.shape[1], src_img.shape[0]
        )
        if source is None:
            continue
        geo = _locate_source_pixel(ctx.data, idx, source)
        if not geo:
            continue
        core = _source_core_info(ctx, source[0], source[1], src_img=src_img, half_ratio=trusted_core_half_ratio)
        center_dist = math.hypot(source[0] - cx, source[1] - cy)
        if core["trusted_core"]:
            candidates.append(
                {
                    "shot_file": shot_file,
                    "shot_pan": float(shot_pan[idx]),
                    "shot_tilt": float(shot_tilt[idx]),
                    "source_px": source[0],
                    "source_py": source[1],
                    "source_center_distance_px": center_dist,
                    "geometric": geo,
                    "visual": None,
                    "trusted_core": True,
                    "center_norm": core["center_norm"],
                    "core_bounds": core["core_bounds"],
                }
            )
            continue
        visual = None
        if use_visual_match:
            source_patch = _crop_center(src_img, source[0], source[1], patch)
        else:
            source_patch = None
        if source_patch is not None:
            margin = patch // 2
            px0 = int(max(0, math.floor(geo["px"] - radius - margin)))
            py0 = int(max(0, math.floor(geo["py"] - radius - margin)))
            px1 = int(min(pano_img.shape[1], math.ceil(geo["px"] + radius + margin + 1)))
            py1 = int(min(pano_img.shape[0], math.ceil(geo["py"] + radius + margin + 1)))
            window = pano_img[py0:py1, px0:px1]
            if window.shape[0] >= patch and window.shape[1] >= patch:
                matched = _match_patch_in_window(source_patch, window, px0, py0, margin)
                visual = {
                    "px": matched["x"],
                    "py": matched["y"],
                    "match_score": matched["score"],
                    "pano_shift_px": {"dx": matched["x"] - geo["px"], "dy": matched["y"] - geo["py"]},
                }
        candidates.append(
            {
                "shot_file": shot_file,
                "shot_pan": float(shot_pan[idx]),
                "shot_tilt": float(shot_tilt[idx]),
                "source_px": source[0],
                "source_py": source[1],
                "source_center_distance_px": center_dist,
                "geometric": geo,
                "visual": visual,
                "trusted_core": False,
                "center_norm": core["center_norm"],
                "core_bounds": core["core_bounds"],
            }
        )

    if not candidates:
        return {"pan": float(pan), "tilt": float(tilt), "in_frame": False, "reliable": False}
    trusted = [item for item in candidates if item.get("trusted_core")]
    if trusted:
        trusted.sort(key=lambda item: float(item["source_center_distance_px"]))
        best = trusted[0]
        return {
            "pan": float(pan),
            "tilt": float(tilt),
            "px": float(best["geometric"]["px"]),
            "py": float(best["geometric"]["py"]),
            "in_frame": True,
            "method": "hugin_angle_core_geometric",
            "reliable": True,
            "match_score": None,
            "source": {k: best[k] for k in ("shot_file", "shot_pan", "shot_tilt", "source_px", "source_py")},
            "geometric": best["geometric"],
            "visual": None,
            "trusted_core": True,
            "trusted_core_half_ratio": _trusted_core_half_ratio(trusted_core_half_ratio),
            "center_norm": best["center_norm"],
            "core_bounds": best["core_bounds"],
            "candidate_count": len(candidates),
        }
    candidates.sort(
        key=lambda item: (
            float((item.get("visual") or {}).get("match_score", -1.0)),
            -float(item["source_center_distance_px"]),
        ),
        reverse=True,
    )
    best = candidates[0]
    visual = best.get("visual")
    if visual:
        px, py = float(visual["px"]), float(visual["py"])
        score = float(visual["match_score"])
        method = "hugin_angle_visual"
        reliable = score >= VISUAL_MATCH_RELIABLE_SCORE
    else:
        px, py = float(best["geometric"]["px"]), float(best["geometric"]["py"])
        score = None
        method = "hugin_angle_geometric_fallback"
        reliable = False
    return {
        "pan": float(pan),
        "tilt": float(tilt),
        "px": px,
        "py": py,
        "in_frame": True,
        "method": method,
        "reliable": reliable,
        "match_score": score,
        "source": {k: best[k] for k in ("shot_file", "shot_pan", "shot_tilt", "source_px", "source_py")},
        "geometric": best["geometric"],
        "visual": visual,
        "trusted_core": False,
        "trusted_core_half_ratio": _trusted_core_half_ratio(trusted_core_half_ratio),
        "center_norm": best.get("center_norm"),
        "core_bounds": best.get("core_bounds"),
        "candidate_count": len(candidates),
    }


def angle_to_pixel_visual(
    map_path: Path,
    pan: float,
    tilt: float,
    radius: int = DEFAULT_VISUAL_REFINE_RADIUS,
    patch: int = DEFAULT_VISUAL_REFINE_PATCH,
    trusted_core_half_ratio: float | None = None,
    use_visual_match: bool = False,
) -> dict:
    ctx = _HuginVisualBatchContext(map_path)
    return _angle_to_pixel_visual_with_context(
        ctx,
        pan,
        tilt,
        radius=radius,
        patch=patch,
        trusted_core_half_ratio=trusted_core_half_ratio,
        use_visual_match=use_visual_match,
    )


def angles_to_pixels_visual(
    map_path: Path,
    angles: list[tuple[float, float]],
    radius: int = DEFAULT_VISUAL_REFINE_RADIUS,
    patch: int = DEFAULT_VISUAL_REFINE_PATCH,
    trusted_core_half_ratio: float | None = None,
    use_visual_match: bool = False,
) -> list[dict]:
    ctx = _HuginVisualBatchContext(map_path)
    return [
        _angle_to_pixel_visual_with_context(
            ctx,
            pan,
            tilt,
            radius=radius,
            patch=patch,
            trusted_core_half_ratio=trusted_core_half_ratio,
            use_visual_match=use_visual_match,
        )
        for pan, tilt in angles
    ]
