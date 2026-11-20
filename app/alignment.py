"""线路里程（桩号）与巡检记录定位，纯计算、不依赖数据库。

线路 = 按行进顺序相接的直线段：
- 每段有唯一段号、地籍坐标系下的起止坐标、递增的起止桩号；
- 上一段终点与下一段起点必须是同一坐标（共用的端点算一处）；
- 段内按桩号线性换算坐标；桩号沿段前进方向递增；
- 允许「里程回跳」：后一段的起始桩号可以小于前一段的终止桩号，
  因此同一个桩号可能同时落在多段、指向多个位置。

巡检记录 = 按实地先后排列，每笔有一个桩号，可另行注明所在段。
把每笔记录在（全部段或所填段）中按段内线性换算得到候选位置后，
要求整批记录能排成一条「完整、不倒退」的走法：

- 位置用线路上的行进坐标 route_key = 段序 + 段内比例 表示；
- 后续记录的 route_key 不得减小（不倒退）：段序不回头；
  由于 route_key 以段序为主，里程回跳造成桩号数值变小并不算倒退；
  同一桩号落在同一处可以连记多笔，真正后退才排除；
- 某笔记录的候选段，只有能出现在至少一条贯穿首尾的走法中时才算数；
- 共用端点（前段 t=1 与后段 t=0）坐标与 route_key 都相同，合并为一处。

若整批不存在这样的走法，则整批无解，给出首个断点及原因，不给出候选。
若某笔记录填写了所在段，则该笔只能在该段内定位；填写的段不存在或不含
该桩号时整批无解。
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

# 几何接缝容差（地籍平面坐标，单位与坐标一致）
JOIN_EPS = 1e-6
# 桩号 / 行进坐标比较容差
STATION_EPS = 1e-9


class AlignmentError(ValueError):
    """线路或巡检记录输入不合法。"""


def _is_number(v: Any) -> bool:
    return isinstance(v, (int, float)) and not isinstance(v, bool)


def _coord(v: Any, label: str) -> tuple[float, float]:
    if (
        not isinstance(v, (list, tuple))
        or len(v) != 2
        or not all(_is_number(c) for c in v)
    ):
        raise AlignmentError(f"{label} 必须是 [x, y] 数值坐标")
    return (float(v[0]), float(v[1]))


@dataclass
class Segment:
    segment_id: str
    start: tuple[float, float]
    end: tuple[float, float]
    start_station: float
    end_station: float

    @property
    def length(self) -> float:
        dx = self.end[0] - self.start[0]
        dy = self.end[1] - self.start[1]
        return (dx * dx + dy * dy) ** 0.5

    @property
    def station_span(self) -> float:
        return self.end_station - self.start_station

    def point_at(self, station: float) -> tuple[float, float]:
        """段内线性换算：桩号 → 坐标。"""
        t = (station - self.start_station) / self.station_span
        x = self.start[0] + (self.end[0] - self.start[0]) * t
        y = self.start[1] + (self.end[1] - self.start[1]) * t
        return (x, y)

    def contains_station(self, station: float) -> bool:
        return self.start_station - STATION_EPS <= station <= self.end_station + STATION_EPS

    def to_dict(self) -> dict:
        return {
            "segment_id": self.segment_id,
            "start": list(self.start),
            "end": list(self.end),
            "start_station": self.start_station,
            "end_station": self.end_station,
            "length": self.length,
        }


def parse_segments(payload: Any) -> list[Segment]:
    """解析一批线路段（按行进顺序）。payload 为数组或 {"segments": [...]}。"""
    if isinstance(payload, dict):
        payload = payload.get("segments")
    if not isinstance(payload, list) or not payload:
        raise AlignmentError("段列表必须是非空数组")
    out: list[Segment] = []
    seen: set[str] = set()
    for i, item in enumerate(payload):
        if not isinstance(item, dict):
            raise AlignmentError(f"第 {i + 1} 段必须是 JSON 对象")
        sid = item.get("segment_id")
        if sid is None or str(sid).strip() == "":
            raise AlignmentError(f"第 {i + 1} 段缺少 segment_id（段号）")
        sid = str(sid).strip()
        if sid in seen:
            raise AlignmentError(f"段号重复：{sid}")
        seen.add(sid)
        start = _coord(item.get("start"), f"段 {sid} 起点")
        end = _coord(item.get("end"), f"段 {sid} 终点")
        ss, es = item.get("start_station"), item.get("end_station")
        if not (_is_number(ss) and _is_number(es)):
            raise AlignmentError(f"段 {sid} 起止桩号必须是数值")
        ss, es = float(ss), float(es)
        if es <= ss:
            raise AlignmentError(f"段 {sid} 桩号必须递增（起 {ss:g} → 止 {es:g}）")
        if abs(start[0] - end[0]) <= JOIN_EPS and abs(start[1] - end[1]) <= JOIN_EPS:
            raise AlignmentError(f"段 {sid} 起止坐标相同，长度为 0")
        out.append(Segment(sid, start, end, ss, es))
    return out


def check_continuity(segments: list[Segment]) -> list[str]:
    """按行进顺序校验相邻段共用端点；返回错误列表（空即通过）。"""
    errors: list[str] = []
    for a, b in zip(segments, segments[1:]):
        if (
            abs(a.end[0] - b.start[0]) > JOIN_EPS
            or abs(a.end[1] - b.start[1]) > JOIN_EPS
        ):
            errors.append(
                f"段 {a.segment_id} 终点 ({a.end[0]:g}, {a.end[1]:g}) 与段 "
                f"{b.segment_id} 起点 ({b.start[0]:g}, {b.start[1]:g}) 不重合，"
                "线路不连续"
            )
    return errors


def segments_to_payload(segments: list[Segment]) -> list[dict]:
    return [
        {
            "segment_id": s.segment_id,
            "start": list(s.start),
            "end": list(s.end),
            "start_station": s.start_station,
            "end_station": s.end_station,
        }
        for s in segments
    ]


@dataclass
class RecordSpec:
    index: int                       # 0-based，按实地先后排列
    station: float
    segment_id: str | None = None    # 用户补填 / 更正的所在段
    inspection_id: int | None = None  # 落库后的记录号（纯计算时可空）


def parse_records(payload: Any) -> list[RecordSpec]:
    if isinstance(payload, dict):
        payload = payload.get("records")
    if not isinstance(payload, list):
        raise AlignmentError("记录列表必须是数组")
    out: list[RecordSpec] = []
    for i, item in enumerate(payload):
        if not isinstance(item, dict):
            raise AlignmentError(f"第 {i + 1} 笔记录必须是 JSON 对象")
        st = item.get("station")
        if not _is_number(st):
            raise AlignmentError(f"第 {i + 1} 笔记录缺少数值桩号 station")
        sid = item.get("segment_id")
        if sid is not None and str(sid).strip() == "":
            sid = None
        out.append(
            RecordSpec(
                index=i,
                station=float(st),
                segment_id=str(sid).strip() if sid is not None else None,
            )
        )
    return out


@dataclass
class Candidate:
    """一个候选位置；共用端点合并后可能对应多个段号。"""

    segment_ids: list[str]
    point: tuple[float, float]
    route_key: float

    @property
    def label(self) -> str:
        return "/".join(self.segment_ids)


@dataclass
class RecordResult:
    index: int
    station: float
    segment_id: str | None
    inspection_id: int | None = None
    candidates: list[Candidate] = field(default_factory=list)
    raw_candidates: list[Candidate] = field(default_factory=list)
    unique: bool = False
    located_segment: str | None = None
    point: tuple[float, float] | None = None
    parcels: list[str] = field(default_factory=list)

    @property
    def needs_confirmation(self) -> bool:
        return len(self.candidates) > 1


@dataclass
class AlignmentResult:
    ok: bool
    reason: str | None
    segments: list[Segment]
    records: list[RecordResult]
    errors: list[str] = field(default_factory=list)

    def to_dict(self) -> dict:
        return {
            "ok": self.ok,
            "reason": self.reason,
            "errors": self.errors,
            "needs_confirmation": (
                sum(1 for r in self.records if r.needs_confirmation) if self.ok else 0
            ),
            "segments": [s.to_dict() for s in self.segments],
            "records": [
                {
                    "index": r.index,
                    "inspection_id": r.inspection_id,
                    "station": r.station,
                    "segment_id": r.segment_id,
                    "unique": r.unique,
                    "needs_confirmation": r.needs_confirmation if self.ok else False,
                    "located_segment": r.located_segment,
                    "point": list(r.point) if r.point is not None else None,
                    "parcels": r.parcels,
                    "candidates": [_cand_dict(c) for c in r.candidates],
                    "raw_candidates": (
                        [_cand_dict(c) for c in r.raw_candidates] if not self.ok else []
                    ),
                }
                for r in self.records
            ],
        }


def _cand_dict(c: Candidate) -> dict:
    return {
        "segment_ids": list(c.segment_ids),
        "label": c.label,
        "point": list(c.point),
        "route_key": c.route_key,
    }


def _raw_candidates(
    rec: RecordSpec, segments: list[Segment], order: dict[str, int]
) -> tuple[list[Candidate], str | None]:
    """单笔记录按段内线性换算得到的全部几何候选（尚未做走法过滤）。"""
    if rec.segment_id is not None:
        if rec.segment_id not in order:
            return [], f"第 {rec.index + 1} 笔记录填写的段号 {rec.segment_id} 不存在"
        pool = [segments[order[rec.segment_id]]]
    else:
        pool = segments
    cands: list[Candidate] = []
    for s in pool:
        if not s.contains_station(rec.station):
            continue
        t = (rec.station - s.start_station) / s.station_span
        cands.append(
            Candidate(
                segment_ids=[s.segment_id],
                point=s.point_at(rec.station),
                route_key=order[s.segment_id] + t,
            )
        )
    if not cands and rec.segment_id is not None:
        return [], (
            f"第 {rec.index + 1} 笔记录桩号 {rec.station:g} 不在所填段 "
            f"{rec.segment_id} 的起止桩号范围内"
        )
    return cands, None


def _merge_places(cands: list[Candidate]) -> list[Candidate]:
    """共用端点合并为一处：坐标相同且行进坐标相同（前段末端 ≡ 后段起首）。"""
    merged: list[Candidate] = []
    for c in sorted(cands, key=lambda x: x.route_key):
        for m in merged:
            if (
                abs(m.route_key - c.route_key) <= STATION_EPS
                and abs(m.point[0] - c.point[0]) <= JOIN_EPS
                and abs(m.point[1] - c.point[1]) <= JOIN_EPS
            ):
                for sid in c.segment_ids:
                    if sid not in m.segment_ids:
                        m.segment_ids.append(sid)
                break
        else:
            merged.append(Candidate(list(c.segment_ids), c.point, c.route_key))
    return merged


def _fmt_places(places: list[Candidate]) -> str:
    if not places:
        return "无"
    return "、".join(
        f"{p.label}({p.point[0]:g},{p.point[1]:g})" for p in places
    )


def _advances(prev: Candidate, nxt: Candidate) -> bool:
    """后一笔是否没有倒退。

    route_key = 段序 + 段内比例，以段序为主：因此里程回跳造成的桩号数值变小
    不会被误判为倒退（后段起飞回跳仍算前进），而回到更早的段则一定倒退。
    同一桩号落在同一处（route_key 相等）不算倒退——现场可能在同一桩号
    记两笔；真正后退（route_key 减小）才排除。
    """
    return nxt.route_key >= prev.route_key - STATION_EPS


def evaluate(segments: list[Segment], records: list[RecordSpec]) -> AlignmentResult:
    """对整批记录求完整、不倒退走法下的候选与唯一落点。"""
    order = {s.segment_id: i for i, s in enumerate(segments)}
    if not records:
        return AlignmentResult(False, "没有巡检记录", segments, [],
                               ["记录为空，无需定位"])

    results: list[RecordResult] = []
    errors: list[str] = []
    for rec in records:
        cands, err = _raw_candidates(rec, segments, order)
        if err:
            errors.append(err)
        places = _merge_places(cands)
        results.append(
            RecordResult(
                index=rec.index,
                station=rec.station,
                segment_id=rec.segment_id,
                inspection_id=rec.inspection_id,
                raw_candidates=places,
            )
        )

    if any(not r.raw_candidates for r in results):
        reason = "；".join(errors) if errors else (
            "存在桩号不在任何线路段内的记录，整批无法定位"
        )
        for r in results:
            r.candidates = []
            r.unique = False
            r.point = None
        return AlignmentResult(
            False, "整批无解：" + reason, segments, results,
            errors + ["整批无解：有记录没有任何几何候选"],
        )

    n = len(results)

    # 前缀可达：reach[i][j] = 从首笔出发、走到第 i 笔第 j 个候选的走法存在
    reach: list[list[bool]] = []
    for i, r in enumerate(results):
        row: list[bool] = []
        for j, c in enumerate(r.raw_candidates):
            if i == 0:
                row.append(True)
                continue
            row.append(
                any(
                    reach[i - 1][k]
                    and _advances(prev, c)
                    for k, prev in enumerate(results[i - 1].raw_candidates)
                )
            )
        reach.append(row)

    if not any(reach[-1]):
        # 找到首个断点：该笔之前还有走法，但这一笔接不上
        broken = next(i for i in range(n) if not any(reach[i]))
        cur = results[broken]
        detail = f"该笔现有候选：{_fmt_places(cur.raw_candidates)}"
        if broken > 0:
            prev = results[broken - 1]
            reachable_prev = [
                p
                for k, p in enumerate(prev.raw_candidates)
                if reach[broken - 1][k]
            ]
            detail += (
                f"；前一笔（桩号 {prev.station:g}）可达位置："
                f"{_fmt_places(reachable_prev)}"
            )
        reason = (
            f"第 {cur.index + 1} 笔记录（桩号 {cur.station:g}）无法接入完整、"
            "不倒退的走法：它的候选位置都早于前一笔的可达位置"
            f"（段序不回头、行进坐标不得减小）。{detail}"
        )
        if errors:
            reason = "；".join(errors) + "。" + reason
        for r in results:
            r.candidates = []
            r.unique = False
            r.point = None
        return AlignmentResult(
            False, "整批无解：" + reason, segments, results,
            errors + [reason],
        )

    # 后缀可行：suffix[i][j] = 从第 i 笔第 j 个候选出发能走完余下记录
    suffix: list[list[bool]] = [[] for _ in range(n)]
    for i in range(n - 1, -1, -1):
        r = results[i]
        for j, c in enumerate(r.raw_candidates):
            if i == n - 1:
                suffix[i].append(True)
                continue
            suffix[i].append(
                any(
                    suffix[i + 1][k]
                    and _advances(c, nxt)
                    for k, nxt in enumerate(results[i + 1].raw_candidates)
                )
            )

    # 只保留能出现在某条完整走法中的候选
    for i, r in enumerate(results):
        r.candidates = [
            c
            for j, c in enumerate(r.raw_candidates)
            if reach[i][j] and suffix[i][j]
        ]
        r.unique = len(r.candidates) == 1
        if r.unique:
            r.located_segment = r.candidates[0].label
            r.point = r.candidates[0].point
        else:
            r.located_segment = None
            r.point = None

    return AlignmentResult(True, None, segments, results, errors)


def covering_parcels(point: tuple[float, float], parcels: dict[str, Any]) -> list[str]:
    """返回覆盖该点的宗地号；落在共边上时相邻宗地都算覆盖。"""
    from shapely.geometry import Point

    p = Point(point)
    return sorted(pid for pid, geom in parcels.items() if geom.covers(p))
