"""巡检定位的 API 测试：上传线路、导入记录、回跳候选、补正联动、整批无解保留。"""
import os
import tempfile

import pytest

_db_fd, _db_path = tempfile.mkstemp(suffix=".db")
os.close(_db_fd)

from fastapi.testclient import TestClient  # noqa: E402

from app.main import app  # noqa: E402


@pytest.fixture(scope="module")
def client():
    # 应用每次连接时读 CADASTRE_DB，故在 fixture 内切换即可用独立库
    os.environ["CADASTRE_DB"] = _db_path
    if os.path.exists(_db_path):
        os.remove(_db_path)
    with TestClient(app) as c:
        yield c

# 线路沿 x 轴：S1 x0..100 桩号 0..100，S2 x100..200 桩号 90..190，
# S3 x200..300 桩号 240..340。S2 与 S1 有 10 个单位桩号回跳。
LINE = [
    {"segment_id": "S1", "start": [0, 0], "end": [100, 0],
     "start_station": 0, "end_station": 100},
    {"segment_id": "S2", "start": [100, 0], "end": [200, 0],
     "start_station": 90, "end_station": 190},
    {"segment_id": "S3", "start": [200, 0], "end": [300, 0],
     "start_station": 240, "end_station": 340},
]

# 宗地：P1 x0..120、P2 120..220、P3 220..300，均 y-20..20（线路穿过它们）
PARCELS = {
    "type": "FeatureCollection",
    "features": [
        {"type": "Feature", "properties": {"parcel_id": "P1"},
         "geometry": {"type": "Polygon", "coordinates": [
             [[0, -20], [120, -20], [120, 20], [0, 20], [0, -20]]]}},
        {"type": "Feature", "properties": {"parcel_id": "P2"},
         "geometry": {"type": "Polygon", "coordinates": [
             [[120, -20], [220, -20], [220, 20], [120, 20], [120, -20]]]}},
        {"type": "Feature", "properties": {"parcel_id": "P3"},
         "geometry": {"type": "Polygon", "coordinates": [
             [[220, -20], [300, -20], [300, 20], [220, 20], [220, -20]]]}},
    ],
}


def _create(client, segments=LINE, name="K线"):
    r = client.post("/api/alignments", json={"name": name, "segments": segments})
    assert r.status_code == 200, r.text
    return r.json()["alignment_id"]


def _import(client, aid, records):
    r = client.post(f"/api/alignments/{aid}/inspections", json={"records": records})
    assert r.status_code == 200, r.text
    return r.json()["inspection_ids"]


def _eval(client, aid, version=None):
    qs = "" if version is None else f"?version={version}"
    r = client.post(f"/api/alignments/{aid}/evaluate{qs}")
    assert r.status_code == 200, r.text
    return r.json()


def test_01_import_parcels(client):
    r = client.post("/api/import", json=PARCELS)
    assert r.status_code == 200, r.text
    assert r.json()["new_version"] == 1


def test_02_alignment_validation(client):
    # 相邻段端点不重合
    bad = [LINE[0], {**LINE[1], "start": [101, 0]}]
    r = client.post("/api/alignments", json={"name": "断线", "segments": bad})
    assert r.status_code == 422 and "不连续" in r.json()["detail"]

    # 段内桩号必须递增
    r = client.post("/api/alignments", json={"segments": [
        {"segment_id": "X", "start": [0, 0], "end": [10, 0],
         "start_station": 10, "end_station": 5}]})
    assert r.status_code == 422 and "递增" in r.json()["detail"]

    # 段号重复
    r = client.post("/api/alignments", json={"segments": [LINE[0], LINE[0]]})
    assert r.status_code == 422 and "段号重复" in r.json()["detail"]


def test_03_rewind_yields_two_candidates(client):
    aid = _create(client)
    _import(client, aid, [{"station": 95}])
    data = _eval(client, aid)
    assert data["ok"] is True
    rec = data["records"][0]
    assert [c["label"] for c in rec["candidates"]] == ["S1", "S2"]
    assert sorted(tuple(c["point"]) for c in rec["candidates"]) == [(95.0, 0.0), (105.0, 0.0)]
    assert rec["unique"] is False and rec["point"] is None
    assert rec["parcels"] == []


def test_04_unique_point_drawn_with_covering_parcels(client):
    aid = _create(client)
    _import(client, aid, [{"station": 50}, {"station": 110}, {"station": 250}])
    data = _eval(client, aid)
    assert data["ok"] is True
    by_station = {r["station"]: r for r in data["records"]}
    # 50 只在 S1；110 只在 S2（S1 上限 100）；250 在 S3（S2 上限 190）
    assert by_station[50]["unique"] and by_station[50]["point"] == [50.0, 0.0]
    assert by_station[50]["parcels"] == ["P1"]
    assert by_station[110]["unique"] and by_station[110]["point"] == [120.0, 0.0]
    # x=120 是 P1/P2 共边，两边都算覆盖
    assert by_station[110]["parcels"] == ["P1", "P2"]
    assert by_station[250]["unique"] and by_station[250]["point"] == [210.0, 0.0]
    assert by_station[250]["parcels"] == ["P2"]


def test_05_selected_map_version_uses_that_snapshot(client):
    # 先做一次分割：P2 → P2A/P2B（x=170）
    req = {
        "base_version": 1,
        "retired_ids": ["P2"],
        "new_parcels": [
            {"parcel_id": "P2A", "geometry": {"type": "Polygon", "coordinates": [
                [[120, -20], [170, -20], [170, 20], [120, 20], [120, -20]]]}},
            {"parcel_id": "P2B", "geometry": {"type": "Polygon", "coordinates": [
                [[170, -20], [220, -20], [220, 20], [170, 20], [170, -20]]]}},
        ],
    }
    r = client.post("/api/changes/commit", json=req)
    assert r.status_code == 200, r.text

    aid = _create(client)
    _import(client, aid, [{"station": 110}])
    old = _eval(client, aid, version=1)
    new = _eval(client, aid, version=2)
    assert old["map_version"] == 1 and new["map_version"] == 2
    assert old["records"][0]["parcels"] == ["P1", "P2"]
    assert new["records"][0]["parcels"] == ["P1", "P2A"]


def test_06_correction_updates_neighbours_and_map(client):
    aid = _create(client)
    ids = _import(client, aid, [{"station": 95}, {"station": 160}])
    data = _eval(client, aid)
    assert [c["label"] for c in data["records"][0]["candidates"]] == ["S1", "S2"]
    assert data["records"][0]["unique"] is False

    # 把第一笔补填到 S2：它唯一落在 x=105（P1），后续一笔仍在 S2 内
    r = client.patch(f"/api/inspections/{ids[0]}", json={"segment_id": "S2"})
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["correction"]["old_segment_id"] is None
    assert body["correction"]["new_segment_id"] == "S2"
    assert body["records"][0]["unique"] is True
    assert body["records"][0]["point"] == [105.0, 0.0]
    assert body["records"][0]["parcels"] == ["P1"]
    assert body["records"][1]["unique"] is True

    # 补正历史可查
    h = client.get(f"/api/inspections/{ids[0]}/corrections").json()
    assert [(c["old_segment_id"], c["new_segment_id"]) for c in h["corrections"]] == [(None, "S2")]

    # 再更正到 S1：落点变为 x=95，宗地仍为 P1
    r2 = client.patch(f"/api/inspections/{ids[0]}", json={"segment_id": "S1"})
    assert r2.status_code == 200
    assert r2.json()["records"][0]["point"] == [95.0, 0.0]
    # 清空所填段后重新回到两处候选
    r3 = client.patch(f"/api/inspections/{ids[0]}", json={"segment_id": None})
    assert r3.status_code == 200
    assert r3.json()["records"][0]["unique"] is False
    assert len(r3.json()["records"][0]["candidates"]) == 2


def test_07_unsolvable_batch_reports_reason_and_keeps_records(client):
    aid = _create(client)
    # 100 之后接 60：60 只能回 S2/S1 中段，但段序与桩号都无法前进
    ids = _import(client, aid, [{"station": 120}, {"station": 60}])
    data = _eval(client, aid)
    assert data["ok"] is False
    assert "整批无解" in data["reason"]
    assert all(r["candidates"] == [] for r in data["records"])
    # 原始候选仍返回，便于资料员查看
    assert data["records"][1]["raw_candidates"]
    # 记录仍在库中
    al = client.get(f"/api/alignments/{aid}").json()
    assert [x["inspection_id"] for x in al["records"]] == ids


def test_08_unsolvable_correction_is_not_written(client):
    aid = _create(client)
    ids = _import(client, aid, [{"station": 95}, {"station": 160}])
    before = _eval(client, aid)
    assert before["ok"] is True

    # 把第一笔错填到 S3（95 不在 S3 的 240..340），补正后整批无解
    r = client.patch(f"/api/inspections/{ids[0]}", json={"segment_id": "S3"})
    assert r.status_code == 409
    body = r.json()
    assert "无解" in body["error"]
    preserved = body["preserved"]
    assert preserved["ok"] is True
    assert preserved["records"][0]["unique"] is False  # 补正前的两处候选

    # 补正没有写入：记录仍空，补正历史为空
    al = client.get(f"/api/alignments/{aid}").json()
    assert al["records"][0]["segment_id"] is None
    h = client.get(f"/api/inspections/{ids[0]}/corrections").json()
    assert h["corrections"] == []
    assert _eval(client, aid)["records"][0]["unique"] is False


def test_08b_same_station_twice_is_accepted(client):
    # 同一桩号两笔：前一笔有两种可行走法（S1→S2、S2 原地），仍列两处；
    # 后一笔已指定 S2，其候选与唯一落点应照常给出
    aid = _create(client)
    _import(client, aid, [{"station": 95}, {"station": 95, "segment_id": "S2"}])
    data = _eval(client, aid)
    assert data["ok"] is True
    assert data["records"][0]["unique"] is False
    assert [c["label"] for c in data["records"][0]["candidates"]] == ["S1", "S2"]
    assert data["records"][1]["unique"] and data["records"][1]["point"] == [105.0, 0.0]


def test_08c_correction_breaks_route_keeps_previous_state(client):
    # 95 先落 C1 或 C2 都能走通；把它定为 C2 后，70 只剩 C2(110) 且早于 95(C2,137.5)
    aid = _create(client, segments=[
        {"segment_id": "C1", "start": [0, 0], "end": [100, 0],
         "start_station": 0, "end_station": 100},
        {"segment_id": "C2", "start": [100, 0], "end": [200, 0],
         "start_station": 60, "end_station": 160},
    ])
    ids = _import(client, aid, [{"station": 95}, {"station": 70}, {"station": 150}])
    before = _eval(client, aid)
    assert before["ok"] is True
    # 95 只能走 C1：若走 C2（137.5），70 无论落 C1(70) 还是 C2(110) 都会倒退
    assert before["records"][0]["unique"] is True
    assert before["records"][0]["point"] == [95.0, 0.0]

    r = client.patch(f"/api/inspections/{ids[0]}", json={"segment_id": "C2"})
    assert r.status_code == 409
    body = r.json()
    assert "第 2 笔记录" in body["detail"]
    assert body["preserved"]["ok"] is True
    assert body["preserved"]["records"][0]["point"] == [95.0, 0.0]
    # 补正未写入，记录保持原样
    al = client.get(f"/api/alignments/{aid}").json()
    assert al["records"][0]["segment_id"] is None
    assert _eval(client, aid)["ok"] is True


def test_09_bad_correction_segment_rejected(client):
    aid = _create(client)
    ids = _import(client, aid, [{"station": 50}])
    r = client.patch(f"/api/inspections/{ids[0]}", json={"segment_id": "NOPE"})
    assert r.status_code == 422 and "不在线路" in r.json()["detail"]
    r2 = client.patch("/api/inspections/99999", json={"segment_id": "S1"})
    assert r2.status_code == 422 and "不存在" in r2.json()["detail"]


def test_10_import_records_validates_segment_and_station(client):
    aid = _create(client)
    r = client.post(f"/api/alignments/{aid}/inspections",
                    json={"records": [{"station": 10, "segment_id": "ZZ"}]})
    assert r.status_code == 422 and "不在该线路" in r.json()["detail"]
    r2 = client.post(f"/api/alignments/{aid}/inspections",
                     json={"records": [{"station": "abc"}]})
    assert r2.status_code == 422 and "数值桩号" in r2.json()["detail"]


def test_11_shared_endpoint_merges_into_one_place(client):
    # S1 终点桩号 = S2 起点桩号（100），共用端点合并为一处置信落点
    aid = _create(client, segments=[
        {"segment_id": "S1", "start": [0, 0], "end": [100, 0],
         "start_station": 0, "end_station": 100},
        {"segment_id": "S2", "start": [100, 0], "end": [200, 0],
         "start_station": 100, "end_station": 200},
    ])
    _import(client, aid, [{"station": 100}])
    rec = _eval(client, aid)["records"][0]
    assert len(rec["candidates"]) == 1
    assert rec["candidates"][0]["segment_ids"] == ["S1", "S2"]
    assert rec["unique"] and rec["point"] == [100.0, 0.0]
