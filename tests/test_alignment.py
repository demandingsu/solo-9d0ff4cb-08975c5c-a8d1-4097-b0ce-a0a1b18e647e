"""线路里程回跳下的接线（桩号）定位：候选、唯一落图、补正与整批无解。"""
import os
import tempfile

import pytest

from app.alignment import (
    AlignmentError,
    Segment,
    check_continuity,
    evaluate,
    parse_records,
    parse_segments,
)


def seg(sid, x0, x1, s0, s1, y=0):
    return {"segment_id": sid, "start": [x0, y], "end": [x1, y],
            "start_station": s0, "end_station": s1}


# S1 0→100（线 x=0..100），S2 从同一端点继续物理前进，
# 但桩号回跳到 90→190；S3 再续到 240→340。
LINE = [
    seg("S1", 0, 100, 0, 100),
    seg("S2", 100, 200, 90, 190),
    seg("S3", 200, 300, 240, 340),
]


def ev(segments, stations, corrections=None):
    corrections = corrections or {}
    recs = parse_records(
        [{"station": s, "segment_id": corrections.get(i)} for i, s in enumerate(stations)]
    )
    return evaluate(parse_segments(segments), recs)


def labels(rec):
    return [c.label for c in rec.candidates]


# ---------- 线路校验 ----------

def test_continuity_requires_shared_endpoint():
    segs = parse_segments(LINE)
    assert check_continuity(segs) == []
    broken = parse_segments([seg("A", 0, 10, 0, 10), seg("B", 11, 20, 10, 20)])
    errs = check_continuity(broken)
    assert errs and "不连续" in errs[0]


def test_segment_station_must_increase_and_length_nonzero():
    with pytest.raises(AlignmentError, match="递增"):
        parse_segments([seg("A", 0, 10, 10, 5)])
    with pytest.raises(AlignmentError, match="长度为 0"):
        parse_segments([{"segment_id": "A", "start": [1, 1], "end": [1, 1],
                         "start_station": 0, "end_station": 5}])
    with pytest.raises(AlignmentError, match="段号重复"):
        parse_segments([seg("A", 0, 10, 0, 10), seg("A", 10, 20, 10, 20)])


# ---------- 回跳：同一桩号指向两处 ----------

def test_rewind_gives_two_candidates_and_points():
    r = ev(LINE, [95])
    assert r.ok
    rec = r.records[0]
    assert labels(rec) == ["S1", "S2"]
    points = sorted(c.point for c in rec.candidates)
    assert points == [(95.0, 0.0), (105.0, 0.0)]
    assert rec.unique is False and rec.point is None


def test_candidate_pruned_when_only_one_fits_the_route():
    # 50 只能在 S1；随后 95 若回跳去 S2 也可以，但再往后 160 只在 S2，
    # 于是 95 的两处都可行 → 仍需确认
    r = ev(LINE, [50, 95, 160])
    assert r.ok
    assert labels(r.records[0]) == ["S1"]
    assert labels(r.records[1]) == ["S1", "S2"]
    assert labels(r.records[2]) == ["S2"]

    # 若前一笔记 120（只可能在 S2），那么 95 若在 S1 会倒退，只能落 S2
    r2 = ev(LINE, [50, 120, 95])
    assert r2.ok is False  # 95 晚于 120 但桩号更小、又只能回到 S2 内倒退

    # 前一笔已过 S1，后一笔 95 的 S1 候选因段序倒退被排除，只剩 S2
    r3 = ev(LINE, [120, 95, 160])
    assert r3.ok is False  # 95 之后 160 在 S2，而 95 的 S2 位置是 105 之后可达

    # 真正能走通的情形：120(S2) → 95 只能回 S1 才算前进，但段序不允许
    r4 = ev(LINE, [120, 95])
    assert r4.ok is False
    assert "整批无解" in r4.reason


def test_shared_endpoint_counts_as_one_place():
    # S1 末端与 S2 起点共用端点且桩号相同（衔接处不回跳），合并为一处置信点
    line = [
        seg("S1", 0, 100, 0, 100),
        seg("S2", 100, 200, 100, 200),
    ]
    r = ev(line, [100])
    assert r.ok
    rec = r.records[0]
    assert len(rec.candidates) == 1
    assert rec.candidates[0].segment_ids == ["S1", "S2"]
    assert rec.unique
    assert rec.point == (100.0, 0.0)

    # 但若回跳后 S2 起点桩号已不同于 S1 末端，则共用端点在行进坐标上分属两处，
    # 同一桩号 100 会对应两个不同坐标（一处是端点，一处在 S2 内部），不算一处置信
    r2 = ev(LINE, [100])
    assert r2.ok and len(r2.records[0].candidates) == 2


def test_station_can_hit_three_segments():
    # 250 落在 S3（240..340）与 S2 的 90..190 无关；再造一段真正三重叠
    line = [
        seg("A", 0, 100, 0, 100),
        seg("B", 100, 200, 0, 100),   # 与 A 桩号完全重合
        seg("C", 200, 300, 0, 100),
    ]
    r = ev(line, [40])
    assert r.ok and labels(r.records[0]) == ["A", "B", "C"]


# ---------- 补正：唯一性随前后记录联动 ----------

def test_correction_narrows_neighbours():
    # 95 有两个候选；若下一笔指定在 S2 的 180（S2 内），95→S1 依然可行，
    # 但若下一笔指定 S3 的 300（S3 内），95 的两个候选都仍能接上
    r = ev(LINE, [95, 180])
    assert r.ok and labels(r.records[0]) == ["S1", "S2"]

    # 指定 95 在 S1，则 95 唯一；下一笔 180 只在 S2（若在 S1 会超段）
    r2 = ev(LINE, [95, 180], {0: "S1"})
    assert r2.ok
    assert labels(r2.records[0]) == ["S1"] and r2.records[0].unique
    assert labels(r2.records[1]) == ["S2"]

    # 指定 95 在 S2，则 95 唯一，且坐标为 S2 内的 105
    r3 = ev(LINE, [95, 180], {0: "S2"})
    assert r3.ok and r3.records[0].point == (105.0, 0.0)

    # 下一笔改到 60（只在 S2 的回落区间内不存在；S2 起点 90）→ 无解
    r4 = ev(LINE, [95, 60], {1: "S2"})
    assert r4.ok is False and "不在所填段" in r4.reason


def test_correction_to_invalid_segment_has_no_candidates():
    r = ev(LINE, [50], {0: "S9"})
    assert r.ok is False and "不存在" in r.reason


def test_correction_with_station_outside_named_segment():
    r = ev(LINE, [150], {0: "S1"})
    assert r.ok is False and "起止桩号范围" in r.reason


# ---------- 整批无解 ----------

def test_whole_batch_no_solution_on_backward_walk():
    r = ev(LINE, [60, 150, 70])
    assert r.ok is False
    assert "整批无解" in r.reason
    assert all(rec.candidates == [] for rec in r.records)
    # 仍保留原始候选供资料员查看
    assert len(r.records[2].raw_candidates) >= 1


def test_no_solution_when_station_outside_all_segments():
    r = ev(LINE, [60, 400])
    assert r.ok is False and "整批无解" in r.reason


def test_same_place_recorded_twice_is_not_backward():
    # 同一桩号落在同一处可连记两笔（route_key 相等不算倒退）
    r = ev(LINE, [50, 50, 70])
    assert r.ok
    assert labels(r.records[1]) == ["S1"] and r.records[1].unique
    assert r.records[1].point == (50.0, 0.0)


def test_single_reverse_record_ok():
    # 只有一笔时不存在「倒退」，任意候选都成立
    r = ev(LINE, [95])
    assert r.ok
