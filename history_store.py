import json
import os
import sqlite3
import time
import uuid
from contextlib import contextmanager
from datetime import datetime, timezone


BASE_DIR = os.path.dirname(os.path.abspath(__file__))
DEFAULT_HISTORY_DIR = os.path.join(BASE_DIR, "data", "history")
DEFAULT_HISTORY_DB = os.path.join(DEFAULT_HISTORY_DIR, "history.db")
HISTORY_DB_PATH = os.getenv("HISTORY_DB_PATH", DEFAULT_HISTORY_DB)


def utc_now_iso():
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def _ensure_parent_dir(path):
    parent = os.path.dirname(path)
    if parent:
        os.makedirs(parent, exist_ok=True)


@contextmanager
def _connect():
    _ensure_parent_dir(HISTORY_DB_PATH)
    conn = sqlite3.connect(HISTORY_DB_PATH, timeout=5)
    conn.row_factory = sqlite3.Row
    try:
        conn.execute("PRAGMA journal_mode=WAL;")
        conn.execute("PRAGMA synchronous=NORMAL;")
        conn.execute("PRAGMA busy_timeout=5000;")
        yield conn
        conn.commit()
    finally:
        conn.close()


def init_db():
    with _connect() as conn:
        conn.executescript(
            """
            CREATE TABLE IF NOT EXISTS projects (
                project_id TEXT PRIMARY KEY,
                project_name TEXT NOT NULL,
                scan_type TEXT NOT NULL,
                status TEXT NOT NULL,
                started_at TEXT NOT NULL,
                ended_at TEXT,
                scan_params_json TEXT NOT NULL DEFAULT '{}',
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL
            );

            CREATE TABLE IF NOT EXISTS snapshots (
                snapshot_id INTEGER PRIMARY KEY AUTOINCREMENT,
                project_id TEXT NOT NULL,
                round_id TEXT NOT NULL,
                round_index INTEGER,
                captured_at TEXT NOT NULL,
                scan_type TEXT NOT NULL,
                status TEXT NOT NULL,
                archive_state TEXT NOT NULL DEFAULT 'NO_MEDIA',
                panorama_path TEXT,
                thumbnail_path TEXT,
                raw_dir TEXT,
                grid_data_json TEXT NOT NULL,
                render_meta_json TEXT NOT NULL,
                created_at TEXT NOT NULL,
                UNIQUE(project_id, round_id),
                FOREIGN KEY(project_id) REFERENCES projects(project_id)
            );

            CREATE INDEX IF NOT EXISTS idx_projects_status
            ON projects(status, started_at DESC);

            CREATE INDEX IF NOT EXISTS idx_snapshots_project_time
            ON snapshots(project_id, captured_at DESC);
            """
        )


def _row_to_dict(row):
    if row is None:
        return None
    return dict(row)


def generate_project_id(scan_type):
    return f"{scan_type}_{int(time.time())}_{uuid.uuid4().hex[:6]}"


def create_project(project_name, scan_type, scan_params=None):
    init_db()
    now = utc_now_iso()
    project_id = generate_project_id(scan_type)
    payload = json.dumps(scan_params or {}, ensure_ascii=False)
    with _connect() as conn:
        conn.execute(
            """
            INSERT INTO projects (
                project_id, project_name, scan_type, status,
                started_at, ended_at, scan_params_json, created_at, updated_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (project_id, project_name, scan_type, "RUNNING", now, None, payload, now, now),
        )
    return get_project(project_id)


def update_project_status(project_id, status, ended_at=None):
    init_db()
    now = utc_now_iso()
    with _connect() as conn:
        conn.execute(
            """
            UPDATE projects
            SET status = ?, ended_at = COALESCE(?, ended_at), updated_at = ?
            WHERE project_id = ?
            """,
            (status, ended_at, now, project_id),
        )
    return get_project(project_id)


def finish_project(project_id, status="STOPPED"):
    return update_project_status(project_id, status, ended_at=utc_now_iso())


def get_project(project_id):
    init_db()
    with _connect() as conn:
        row = conn.execute(
            """
            SELECT project_id, project_name, scan_type, status,
                   started_at, ended_at, scan_params_json, created_at, updated_at
            FROM projects
            WHERE project_id = ?
            """,
            (project_id,),
        ).fetchone()
    project = _row_to_dict(row)
    if project and project.get("scan_params_json"):
        project["scan_params"] = json.loads(project["scan_params_json"])
    return project


def list_projects(limit=100):
    init_db()
    with _connect() as conn:
        rows = conn.execute(
            """
            SELECT project_id, project_name, scan_type, status,
                   started_at, ended_at, scan_params_json, created_at, updated_at
            FROM projects
            ORDER BY started_at DESC
            LIMIT ?
            """,
            (int(limit),),
        ).fetchall()
    projects = []
    for row in rows:
        item = dict(row)
        if item.get("scan_params_json"):
            item["scan_params"] = json.loads(item["scan_params_json"])
        projects.append(item)
    return projects


def add_snapshot(
    project_id,
    round_id,
    round_index,
    captured_at,
    scan_type,
    grid_data,
    render_meta,
    status="SUCCESS",
    archive_state="NO_MEDIA",
    panorama_path=None,
    thumbnail_path=None,
    raw_dir=None,
):
    init_db()
    now = utc_now_iso()
    grid_data_json = json.dumps(grid_data or {}, ensure_ascii=False)
    render_meta_json = json.dumps(render_meta or {}, ensure_ascii=False)
    with _connect() as conn:
        conn.execute(
            """
            INSERT OR REPLACE INTO snapshots (
                project_id, round_id, round_index, captured_at, scan_type,
                status, archive_state, panorama_path, thumbnail_path, raw_dir,
                grid_data_json, render_meta_json, created_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                project_id,
                round_id,
                round_index,
                captured_at,
                scan_type,
                status,
                archive_state,
                panorama_path,
                thumbnail_path,
                raw_dir,
                grid_data_json,
                render_meta_json,
                now,
            ),
        )
    return get_snapshot_by_round(project_id, round_id)


def _decode_snapshot(snapshot):
    if snapshot is None:
        return None
    if snapshot.get("grid_data_json"):
        snapshot["grid_data"] = json.loads(snapshot["grid_data_json"])
    if snapshot.get("render_meta_json"):
        snapshot["render_meta"] = json.loads(snapshot["render_meta_json"])
    return snapshot


def get_snapshot_by_round(project_id, round_id):
    init_db()
    with _connect() as conn:
        row = conn.execute(
            """
            SELECT snapshot_id, project_id, round_id, round_index, captured_at,
                   scan_type, status, archive_state, panorama_path, thumbnail_path,
                   raw_dir, grid_data_json, render_meta_json, created_at
            FROM snapshots
            WHERE project_id = ? AND round_id = ?
            """,
            (project_id, round_id),
        ).fetchone()
    return _decode_snapshot(_row_to_dict(row))


def list_snapshots(project_id, limit=200):
    init_db()
    with _connect() as conn:
        rows = conn.execute(
            """
            SELECT snapshot_id, project_id, round_id, round_index, captured_at,
                   scan_type, status, archive_state, panorama_path, thumbnail_path,
                   raw_dir, created_at
            FROM snapshots
            WHERE project_id = ?
            ORDER BY captured_at DESC
            LIMIT ?
            """,
            (project_id, int(limit)),
        ).fetchall()
    return [dict(row) for row in rows]


def get_snapshot_at(project_id, timestamp):
    init_db()
    with _connect() as conn:
        row = conn.execute(
            """
            SELECT snapshot_id, project_id, round_id, round_index, captured_at,
                   scan_type, status, archive_state, panorama_path, thumbnail_path,
                   raw_dir, grid_data_json, render_meta_json, created_at
            FROM snapshots
            WHERE project_id = ? AND captured_at <= ?
            ORDER BY captured_at DESC
            LIMIT 1
            """,
            (project_id, timestamp),
        ).fetchone()
    return _decode_snapshot(_row_to_dict(row))


def get_latest_snapshot(project_id):
    init_db()
    with _connect() as conn:
        row = conn.execute(
            """
            SELECT snapshot_id, project_id, round_id, round_index, captured_at,
                   scan_type, status, archive_state, panorama_path, thumbnail_path,
                   raw_dir, grid_data_json, render_meta_json, created_at
            FROM snapshots
            WHERE project_id = ?
            ORDER BY captured_at DESC
            LIMIT 1
            """,
            (project_id,),
        ).fetchone()
    return _decode_snapshot(_row_to_dict(row))
