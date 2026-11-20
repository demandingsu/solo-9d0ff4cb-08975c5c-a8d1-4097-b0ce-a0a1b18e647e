# 地籍变更系统（分割 / 合并）

FastAPI + SQLite + Shapely 实现的网页应用：导入单一平面坐标系下、整数坐标、
无洞、互不重叠的 GeoJSON 宗地，支持宗地分割/合并变更的预核验、问题区域预览、
原子确认提交，以及现势与历史边界、变更流水查询。

## 启动（Docker Compose）

```bash
docker compose up --build
```

浏览器打开 <http://localhost:8000> 即可操作。SQLite 数据文件持久化在 `./data/cadastre.db`。

本地无 Docker 时：

```bash
pip install -r requirements.txt
CADASTRE_DB=./data/cadastre.db uvicorn app.main:app --host 0.0.0.0 --port 8000
```

## 业务规则

1. **导入**：FeatureCollection，每个 Feature 的 `properties.parcel_id` 为宗地号；
   几何必须是 Polygon、整数坐标、无内环（无洞）、面积大于 0；初始宗地之间不得
   面积重叠，共边允许。仅允许在空库导入一次。
2. **变更请求**：指定 `retired_ids`（退出的现势宗地号）与 `new_parcels`（接替新面）。
   - 新面之间不得面积重叠（共边允许）；
   - 新面不得侵占任何未退出的现势宗地；
   - 新面并集须**恰好**等于退出旧面并集：少了即「缺口」，多了即「越界」；
   - 新宗地号不得与现势宗地号冲突。
3. **两阶段操作**：先 `POST /api/changes/plan` 预核验，返回缺口/侵占/越界/互叠的
   GeoJSON 几何，网页在 SVG 地图上以红/橙/紫/粉色叠加预览；核验通过后才能
   `POST /api/changes/commit` 确认。
4. **乐观并发**：每个请求携带 `base_version`。提交在 `BEGIN IMMEDIATE` 事务内复核
   当前版本，版本不一致返回 409。基于同一版本的两笔提交只有一笔生效。
5. **失败原子性**：任何核验或写入失败都回滚整个事务，现势与历史保持原状。
6. **历史保留**：宗地版本行 append-only——退出时追加 `retired` 行（旧行不改），
   新设时追加 `current` 行；可按宗地号查全版本边界，也可按版本号查当时的现势快照。

## 线路里程与巡检记录（桩号定位）

线路里程可能回跳（改线、复测后桩号重新起算），同一个桩号因此可能指向多处。
网页「巡检」页签提供以下流程：

1. **上传线路**：按行进顺序给出相接的直线段，每段注明段号、地籍坐标系下的
   起止坐标与递增的起止桩号；相邻段必须共用端点（共用端点算一处）。
   桩号允许跨段回跳，段内桩号仍须递增。
2. **导入巡检记录**：按实地先后顺序，每笔给出桩号，可选填写所在段。
3. **定位**：按段内线性换算，把每笔记录在「全部段」或「所填段」中求出候选位置；
   再要求整批记录能排成一条**完整、不倒退**的走法——后续记录的行进坐标
   （段序 + 段内比例）不得减小，即段序不回头——回跳造成桩号数值变小不算倒退，
   同一桩号落在同一处可连记多笔，真正后退才排除。
   只有能出现在至少一条贯穿首尾走法中的候选才列出；某笔只剩一处时判定唯一，
   在所选地籍版本的地图上标点，并列出覆盖该点的宗地（落在共边上时相邻宗地都算）。
4. **补正联动**：网页上为某笔补填或更正所在段后，整批候选与落图立即重算；
   补正写入 `inspection_corrections` 追加历史。
5. **整批无解**：给出无解原因（首处断点、该笔候选、前一笔可达位置），
   不落图、不给出候选，保留补正前的记录与落图；补正导致无解时补正不写入（409，
   响应中带 `preserved` 供网页继续展示）。

段内换算：`t = (桩号 - 段首桩号) / (段末桩号 - 段首桩号)`，坐标按 `t` 线性插值。
共用端点的合并条件是「坐标相同且行进坐标相同」，即前段末端与后段起首
在走法上是同一处；回跳段中桩号相同的另一处仍算独立候选。

## API

| 方法 | 路径 | 说明 |
| --- | --- | --- |
| GET | `/api/state` | 当前版本与全部现势宗地 |
| POST | `/api/import` | 初始导入 GeoJSON |
| POST | `/api/changes/plan` | 预核验（不落库），返回问题区域几何 |
| POST | `/api/changes/commit` | 确认提交（原子写入，版本冲突返回 409） |
| GET | `/api/history/parcel/{id}` | 单宗地全历史版本边界 |
| GET | `/api/history/version/{v}` | 截至 v 版本的现势快照 |
| GET | `/api/txns` | 变更流水列表 |
| GET | `/api/txns/{id}` | 单笔流水详情（退出/新设明细 + 核验报告） |
| GET | `/api/alignments` | 线路列表 |
| POST | `/api/alignments` | 上传线路（`name` + 相接直线段 `segments`） |
| GET | `/api/alignments/{id}` | 线路详情与已导入记录 |
| POST | `/api/alignments/{id}/inspections` | 按实地先后导入巡检记录 |
| POST | `/api/alignments/{id}/evaluate?version=` | 逐笔候选；唯一者落图并列覆盖宗地（`version` 缺省为当前版本） |
| PATCH | `/api/inspections/{id}` | 补填/更正一笔所在段（`segment_id: null` 清空），返回联动后整批结果 |
| GET | `/api/inspections/{id}/corrections` | 该笔的补正历史 |

## 样例数据（`samples/`）

- `initial.geojson`：P1、P2 两宗共边相邻宗地，用于初始导入；
- `split_p1.geojson`：P1 拆分为 P1A/P1B（共边 x=5）；
- `merge_p1.geojson`：P1A/P1B 合并回 P1C；
- `invalid_change.geojson`：只覆盖 P1 左部的问题面，用于预览缺口；
- `alignment.json`：S1/S2/S3 三段相接、S2 起桩号回跳 10 的线路示例；
- `alignment_parcels.geojson`：与上述线路匹配、覆盖 x 0..300 的宗地（K1~K3）；
- `inspections.json`：含回跳候选、补填所在段的巡检记录示例；
- `inspections_unsolvable.json`：120 之后接 60 的整批无解示例。

## 测试

```bash
pip install -r requirements.txt pytest httpx
pytest -v
```

覆盖：导入校验（非整数/带洞/重叠/重复导入）、缺口与侵占预核验、预核验不落库、
非法提交被拒且数据不变、拆分、合并、历史与版本快照、多线程竞争提交（200+409）、
失败回滚；线路校验（不连续/桩号不递增/段号重复）、回跳多候选、共用端点合并、
补正联动唯一性与前后记录、整批无解原因、无解补正不写入、按所选版本列覆盖宗地。
