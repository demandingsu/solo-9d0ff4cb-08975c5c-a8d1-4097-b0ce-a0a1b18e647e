"""SQLite 持久层：现势宗地、历史版本、变更记录、线路与巡检记录。

并发控制：每次提交必须携带 base_version，写入时在 IMMEDIATE 事务内
复核当前版本；版本不一致即拒绝并整体回滚，保证同一版上的两笔提交
最多一笔生效，且任何失败都不改动现势与历史数据。
巡检补正同样在事务内先重算整批：补正后无解即回滚，库内保持补正前状态。
"""
from __future__ import annotations

import json
import sqlite3
from datetime import datetime, timezone
from typing import Any

from shapely.geometry import mapping
from shapely import wkt as shapely_wkt

from .geometry import (
    GeoValidationError,
    PlanValidationError,
    ensure_no_overlap,
    parse_feature_collection,
    parse_polygon,
    plan_change,
)
from .alignment import (
    AlignmentError,
    check_continuity,
    covering_parcels,
    evaluate,
    parse_records,
    parse_segments,
    segments_to_payload,
)

SCHEMA = """
CREATE TABLE IF NOT EXISTS meta (
    key   TEXT PRIMARY KEY,
    value TEXT NOT NULL
);
-- 宗地版本行：append-only。同一 parcel_id 每个生效版本一行；
-- status 表示该宗地在该版本结束时的状态（current / retired），旧行永不 UPDATE。
CREATE TABLE IF NOT EXISTS parcel_versions (
    parcel_id      TEXT NOT NULL,
    version        INTEGER NOT NULL,
    status         TEXT NOT NULL CHECK (status IN ('current', 'retired')),
    geom_wkt       TEXT NOT NULL,
    area           REAL NOT NULL,
    created_txn_id INTEGER NOT NULL,
    retired_txn_id INTEGER,
    PRIMARY KEY (parcel_id, version)
);
CREATE INDEX IF NOT EXISTS idx_pv_status ON parcel_versions(status);
CREATE TABLE IF NOT EXISTS txns (
    txn_id       INTEGER PRIMARY KEY AUTOINCREMENT,
    kind         TEXT NOT NULL CHECK (kind IN ('import', 'change')),
    base_version INTEGER NOT NULL,
    new_version  INTEGER NOT NULL,
    reason       TEXT,
    report_json  TEXT,
    created_at   TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS txn_items (
    txn_id    INTEGER NOT NULL REFERENCES txns(txn_id),
    role      TEXT NOT NULL CHECK (role IN ('retired', 'new')),
    parcel_id TEXT NOT NULL,
    geom_wkt  TEXT NOT NULL,
    area      REAL NOT NULL
);
-- 线路：多条线路并存，segments_json 保存按行进顺序排列的段（含起止坐标与桩号）。
CREATE TABLE IF NOT EXISTS alignments (
    alignment_id   INTEGER PRIMARY KEY AUTOINCREMENT,
    name           TEXT NOT NULL,
    segments_json  TEXT NOT NULL,
    created_at     TEXT NOT NULL
);
-- 巡检记录：按实地先后排列（sort_no），segment_id 为资料员补填/更正的所在段。
CREATE TABLE IF NOT EXISTS inspections (
    inspection_id  INTEGER PRIMARY KEY AUTOINCREMENT,
    alignment_id   INTEGER NOT NULL REFERENCES alignments(alignment_id),
    sort_no        INTEGER NOT NULL,
    station        REAL NOT NULL,
    segment_id     TEXT,
    note           TEXT,
    created_at     TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_insp_alignment ON inspections(alignment_id, sort_no);
-- 补正历史：每次网页补填/更正所在段追加一行，旧值不改，便于追溯。
CREATE TABLE IF NOT EXISTS inspection_corrections (
    correction_id  INTEGER PRIMARY KEY AUTOINCREMENT,
    inspection_id  INTEGER NOT NULL REFERENCES inspections(inspection_id),
    old_segment_id TEXT,
    new_segment_id TEXT,
    created_at     TEXT NOT NULL
);
"""


class VersionConflictError(Exception):
    """base_version 与当前版本不一致（竞争提交的失败者）。"""


def connect(db_path: str) -> sqlite3.Connection:
    conn = sqlite3.connect(db_path, timeout=30)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    conn.execute("PRAGMA busy_timeout = 30000")
    return conn


def init_db(conn: sqlite3.Connection) -> None:
    conn.executescript(SCHEMA)
    conn.commit()


def current_version(conn: sqlite3.Connection) -> int:
    row = conn.execute("SELECT value FROM meta WHERE key = 'current_version'").fetchone()
    return int(row["value"]) if row else 0


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _parcel_to_geojson(row: sqlite3.Row) -> dict[str, Any]:
    geom = shapely_wkt.loads(row["geom_wkt"])
    return {
        "parcel_id": row["parcel_id"],
        "version": row["version"],
        "status": row["status"],
        "area": row["area"],
        "geometry": mapping(geom),
        "created_txn_id": row["created_txn_id"],
        "retired_txn_id": row["retired_txn_id"],
    }


def list_current(conn: sqlite3.Connection) -> list[dict]:
    rows = conn.execute(
        """
        SELECT pv.* FROM parcel_versions pv
        JOIN (SELECT parcel_id, MAX(version) AS mv
              FROM parcel_versions GROUP BY parcel_id) latest
          ON latest.parcel_id = pv.parcel_id AND latest.mv = pv.version
        WHERE pv.status = 'current'
        ORDER BY pv.parcel_id
        """
    ).fetchall()
    return [_parcel_to_geojson(r) for r in rows]


def _current_geoms(conn: sqlite3.Connection) -> dict[str, Any]:
    rows = conn.execute(
        """
        SELECT pv.parcel_id, pv.geom_wkt FROM parcel_versions pv
        JOIN (SELECT parcel_id, MAX(version) AS mv
              FROM parcel_versions GROUP BY parcel_id) latest
          ON latest.parcel_id = pv.parcel_id AND latest.mv = pv.version
        WHERE pv.status = 'current'
        """
    ).fetchall()
    return {r["parcel_id"]: shapely_wkt.loads(r["geom_wkt"]) for r in rows}


def parcel_history(conn: sqlite3.Connection, parcel_id: str) -> dict:
    rows = conn.execute(
        "SELECT * FROM parcel_versions WHERE parcel_id = ? ORDER BY version",
        (parcel_id,),
    ).fetchall()
    if not rows:
        raise PlanValidationError(f"宗地号 {parcel_id} 无任何历史记录")
    return {
        "parcel_id": parcel_id,
        "versions": [_parcel_to_geojson(r) for r in rows],
    }


def snapshot_at(conn: sqlite3.Connection, version: int) -> list[dict]:
    """截至某版本的现势快照：每宗地取 <= version 的最新行，排除当时已退休者。"""
    rows = conn.execute(
        """
        SELECT pv.* FROM parcel_versions pv
        JOIN (SELECT parcel_id, MAX(version) AS mv
              FROM parcel_versions WHERE version <= ?
              GROUP BY parcel_id) latest
          ON latest.parcel_id = pv.parcel_id AND latest.mv = pv.version
        WHERE pv.status = 'current'
        ORDER BY pv.parcel_id
        """,
        (version,),
    ).fetchall()
    return [_parcel_to_geojson(r) for r in rows]


def list_txns(conn: sqlite3.Connection) -> list[dict]:
    rows = conn.execute(
        "SELECT txn_id, kind, base_version, new_version, reason, created_at "
        "FROM txns ORDER BY txn_id"
    ).fetchall()
    return [dict(r) for r in rows]


def get_txn(conn: sqlite3.Connection, txn_id: int) -> dict:
    row = conn.execute("SELECT * FROM txns WHERE txn_id = ?", (txn_id,)).fetchone()
    if row is None:
        raise PlanValidationError(f"变更记录 {txn_id} 不存在")
    items = conn.execute(
        "SELECT role, parcel_id, area, geom_wkt FROM txn_items WHERE txn_id = ? ORDER BY role, parcel_id",
        (txn_id,),
    ).fetchall()
    return {
        **dict(row),
        "report": json.loads(row["report_json"]) if row["report_json"] else None,
        "items": [
            {
                "role": it["role"],
                "parcel_id": it["parcel_id"],
                "area": it["area"],
                "geometry": mapping(shapely_wkt.loads(it["geom_wkt"])),
            }
            for it in items
        ],
    }


def import_initial(conn: sqlite3.Connection, payload: Any) -> dict:
    """初始导入：要求库内尚无现势宗地，整批校验通过才写入。"""
    if current_version(conn) != 0 or _current_geoms(conn):
        raise VersionConflictError("数据库已存在现势宗地，不能重复初始导入")

    parsed = parse_feature_collection(payload)
    overlaps = ensure_no_overlap(
        [(pid, geom) for pid, geom, _ in parsed], label="初始宗地"
    )
    if overlaps:
        raise GeoValidationError(
            "初始宗地之间存在面积重叠："
            + "；".join(o["message"] for o in overlaps)
        )

    try:
        conn.execute("BEGIN IMMEDIATE")
        cur = conn.execute(
            "INSERT INTO txns (kind, base_version, new_version, reason, report_json, created_at) "
            "VALUES ('import', 0, 1, ?, NULL, ?)",
            ("初始导入", _now()),
        )
        txn_id = cur.lastrowid
        for pid, geom, _ in parsed:
            conn.execute(
                "INSERT INTO parcel_versions (parcel_id, version, status, geom_wkt, area, created_txn_id) "
                "VALUES (?, 1, 'current', ?, ?, ?)",
                (pid, geom.wkt, geom.area, txn_id),
            )
            conn.execute(
                "INSERT INTO txn_items (txn_id, role, parcel_id, geom_wkt, area) "
                "VALUES (?, 'new', ?, ?, ?)",
                (txn_id, pid, geom.wkt, geom.area),
            )
        conn.execute(
            "INSERT OR REPLACE INTO meta (key, value) VALUES ('current_version', '1')"
        )
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    return {"txn_id": txn_id, "new_version": 1, "imported": len(parsed)}


def _parse_change_request(req: dict) -> tuple[list[str], list[tuple[str, Any]]]:
    if not isinstance(req, dict):
        raise GeoValidationError("请求体必须是 JSON 对象")
    retired_ids = req.get("retired_ids")
    new_parcels = req.get("new_parcels")
    if not isinstance(retired_ids, list) or not retired_ids:
        raise GeoValidationError("retired_ids 必须是非空数组（退出的宗地号）")
    if not isinstance(new_parcels, list) or not new_parcels:
        raise GeoValidationError("new_parcels 必须是非空数组（接替的新宗地）")

    retired_ids = [str(x) for x in retired_ids]
    if len(set(retired_ids)) != len(retired_ids):
        raise GeoValidationError("retired_ids 存在重复宗地号")

    new_items: list[tuple[str, Any]] = []
    new_ids: set[str] = set()
    for i, item in enumerate(new_parcels):
        if not isinstance(item, dict) or "parcel_id" not in item or "geometry" not in item:
            raise GeoValidationError(
                f"new_parcels[{i}] 必须包含 parcel_id 与 geometry"
            )
        nid = str(item["parcel_id"])
        if not nid.strip():
            raise GeoValidationError(f"new_parcels[{i}] 宗地号为空")
        if nid in new_ids:
            raise GeoValidationError(f"新宗地号重复：{nid}")
        new_ids.add(nid)
        geom = parse_polygon(item["geometry"], label=f"新宗地 {nid}")
        new_items.append((nid, geom))
    return retired_ids, new_items


def plan(conn: sqlite3.Connection, req: dict) -> dict:
    """预核验：不落库，返回缺口/侵占/越界/重叠明细供网页预览。"""
    retired_ids, new_items = _parse_change_request(req)
    current = _current_geoms(conn)

    missing = [pid for pid in retired_ids if pid not in current]
    if missing:
        raise PlanValidationError(f"退出宗地号在现势库中不存在：{', '.join(missing)}")
    collision = [nid for nid, _ in new_items if nid in current]
    if collision:
        raise PlanValidationError(
            f"新宗地号与现有宗地号冲突：{', '.join(collision)}"
        )

    retired = [(pid, current[pid]) for pid in retired_ids]
    surviving = [(pid, g) for pid, g in current.items() if pid not in set(retired_ids)]
    report = plan_change(retired, new_items, surviving)
    return {
        "base_version": current_version(conn),
        "retired_ids": retired_ids,
        "new_ids": [nid for nid, _ in new_items],
        **report.to_dict(),
    }


def commit_change(conn: sqlite3.Connection, req: dict) -> dict:
    """确认提交：版本检查 + 事务内复核 + 原子写入。"""
    base_version = req.get("base_version")
    if not isinstance(base_version, int) or isinstance(base_version, bool):
        raise GeoValidationError("base_version 必须是整数")
    retired_ids, new_items = _parse_change_request(req)
    reason = str(req.get("reason") or "").strip() or None

    try:
        conn.execute("BEGIN IMMEDIATE")
        ver = current_version(conn)
        if ver != base_version:
            raise VersionConflictError(
                f"版本冲突：请求基于 v{base_version}，当前已是 v{ver}，"
                "该笔提交未生效，请刷新现势数据后重新核验"
            )

        current = _current_geoms(conn)
        missing = [pid for pid in retired_ids if pid not in current]
        if missing:
            raise PlanValidationError(
                f"退出宗地号在现势库中不存在：{', '.join(missing)}"
            )
        collision = [nid for nid, _ in new_items if nid in current]
        if collision:
            raise PlanValidationError(
                f"新宗地号与现有宗地号冲突：{', '.join(collision)}"
            )

        retired_set = set(retired_ids)
        retired = [(pid, current[pid]) for pid in retired_ids]
        surviving = [(pid, g) for pid, g in current.items() if pid not in retired_set]
        report = plan_change(retired, new_items, surviving)
        if not report.valid:
            raise GeoValidationError("核验未通过：" + "；".join(report.errors))

        new_version = ver + 1
        cur = conn.execute(
            "INSERT INTO txns (kind, base_version, new_version, reason, report_json, created_at) "
            "VALUES ('change', ?, ?, ?, ?, ?)",
            (ver, new_version, reason, json.dumps(report.to_dict()), _now()),
        )
        txn_id = cur.lastrowid

        # 退出宗地：追加一条 retired 版本行（旧行原样保留作为历史依据）
        for pid in retired_ids:
            old = conn.execute(
                """
                SELECT pv.* FROM parcel_versions pv
                JOIN (SELECT parcel_id, MAX(version) AS mv FROM parcel_versions
                      WHERE parcel_id = ? GROUP BY parcel_id) latest
                  ON latest.parcel_id = pv.parcel_id AND latest.mv = pv.version
                """,
                (pid,),
            ).fetchone()
            conn.execute(
                "INSERT INTO parcel_versions "
                "(parcel_id, version, status, geom_wkt, area, created_txn_id, retired_txn_id) "
                "VALUES (?, ?, 'retired', ?, ?, ?, ?)",
                (pid, new_version, old["geom_wkt"], old["area"],
                 old["created_txn_id"], txn_id),
            )
            conn.execute(
                "INSERT INTO txn_items (txn_id, role, parcel_id, geom_wkt, area) "
                "VALUES (?, 'retired', ?, ?, ?)",
                (txn_id, pid, old["geom_wkt"], old["area"]),
            )
        # 新宗地：追加 current 版本行
        for nid, geom in new_items:
            conn.execute(
                "INSERT INTO parcel_versions "
                "(parcel_id, version, status, geom_wkt, area, created_txn_id) "
                "VALUES (?, ?, 'current', ?, ?, ?)",
                (nid, new_version, geom.wkt, geom.area, txn_id),
            )
            conn.execute(
                "INSERT INTO txn_items (txn_id, role, parcel_id, geom_wkt, area) "
                "VALUES (?, 'new', ?, ?, ?)",
                (txn_id, nid, geom.wkt, geom.area),
            )
        conn.execute(
            "UPDATE meta SET value = ? WHERE key = 'current_version'", (str(new_version),)
        )
        conn.commit()
    except Exception:
        conn.rollback()
        raise

    return {
        "txn_id": txn_id,
        "base_version": base_version,
        "new_version": new_version,
        "retired_ids": retired_ids,
        "new_ids": [nid for nid, _ in new_items],
        "report": report.to_dict(),
    }


# ---------------------------------------------------------------------------
# 线路里程与巡检记录
# ---------------------------------------------------------------------------

def _alignment_row(conn: sqlite3.Connection, alignment_id: int) -> sqlite3.Row:
    row = conn.execute(
        "SELECT * FROM alignments WHERE alignment_id = ?", (alignment_id,)
    ).fetchone()
    if row is None:
        raise PlanValidationError(f"线路 {alignment_id} 不存在")
    return row


def load_segments(conn: sqlite3.Connection, alignment_id: int):
    row = _alignment_row(conn, alignment_id)
    return parse_segments(json.loads(row["segments_json"]))


def load_records(conn: sqlite3.Connection, alignment_id: int):
    rows = conn.execute(
        "SELECT inspection_id, station, segment_id FROM inspections "
        "WHERE alignment_id = ? ORDER BY sort_no, inspection_id",
        (alignment_id,),
    ).fetchall()
    specs = parse_records(
        [{"station": r["station"], "segment_id": r["segment_id"]} for r in rows]
    )
    for spec, row in zip(specs, rows):
        spec.inspection_id = row["inspection_id"]
    return specs


def create_alignment(conn: sqlite3.Connection, name: str, payload: Any) -> dict:
    """上传一批相接的直线段；校验段内桩号递增与相邻段共用端点后原子写入。"""
    segments = parse_segments(payload)
    errors = check_continuity(segments)
    if errors:
        raise AlignmentError("线路不连续：" + "；".join(errors))
    name = (name or "").strip() or "未命名线路"
    cur = conn.execute(
        "INSERT INTO alignments (name, segments_json, created_at) VALUES (?, ?, ?)",
        (name, json.dumps(segments_to_payload(segments), ensure_ascii=False), _now()),
    )
    conn.commit()
    row = conn.execute(
        "SELECT * FROM alignments WHERE alignment_id = ?", (cur.lastrowid,)
    ).fetchone()
    return _alignment_dict(row)


def _alignment_dict(row: sqlite3.Row) -> dict:
    return {
        "alignment_id": row["alignment_id"],
        "name": row["name"],
        "created_at": row["created_at"],
        "segments": json.loads(row["segments_json"]),
    }


def list_alignments(conn: sqlite3.Connection) -> list[dict]:
    rows = conn.execute("SELECT * FROM alignments ORDER BY alignment_id").fetchall()
    return [
        {
            "alignment_id": r["alignment_id"],
            "name": r["name"],
            "created_at": r["created_at"],
            "segment_count": len(json.loads(r["segments_json"])),
        }
        for r in rows
    ]


def get_alignment(conn: sqlite3.Connection, alignment_id: int) -> dict:
    data = _alignment_dict(_alignment_row(conn, alignment_id))
    data["records"] = [
        dict(r)
        for r in conn.execute(
            "SELECT inspection_id, sort_no, station, segment_id, note FROM inspections "
            "WHERE alignment_id = ? ORDER BY sort_no, inspection_id",
            (alignment_id,),
        ).fetchall()
    ]
    return data


def import_inspections(
    conn: sqlite3.Connection, alignment_id: int, payload: Any
) -> dict:
    """导入巡检记录（追加到该线路，按数组顺序排实地先后）。"""
    segments = load_segments(conn, alignment_id)
    order = {s.segment_id for s in segments}
    records = parse_records(payload)
    if not records:
        raise AlignmentError("记录列表为空")
    bad = [r for r in records if r.segment_id is not None and r.segment_id not in order]
    if bad:
        raise AlignmentError(
            "；".join(
                f"第 {r.index + 1} 笔记录填写的段号 {r.segment_id} 不在该线路中"
                for r in bad
            )
        )
    base = conn.execute(
        "SELECT COALESCE(MAX(sort_no), 0) FROM inspections WHERE alignment_id = ?",
        (alignment_id,),
    ).fetchone()[0]
    now = _now()
    try:
        conn.execute("BEGIN IMMEDIATE")
        ids = []
        for rec in records:
            cur = conn.execute(
                "INSERT INTO inspections "
                "(alignment_id, sort_no, station, segment_id, note, created_at) "
                "VALUES (?, ?, ?, ?, ?, ?)",
                (
                    alignment_id,
                    base + rec.index + 1,
                    rec.station,
                    rec.segment_id,
                    None,
                    now,
                ),
            )
            ids.append(cur.lastrowid)
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    return {"alignment_id": alignment_id, "imported": len(ids), "inspection_ids": ids}


def correct_inspection(
    conn: sqlite3.Connection,
    inspection_id: int,
    segment_id: Any,
    version: int | None = None,
) -> dict:
    """补填 / 更正一笔记录的所在段；segment_id 为 null 表示清空。

    整批因此变为无解时不生效：回滚并抛出 CorrectionUnsolvable，
    调用方据此保留补正前的记录与落图。
    """
    row = conn.execute(
        "SELECT * FROM inspections WHERE inspection_id = ?", (inspection_id,)
    ).fetchone()
    if row is None:
        raise PlanValidationError(f"巡检记录 {inspection_id} 不存在")
    if segment_id is not None:
        segment_id = str(segment_id).strip() or None
    if segment_id is not None:
        segs = load_segments(conn, row["alignment_id"])
        if segment_id not in {s.segment_id for s in segs}:
            raise PlanValidationError(
                f"段号 {segment_id} 不在线路 {row['alignment_id']} 中"
            )
    old = row["segment_id"]
    alignment_id = row["alignment_id"]
    unsolvable_reason: str | None = None
    try:
        conn.execute("BEGIN IMMEDIATE")
        conn.execute(
            "UPDATE inspections SET segment_id = ? WHERE inspection_id = ?",
            (segment_id, inspection_id),
        )
        result = evaluate_alignment(conn, alignment_id, version)
        if not result["ok"]:
            unsolvable_reason = result["reason"] or "整批无解"
            raise _Unsolvable
        conn.execute(
            "INSERT INTO inspection_corrections "
            "(inspection_id, old_segment_id, new_segment_id, created_at) "
            "VALUES (?, ?, ?, ?)",
            (inspection_id, old, segment_id, _now()),
        )
        conn.commit()
    except _Unsolvable:
        conn.rollback()
        # 回滚后按补正前的库状态重算，随错误一并返回，供网页保留记录与落图
        raise CorrectionUnsolvable(
            unsolvable_reason or "整批无解",
            evaluate_alignment(conn, alignment_id, version),
        ) from None
    except Exception:
        conn.rollback()
        raise
    result["correction"] = {
        "inspection_id": inspection_id,
        "old_segment_id": old,
        "new_segment_id": segment_id,
    }
    return result


class _Unsolvable(Exception):
    """内部信号：补正后整批无解，需要回滚并保留补正前结果。"""


class CorrectionUnsolvable(PlanValidationError):
    """补正后整批无解：补正不生效，附带补正前的完整结果供网页保留展示。"""

    def __init__(self, message: str, preserved: dict):
        super().__init__(message)
        self.preserved = preserved


def evaluate_alignment(
    conn: sqlite3.Connection, alignment_id: int, version: int | None = None
) -> dict:
    """计算整批候选；位置唯一的记录落图并列出覆盖宗地。"""
    segments = load_segments(conn, alignment_id)
    records = load_records(conn, alignment_id)
    result = evaluate(segments, records)

    current = current_version(conn)
    if version is None:
        version = current
    if version < 0:
        raise PlanValidationError("版本号必须 >= 0")
    if version > current:
        raise PlanValidationError(
            f"所选地籍版本 v{version} 超过当前版本 v{current}，无法落图"
        )
    parcels = _parcels_at(conn, version) if version > 0 else {}
    for rec in result.records:
        if rec.unique and rec.point is not None:
            rec.parcels = covering_parcels(rec.point, parcels)

    data = result.to_dict()
    data.update(
        {
            "alignment_id": alignment_id,
            "map_version": version,
            "current_version": current,
            # 落图与覆盖宗地来自同一版本快照，保证二者一致
            "map_parcels": [
                {"parcel_id": pid, "geometry": mapping(geom)}
                for pid, geom in sorted(parcels.items())
            ],
        }
    )
    return data


def _parcels_at(conn: sqlite3.Connection, version: int) -> dict[str, Any]:
    rows = conn.execute(
        """
        SELECT pv.parcel_id, pv.geom_wkt FROM parcel_versions pv
        JOIN (SELECT parcel_id, MAX(version) AS mv
              FROM parcel_versions WHERE version <= ?
              GROUP BY parcel_id) latest
          ON latest.parcel_id = pv.parcel_id AND latest.mv = pv.version
        WHERE pv.status = 'current'
        """,
        (version,),
    ).fetchall()
    return {r["parcel_id"]: shapely_wkt.loads(r["geom_wkt"]) for r in rows}
