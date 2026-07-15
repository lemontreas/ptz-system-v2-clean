"""全面扫描图像上下文解析（纯函数，可独立测试）。

从 web_server.py 提取，供 Flask 路由和测试共同使用。
"""

import json
import re
from pathlib import Path


SINGLE_CAPTURE_RE = re.compile(
    r"^snap_pan(?P<pan>[-+]?(?:\d+(?:\.\d*)?|\.\d+))"
    r"_tilt(?P<tilt>[-+]?(?:\d+(?:\.\d*)?|\.\d+))_"
)

IMAGE_URL_PREFIX = "/api/v1/camera/images/"


def normalize_image_url(file_path, captures_dir):
    """将绝对路径规范化为 /api/v1/camera/images/... 格式。

    要求 file_path resolve 后位于 captures_dir 内，保留完整子目录。
    """
    resolved = Path(file_path).resolve()
    root = Path(captures_dir).resolve()
    if resolved == root:
        raise ValueError(f"路径不是文件: {resolved}")
    try:
        rel = resolved.relative_to(root)
    except ValueError:
        raise ValueError(f"路径不在 captures 目录内: {resolved}")
    return f"{IMAGE_URL_PREFIX}{rel.as_posix()}"


def resolve_panorama_image_context(
    meta, normalized_url, validate_paths=True, captures_dir=None
):
    """从全景 metadata dict 构建 image_context。

    Args:
        meta: 全景 metadata dict（来自 Redis 或 _meta.json）。
        normalized_url: 已规范化的 image_url。
        validate_paths: 是否校验文件存在（测试时可关闭）。
        captures_dir: captures 根目录；传入时用于一致性校验，为 None 时自动猜测。
    """
    # 要求 backend = hugin
    backend = meta.get("backend")
    if backend != "hugin":
        raise ValueError(
            f"全景 metadata backend 须为 'hugin'，收到: {backend!r}"
        )

    # 校验图片
    meta_image_path = meta.get("image_path")
    if validate_paths:
        if not meta_image_path or not Path(str(meta_image_path)).is_file():
            raise FileNotFoundError(
                f"全景图片文件不存在: {meta_image_path}"
            )

    # 显式 image_url 时校验一致性
    if normalized_url and meta_image_path:
        _cap_dir = captures_dir if captures_dir is not None else _guess_captures_dir(meta_image_path)
        url_from_meta = normalize_image_url(meta_image_path, _cap_dir)
        if normalized_url != url_from_meta:
            raise ValueError(
                f"image_url 与全景 metadata image_path 不一致: "
                f"{normalized_url} vs {url_from_meta}"
            )

    # canvas_size
    canvas_size = meta.get("canvas_size")
    if (
        not canvas_size
        or not isinstance(canvas_size, (list, tuple))
        or len(canvas_size) != 2
    ):
        raise ValueError("全景图元数据缺少有效的 canvas_size")
    width, height = int(canvas_size[0]), int(canvas_size[1])
    if width <= 0 or height <= 0:
        raise ValueError(f"全景图 canvas_size 无效: {width}x{height}")

    # pmap_path
    pmap_path = meta.get("pmap_path")
    if validate_paths:
        if not pmap_path or not Path(str(pmap_path)).is_file():
            raise FileNotFoundError(
                f"全景图 PMAP 文件不存在: {pmap_path}"
            )

    # session_json
    session_json = meta.get("session_json")
    if not session_json:
        raise ValueError("全景图元数据缺少 session_json")
    if validate_paths and not Path(str(session_json)).is_file():
        raise FileNotFoundError(
            f"全景图 session_json 文件不存在: {session_json}"
        )

    return {
        "mode": "panorama",
        "image_url": normalized_url,
        "width": width,
        "height": height,
        "pmap_path": str(pmap_path),
        "session_json": str(session_json),
    }


def resolve_single_image_context(
    image_path, width, height, intrinsics_fn=None
):
    """从单图文件构建 image_context。

    Args:
        image_path: 图片的 Path 对象。
        width: 图片实际宽度（像素）。
        height: 图片实际高度（像素）。
        intrinsics_fn: 可选的 () -> (fx,fy,cx,cy) 回调。
    """
    match = SINGLE_CAPTURE_RE.match(Path(image_path).name)
    if not match:
        raise ValueError(
            f"单图文件名无法解析角度: {Path(image_path).name}"
        )
    capture_pan = float(match.group("pan"))
    capture_tilt = float(match.group("tilt"))

    intrinsics = {}
    if intrinsics_fn:
        try:
            fx, fy, cx, cy = intrinsics_fn()
            intrinsics = {"fx": fx, "fy": fy, "cx": cx, "cy": cy}
        except Exception as e:
            raise RuntimeError(f"标定参数加载失败: {e}")

    ctx = {
        "mode": "single",
        "image_url": None,  # 由调用方填充
        "width": int(width),
        "height": int(height),
        "capture_pan": capture_pan,
        "capture_tilt": capture_tilt,
        "intrinsics": intrinsics,
    }
    return ctx


def _guess_captures_dir(image_path_str):
    """从绝对路径猜测 captures_dir（用于一致性校验）。

    查找路径中包含 'captures' 的部分作为根目录。
    """
    p = Path(image_path_str).resolve()
    parts = p.parts
    for i, part in enumerate(parts):
        if part == "captures":
            return Path(*parts[: i + 1])
    raise ValueError(f"无法从路径中猜测 captures 目录: {image_path_str}")
