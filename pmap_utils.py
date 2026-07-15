from __future__ import annotations

import json
import struct
from pathlib import Path
from typing import Any

import numpy as np


PMAP_MAGIC = b"PMAP0001"
PMAP_VERSION = 1
PMAP_INVALID_INT16 = -32768
PMAP_HEADER = struct.Struct("<8sHHIIhHQ")

PCAND_MAGIC = b"PCAND001"
PCAND_VERSION = 1
PCAND_HEADER = struct.Struct("<8sHHQ")
PCAND_RECORD_DTYPE = np.dtype(
    [
        ("pano_x", "<u4"),
        ("pano_y", "<u4"),
        ("image_index", "<i2"),
        ("source_x", "<i2"),
        ("source_y", "<i2"),
        ("center_distance", "<f4"),
        ("center_norm", "<f4"),
        ("flags", "<u2"),
        ("reserved", "<u2"),
    ]
)

PCAND_FLAG_CENTER_NEAREST = 1 << 0


def _metadata_bytes(metadata: dict[str, Any] | None) -> bytes:
    return json.dumps(metadata or {}, ensure_ascii=False, separators=(",", ":")).encode("utf-8")


def _as_planar_array(array, dtype, shape: tuple[int, int], name: str):
    arr = np.asarray(array)
    if arr.shape != shape:
        raise ValueError(f"{name} shape {arr.shape} does not match expected {shape}")
    return arr.astype(dtype, copy=False)


def write_pmap(
    path: Path,
    owner,
    source_x,
    source_y,
    coverage,
    metadata: dict[str, Any] | None = None,
    invalid: int = PMAP_INVALID_INT16,
) -> dict[str, Any]:
    owner_arr = np.asarray(owner)
    if owner_arr.ndim != 2:
        raise ValueError("owner must be a 2D array")
    height, width = owner_arr.shape
    shape = (height, width)
    owner_arr = _as_planar_array(owner_arr, "<i2", shape, "owner")
    sx_arr = _as_planar_array(source_x, "<i2", shape, "source_x")
    sy_arr = _as_planar_array(source_y, "<i2", shape, "source_y")
    cov_arr = _as_planar_array(coverage, "<u2", shape, "coverage")

    meta = dict(metadata or {})
    meta.setdefault("schema", "pmap_dense_planar_v1")
    meta["width"] = int(width)
    meta["height"] = int(height)
    meta["invalid_int16"] = int(invalid)
    meta["layout"] = ["owner:int16", "source_x:int16", "source_y:int16", "coverage:uint16"]
    meta_bytes = _metadata_bytes(meta)

    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("wb") as fh:
        fh.write(
            PMAP_HEADER.pack(
                PMAP_MAGIC,
                PMAP_VERSION,
                0,
                int(width),
                int(height),
                int(invalid),
                0,
                len(meta_bytes),
            )
        )
        fh.write(meta_bytes)
        fh.write(np.ascontiguousarray(owner_arr).tobytes(order="C"))
        fh.write(np.ascontiguousarray(sx_arr).tobytes(order="C"))
        fh.write(np.ascontiguousarray(sy_arr).tobytes(order="C"))
        fh.write(np.ascontiguousarray(cov_arr).tobytes(order="C"))

    return {
        "path": str(path),
        "schema": meta["schema"],
        "shape": [int(width), int(height)],
        "valid_pixels": int((owner_arr != int(invalid)).sum()),
        "overlap_pixels": int((cov_arr > 1).sum()),
        "size_bytes": int(path.stat().st_size),
    }


def read_pmap_header(path: Path) -> dict[str, Any]:
    path = Path(path)
    with path.open("rb") as fh:
        raw = fh.read(PMAP_HEADER.size)
        if len(raw) != PMAP_HEADER.size:
            raise RuntimeError(f"PMAP header is truncated: {path}")
        magic, version, flags, width, height, invalid, _reserved, meta_len = PMAP_HEADER.unpack(raw)
        if magic != PMAP_MAGIC:
            raise RuntimeError(f"unsupported PMAP magic in {path}: {magic!r}")
        if version != PMAP_VERSION:
            raise RuntimeError(f"unsupported PMAP version in {path}: {version}")
        meta_raw = fh.read(meta_len)
        if len(meta_raw) != meta_len:
            raise RuntimeError(f"PMAP metadata is truncated: {path}")
    metadata = json.loads(meta_raw.decode("utf-8")) if meta_raw else {}
    return {
        "path": str(path),
        "version": int(version),
        "flags": int(flags),
        "width": int(width),
        "height": int(height),
        "invalid_int16": int(invalid),
        "metadata_len": int(meta_len),
        "data_offset": int(PMAP_HEADER.size + meta_len),
        "metadata": metadata,
    }


def load_pmap(path: Path, mmap: bool = True) -> dict[str, Any]:
    header = read_pmap_header(path)
    width = int(header["width"])
    height = int(header["height"])
    count = width * height
    offset = int(header["data_offset"])
    item_bytes = count * 2
    path = Path(path)

    def array_at(dtype, index: int):
        arr_offset = offset + index * item_bytes
        if mmap:
            return np.memmap(path, dtype=dtype, mode="r", offset=arr_offset, shape=(height, width))
        with path.open("rb") as fh:
            fh.seek(arr_offset)
            data = np.frombuffer(fh.read(item_bytes), dtype=dtype, count=count)
        return data.reshape((height, width)).copy()

    return {
        "owner": array_at("<i2", 0),
        "source_x": array_at("<i2", 1),
        "source_y": array_at("<i2", 2),
        "coverage_count": array_at("<u2", 3),
        "metadata": header["metadata"],
        "header": header,
    }


def quantize_source_array(values, valid_mask, invalid: int = PMAP_INVALID_INT16):
    out = np.full(np.asarray(values).shape, int(invalid), dtype=np.int16)
    rounded = np.rint(np.asarray(values, dtype=np.float64))
    clipped = np.clip(rounded, -32767, 32767).astype(np.int16)
    out[valid_mask] = clipped[valid_mask]
    return out


def write_pcand(path: Path, records, metadata: dict[str, Any] | None = None) -> dict[str, Any]:
    arr = np.asarray(records, dtype=PCAND_RECORD_DTYPE)
    meta = dict(metadata or {})
    meta.setdefault("schema", "pmap_overlap_candidates_v1")
    meta["record_dtype"] = str(PCAND_RECORD_DTYPE.descr)
    meta["record_count"] = int(arr.shape[0])
    meta_bytes = _metadata_bytes(meta)

    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("wb") as fh:
        fh.write(PCAND_HEADER.pack(PCAND_MAGIC, PCAND_VERSION, 0, len(meta_bytes)))
        fh.write(meta_bytes)
        fh.write(np.ascontiguousarray(arr).tobytes(order="C"))
    return {
        "path": str(path),
        "schema": meta["schema"],
        "records": int(arr.shape[0]),
        "size_bytes": int(path.stat().st_size),
    }


def read_pcand_header(path: Path) -> dict[str, Any]:
    path = Path(path)
    with path.open("rb") as fh:
        raw = fh.read(PCAND_HEADER.size)
        if len(raw) != PCAND_HEADER.size:
            raise RuntimeError(f"PCAND header is truncated: {path}")
        magic, version, flags, meta_len = PCAND_HEADER.unpack(raw)
        if magic != PCAND_MAGIC:
            raise RuntimeError(f"unsupported PCAND magic in {path}: {magic!r}")
        if version != PCAND_VERSION:
            raise RuntimeError(f"unsupported PCAND version in {path}: {version}")
        meta_raw = fh.read(meta_len)
        if len(meta_raw) != meta_len:
            raise RuntimeError(f"PCAND metadata is truncated: {path}")
    metadata = json.loads(meta_raw.decode("utf-8")) if meta_raw else {}
    return {
        "path": str(path),
        "version": int(version),
        "flags": int(flags),
        "metadata_len": int(meta_len),
        "data_offset": int(PCAND_HEADER.size + meta_len),
        "metadata": metadata,
    }


def load_pcand(path: Path, mmap: bool = True):
    header = read_pcand_header(path)
    path = Path(path)
    record_count = int((path.stat().st_size - int(header["data_offset"])) // PCAND_RECORD_DTYPE.itemsize)
    if mmap:
        records = np.memmap(
            path,
            dtype=PCAND_RECORD_DTYPE,
            mode="r",
            offset=int(header["data_offset"]),
            shape=(record_count,),
        )
    else:
        with path.open("rb") as fh:
            fh.seek(int(header["data_offset"]))
            records = np.frombuffer(fh.read(), dtype=PCAND_RECORD_DTYPE, count=record_count).copy()
    return {"records": records, "metadata": header["metadata"], "header": header}
