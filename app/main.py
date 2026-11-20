"""地籍 Web 应用：宗地变更（导入/预核验/提交/历史）与线路里程巡检定位。"""
from __future__ import annotations

import os
from pathlib import Path

from contextlib import asynccontextmanager

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse
from fastapi.staticfiles import StaticFiles

from . import db
from .alignment import AlignmentError
from .geometry import GeoValidationError, PlanValidationError

DEFAULT_DB_PATH = "/data/cadastre.db"
STATIC_DIR = Path(__file__).resolve().parent.parent / "static"


def db_path() -> str:
    """每次连接时读取环境变量，便于测试与运行期切换数据文件。"""
    return os.environ.get("CADASTRE_DB", DEFAULT_DB_PATH)


def get_conn() -> db.sqlite3.Connection:
    path = db_path()
    Path(path).parent.mkdir(parents=True, exist_ok=True)
    conn = db.connect(path)
    db.init_db(conn)
    return conn


@asynccontextmanager
async def lifespan(_: FastAPI):
    conn = get_conn()
    conn.close()
    yield


app = FastAPI(title="地籍变更系统", version="1.0", lifespan=lifespan)


@app.exception_handler(GeoValidationError)
def _geo_error_handler(_: Request, exc: GeoValidationError) -> JSONResponse:
    return JSONResponse(status_code=422, content={"error": "数据校验失败", "detail": str(exc)})


@app.exception_handler(PlanValidationError)
def _plan_error_handler(_: Request, exc: PlanValidationError) -> JSONResponse:
    return JSONResponse(status_code=422, content={"error": "变更前提不成立", "detail": str(exc)})


@app.exception_handler(db.VersionConflictError)
def _conflict_handler(_: Request, exc: db.VersionConflictError) -> JSONResponse:
    return JSONResponse(status_code=409, content={"error": "版本冲突", "detail": str(exc)})


@app.exception_handler(AlignmentError)
def _alignment_error_handler(_: Request, exc: AlignmentError) -> JSONResponse:
    return JSONResponse(
        status_code=422, content={"error": "线路/记录数据不合法", "detail": str(exc)}
    )


@app.exception_handler(db.CorrectionUnsolvable)
def _correction_unsolvable_handler(
    _: Request, exc: db.CorrectionUnsolvable
) -> JSONResponse:
    """补正使整批无解：补正不写入，返回补正前的记录与落图供网页保留。"""
    return JSONResponse(
        status_code=409,
        content={
            "error": "补正后整批无解，补正未生效",
            "detail": str(exc),
            "preserved": exc.preserved,
        },
    )


@app.get("/api/state")
def state() -> dict:
    conn = get_conn()
    try:
        parcels = db.list_current(conn)
        return {
            "current_version": db.current_version(conn),
            "parcel_count": len(parcels),
            "parcels": parcels,
        }
    finally:
        conn.close()


@app.post("/api/import")
async def import_geojson(request: Request) -> dict:
    payload = await request.json()
    conn = get_conn()
    try:
        return db.import_initial(conn, payload)
    finally:
        conn.close()


@app.post("/api/changes/plan")
async def plan_change(request: Request) -> dict:
    req = await request.json()
    conn = get_conn()
    try:
        return db.plan(conn, req)
    finally:
        conn.close()


@app.post("/api/changes/commit")
async def commit_change(request: Request) -> dict:
    req = await request.json()
    conn = get_conn()
    try:
        return db.commit_change(conn, req)
    finally:
        conn.close()


@app.get("/api/history/parcel/{parcel_id}")
def history_parcel(parcel_id: str) -> dict:
    conn = get_conn()
    try:
        return db.parcel_history(conn, parcel_id)
    finally:
        conn.close()


@app.get("/api/history/version/{version}")
def history_version(version: int) -> dict:
    if version < 1:
        raise PlanValidationError("版本号必须 >= 1")
    conn = get_conn()
    try:
        return {
            "version": version,
            "parcels": db.snapshot_at(conn, version),
        }
    finally:
        conn.close()


@app.get("/api/txns")
def txns() -> dict:
    conn = get_conn()
    try:
        return {"txns": db.list_txns(conn)}
    finally:
        conn.close()


@app.get("/api/txns/{txn_id}")
def txn_detail(txn_id: int) -> dict:
    conn = get_conn()
    try:
        return db.get_txn(conn, txn_id)
    finally:
        conn.close()


# ---------------------------------------------------------------------------
# 线路里程 / 巡检记录
# ---------------------------------------------------------------------------

@app.get("/api/alignments")
def alignments() -> dict:
    conn = get_conn()
    try:
        return {"alignments": db.list_alignments(conn)}
    finally:
        conn.close()


@app.post("/api/alignments")
async def create_alignment(request: Request) -> dict:
    """上传线路：按行进顺序相接的直线段（段号、起止坐标、递增起止桩号）。"""
    body = await request.json()
    payload = body.get("segments") if isinstance(body, dict) else body
    name = body.get("name") if isinstance(body, dict) else None
    conn = get_conn()
    try:
        return db.create_alignment(conn, name or "", payload)
    finally:
        conn.close()


@app.get("/api/alignments/{alignment_id}")
def get_alignment(alignment_id: int) -> dict:
    conn = get_conn()
    try:
        return db.get_alignment(conn, alignment_id)
    finally:
        conn.close()


@app.post("/api/alignments/{alignment_id}/inspections")
async def import_inspections(alignment_id: int, request: Request) -> dict:
    """按实地先后导入巡检记录（每笔含桩号，可选所在段）。"""
    body = await request.json()
    payload = body.get("records") if isinstance(body, dict) else body
    conn = get_conn()
    try:
        return db.import_inspections(conn, alignment_id, payload)
    finally:
        conn.close()


@app.post("/api/alignments/{alignment_id}/evaluate")
def evaluate_alignment(alignment_id: int, version: int | None = None) -> dict:
    """逐笔给出候选段号与坐标；位置唯一者按所选地籍版本落图并列出覆盖宗地。"""
    conn = get_conn()
    try:
        return db.evaluate_alignment(conn, alignment_id, version)
    finally:
        conn.close()


@app.patch("/api/inspections/{inspection_id}")
async def correct_inspection(
    inspection_id: int, request: Request, version: int | None = None
) -> dict:
    """补填 / 更正一笔记录的所在段；返回该线路更新后的整批结果。

    version 与定位接口一致，用于指定落图所用的地籍版本（缺省为当前版本）。
    补正后整批无解时不写入，返回 409 并附补正前的结果供网页保留。
    """
    body = await request.json()
    if not isinstance(body, dict):
        raise AlignmentError("请求体必须是 JSON 对象")
    conn = get_conn()
    try:
        return db.correct_inspection(
            conn, inspection_id, body.get("segment_id"), version
        )
    finally:
        conn.close()


@app.get("/api/inspections/{inspection_id}/corrections")
def inspection_corrections(inspection_id: int) -> dict:
    conn = get_conn()
    try:
        row = conn.execute(
            "SELECT 1 FROM inspections WHERE inspection_id = ?", (inspection_id,)
        ).fetchone()
        if row is None:
            raise PlanValidationError(f"巡检记录 {inspection_id} 不存在")
        rows = conn.execute(
            "SELECT correction_id, old_segment_id, new_segment_id, created_at "
            "FROM inspection_corrections WHERE inspection_id = ? "
            "ORDER BY correction_id",
            (inspection_id,),
        ).fetchall()
        return {"inspection_id": inspection_id, "corrections": [dict(r) for r in rows]}
    finally:
        conn.close()


app.mount("/", StaticFiles(directory=str(STATIC_DIR), html=True), name="static")
