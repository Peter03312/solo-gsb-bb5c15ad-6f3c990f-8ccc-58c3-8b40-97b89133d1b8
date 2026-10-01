# 临时限行核验服务

面向场地安全员的临时限行区域登记、轨迹穿越核验与安全出发时刻调度服务。
安全员在平面直角坐标系中登记带 UTC 生效时段的简单多边形区域；系统对按时刻
严格递增、相邻点直线匀速运动的连续轨迹做整段核验，返回每个区域的穿越起止
时刻以及总体是否安全；也可以对一条固定行程（相对秒数轨迹）在带时区的候选
出发时段内，精确求出所有不会触碰限行区的出发区间。此外，安全员可以为指定
通行轨迹**签发可取消的通行凭证**：未取消且行程未结束的凭证会约束后续一切
区域变更，任何会使其轨迹不再安全的登记/修订/删除/批量变更都会被整体拒绝。

## 核验规则

* **轨迹**：至少 2 个点 `(x, y, t)`；时刻必须严格递增（重复/逆序报错且不处理）；
  相邻点之间按**直线匀速**运动。
* **交点插值**：交点时刻通过线段参数插值得到，**不做采样点检查**——即使所有提交
  的采样点都在区域外，只要两点连线穿过区域就会被检出。
* **边界即穿越**：沿边界运动、与顶点单点接触都算穿越；单点接触返回
  `entry_time == exit_time` 的退化区间，并置 `point_contact: true`。
* **区间合并**：同一区域相接（端点相同）或重叠的穿越段合并为一段；
  不同区域分别列出。
* **生效时段左闭右开**：区域仅在 `[valid_from, valid_to)` 内参与核验。
  * 接触时刻恰为 `valid_from`：参与（左端闭）；
  * 接触时刻恰为 `valid_to`：不参与（右端开，此时区域已失效）；
  * 正长度穿越跨过 `valid_to` 时，区间在 `valid_to` 处截断，
    `exit_inclusive=false`。
* **非法输入不保存**：非法多边形（自相交、退化、面积为 0）、非有限坐标
  （NaN/Infinity）、`valid_from >= valid_to` 均返回 422 且不落库。
* **一致性快照**：一次核验在单个 `REPEATABLE READ READ ONLY` 事务中读取区域集合，
  即使核验期间有区域被并发登记/修订/删除，本次核验仍使用同一已提交版本；
  响应中的 `revision` 标明所用集合版本。

## 批量原子变更规则（`POST /api/regions/batch`）

安全员可以把**多处**限行区域的登记/修订/删除放在**一次变更**里提交，并要求
一条或多条指定通行轨迹在变更后的完整区域集合下仍然安全：

* **请求**：`expected_revision`（预期全局 revision）、`operations`（非空，
  元素为 `create` / `update` / `delete`，按数组顺序执行）、`trajectories`
  （至少 1 条带时区绝对时刻轨迹，结构与 `/api/verify` 相同；每条 ≥2 点、
  时刻严格递增）。
* **同一现有区域在一批中只能出现一次**：对同一 `id` 两次 update、
  update 与 delete 混用等都返回 422 `duplicate_region`（create 不占现有 id，
  可任意多个）。
* **对变更后完整集合核验**：未被本批涉及的区域原样保留，删除的区域不参与，
  新建/修订后的区域以新形态参与；沿用直线匀速、**边界接触即穿越**、
  生效时段 **左闭右开** 的既有规则。所有轨迹必须全部安全。
* **原子提交**：仅当 revision 匹配、全部操作合法且全部轨迹安全时才落库；
  已有区域修订沿用 `version = version + 1`，**整批全局 revision 只递增一次**。
* **并发互斥**：批量接口与单个登记/修订/删除共用同一把全局元数据行锁，
  写事务在此串行化；基于过期 revision 的并发提交无法绕过校验，直接返回 409。
* **成功响应**：新 `revision`、逐操作结果（create 带**新区域 ID** 与
  `version=1`，update 带修订后 `version`，delete 回显被删 id 与删除前
  version），以及逐轨迹核验结果（含每条轨迹对每个区域的穿越明细）。
* **失败不落库，原因可区分**：

| 状态码 | error.code | 触发条件 |
| --- | --- | --- |
| 409 | `revision_conflict` | `expected_revision` 与当前 revision 不符（details 带当前值） |
| 404 | `region_not_found` | update/delete 引用了不存在的区域（带操作下标与 id） |
| 422 | `duplicate_region` | 同一现有区域在一批中出现多次 |
| 422 | `invalid_operation` | 单条操作语义非法（如修订后 `valid_from >= valid_to`），带操作下标 |
| 422 | `unsafe_trajectory` | 存在轨迹在变更后集合下穿越；details 列出轨迹下标与穿越起止明细 |
| 409 | `pass_conflict` | 变更后集合将使未取消且未结束的凭证不安全；details 列出冲突凭证 id |
| 422 | `validation_error` / `invalid_polygon` / `invalid_trajectory` | 结构/多边形/轨迹非法（同既有接口） |

## 通行凭证规则（签发 / 查询 / 取消）

安全员可以为一条指定通行轨迹签发**可取消的通行凭证**；未取消且行程未结束
的凭证会约束后续所有区域写入，保证已放行的轨迹不会因区域变更而变得不安全。

* **签发 `POST /api/passes`**：请求体与 `/api/verify` 相同（`points` 至少
  2 点、带时区绝对时刻、时刻严格递增、相邻点直线匀速），且**首点时刻必须
  晚于签发时刻**（以服务端拿到写锁后的当前时间为准；已开始的轨迹拒绝）。
  签发在持有全局元数据行锁的写事务内完成，按**当前完整区域集合**核验，
  沿用边界接触即违规、生效期左闭右开规则：
  * 安全：持久化并返回 **201**，响应含凭证 `id`、`status`、`revision`
    （签发时核验所用的区域集合版本）、回显轨迹与 `issued_at`；
    签发不推进区域 revision；
  * 不安全：**422** `unsafe_trajectory`，details 列出穿越区域与起止明细，
    不落库；
  * 非法（结构/时刻问题）或已开始：**422** `invalid_trajectory` /
    `validation_error`，不落库。
* **查询 `GET /api/passes/{id}`**：返回凭证（含 `status`：
  `active`/`cancelled`）；未知 ID 返回 **404** `not_found`。
* **取消 `POST /api/passes/{id}/cancel`**：置为 `cancelled` 并返回凭证状态；
  **重复取消幂等**（结果不变，仍为 200）；未知 ID 返回 **404**。
  取消不推进区域 revision。
* **凭证约束区域变更**：**未取消且末点时刻未过**的凭证，在单区登记、修订、
  删除及批量变更**提交前**都会被重新核验——用变更后的完整区域集合检查这些
  凭证的轨迹是否仍然安全；任一凭证不再安全则返回 **409** `pass_conflict`
  （details 逐条列出冲突凭证 `pass_id`），**整笔回滚**，区域 `version` 与
  全局 `revision` 均不变。已取消或末点时刻已过的凭证不再约束。
* **串行化**：签发、取消与所有区域写入（含批量）共用同一把全局元数据行锁，
  完全串行化——不存在"核验通过到落库之间被并发变更插队"的漏检窗口。

## 出发时刻调度规则（`POST /api/schedule`）

* **输入**：固定行程用**相对秒数**给出 `{"x","y","t"}`，首点相对秒数必须为 `0`，
  其余时刻严格递增；候选出发时段 `[window_start, window_end)` 带时区，服务端
  归一化为 UTC。相邻点之间仍按直线匀速运动。
* **精确区间运算，不做离散抽样**：对每个区域，先求轨迹与多边形（**含边界**）
  的全部几何接触，得到相对时刻闭区间 `[a, b]`（单点接触时 `a == b`）。
  设出发时刻为 `s`、区域生效窗口为 `[vf, vt)`，则该接触造成违规当且仅当
  `存在 τ∈[a,b] 使 vf ≤ s+τ < vt`，等价于
  **`s ∈ [vf-b, vt-a)`（左闭右开）**。该公式对正长度接触与单点接触统一成立。
* **端点语义准确**：
  * 恰在 `valid_from` 接触 → 对应出发时刻禁止（禁止区间左端闭）；
  * 恰在 `valid_to` 接触 → 对应出发时刻安全（禁止区间右端开）；
  * 沿边界整段运动按闭区间 `[a,b]` 参与计算，边界即违规；
  * 各区域禁止区间取并集（相接或重叠即合并），再从候选窗口（左闭右开）中扣除。
* **输出**：按起点排序的安全出发区间，端点统一为 UTC ISO8601 并带
  `start_inclusive/end_inclusive` 开闭标记（当前规则下恒为左闭右开）；
  无任何可行时刻时返回 `"intervals": []`。响应带 `revision`。
* **拒绝整个请求（422）**：轨迹点少于 2 个；坐标或相对时刻非有限
  （NaN/Infinity）；首点相对秒数不为 0 或相对时刻不严格递增；
  `window_start >= window_end`；候选时段不带时区。
* **一致性快照**：与 `/api/verify` 相同，整个计算在单个
  `REPEATABLE READ READ ONLY` 事务中完成，区域并发修订时本请求使用同一已提交
  快照。

## 技术栈

Python 3.11 · FastAPI · PostgreSQL 16 · Shapely 2（GEOS）· psycopg3 · Uvicorn

## 一键启动（Docker Compose）

```bash
docker compose up --build
```

启动后：

| 地址 | 说明 |
| --- | --- |
| http://localhost:8000 | 服务根路径（端点列表） |
| http://localhost:8000/docs | Swagger UI（可在线调用） |
| http://localhost:8000/openapi.json | OpenAPI 描述 |
| http://localhost:8000/health | 健康检查 |
| PostgreSQL | localhost:5432（用户/库/密码均为 restrict） |

首次启动时应用会自动建表（`regions`、`region_meta`、`passes`）并初始化全局版本号，
无需手工执行 SQL。数据库未就绪时应用会自动重试连接（默认 30 次、间隔 1 秒）。

停止并清理数据卷：

```bash
docker compose down -v
```

## 配置（环境变量）

| 变量 | 默认值 | 说明 |
| --- | --- | --- |
| `DATABASE_URL` | `postgresql://restrict:restrict@localhost:5432/restrict` | PostgreSQL DSN |
| `API_PORT` | `8000` | Compose 映射到宿主机的 API 端口 |
| `POSTGRES_PORT` | `5432` | Compose 映射到宿主机的数据库端口 |
| `DB_CONNECT_RETRIES` | `30` | 启动时连接数据库的重试次数 |
| `DB_CONNECT_INTERVAL` | `1.0` | 重试间隔（秒） |

例如改用 18000 端口：`API_PORT=18000 docker compose up --build`。

## 本地（无 Docker）运行

```bash
pip install -r requirements.txt
export DATABASE_URL="postgresql://restrict:restrict@localhost:5432/restrict"
uvicorn app.main:app --host 0.0.0.0 --port 8000
```

## HTTP API

时间字段一律使用带时区的 ISO8601，建议统一 `...Z`（UTC）。
多边形顶点按 `[[x, y], ...]` 提交，首尾点可显式闭合也可不闭合；
服务端校验为简单多边形后归一化为逆时针闭合环存储。

### 区域管理

| 方法 | 路径 | 说明 |
| --- | --- | --- |
| POST | `/api/regions` | 登记区域（201） |
| GET | `/api/regions` | 列出全部区域 |
| GET | `/api/regions/{id}` | 查询单个区域 |
| PATCH | `/api/regions/{id}` | 修订区域（字段任意组合，部分更新）；`version` +1 |
| DELETE | `/api/regions/{id}` | 删除区域 |
| POST | `/api/regions/batch` | 多处区域登记/修订/删除一次原子提交，并核验指定轨迹全部安全 |
| GET | `/api/revision` | 当前区域集合全局版本号（任意写操作后单调 +1） |
| POST | `/api/verify` | 对一条绝对时刻轨迹做整段穿越核验 |
| POST | `/api/schedule` | 对一条相对秒数固定行程，求候选时段内全部安全出发区间 |
| POST | `/api/passes` | 为一条绝对时刻轨迹签发可取消的通行凭证（201） |
| GET | `/api/passes/{id}` | 按 ID 查询凭证（含状态） |
| POST | `/api/passes/{id}/cancel` | 取消凭证（幂等），返回状态 |

登记示例：

```bash
curl -X POST http://localhost:8000/api/regions \
  -H 'Content-Type: application/json' \
  -d '{
        "name": "A区-焊接作业面",
        "polygon": [[0,0],[100,0],[100,100],[0,100]],
        "valid_from": "2026-09-01T00:00:00Z",
        "valid_to":   "2026-10-01T00:00:00Z"
      }'
```

### 轨迹核验 `POST /api/verify`

请求：

```json
{
  "points": [
    {"x": -50, "y": 50, "t": "2026-09-30T00:00:00Z"},
    {"x": 150, "y": 50, "t": "2026-09-30T00:01:00Z"}
  ]
}
```

响应（该轨迹在 00:00:15 进入、00:00:45 离开 0~100 的区域，时刻由插值得到）：

```json
{
  "safe": false,
  "revision": 1,
  "checked_at": "2026-09-30T03:40:00.000000Z",
  "results": [
    {
      "region_id": 1,
      "region_name": "A区-焊接作业面",
      "crossings": [
        {
          "entry_time": "2026-09-30T00:00:15Z",
          "exit_time": "2026-09-30T00:00:45Z",
          "entry_inclusive": true,
          "exit_inclusive": true,
          "point_contact": false
        }
      ]
    }
  ]
}
```

* `safe=true` 当且仅当所有区域的 `crossings` 均为空。
* `entry_inclusive/exit_inclusive` 标明端点开闭；穿越被生效窗口右端截断时
  `exit_inclusive=false`。
* 无穿越的区域也会出现在 `results` 中（`crossings: []`），保证逐区域可核对。

### 批量原子变更 `POST /api/regions/batch`

把多处区域变更与"这些变更后轨迹必须安全"的前置条件合并为一次原子提交。
操作按数组顺序在内存中构造**变更后完整区域集合**，再对每条轨迹核验；
全部通过后才一次性写入，全局 revision 只推进一次。

请求（`update`/`delete` 用 `id` 引用现有区域；`update` 字段任意组合的部分更新）：

```json
{
  "expected_revision": 2,
  "operations": [
    {
      "action": "update",
      "id": 1,
      "name": "A区-缩小",
      "polygon": [[0, 0], [50, 0], [50, 50], [0, 50]]
    },
    {
      "action": "delete",
      "id": 2
    },
    {
      "action": "create",
      "name": "C区-新作业面",
      "polygon": [[400, 0], [500, 0], [500, 100], [400, 100]],
      "valid_from": "2026-10-01T00:00:00Z",
      "valid_to":   "2026-11-01T00:00:00Z"
    }
  ],
  "trajectories": [
    [
      {"x": -50, "y": 200, "t": "2026-10-02T00:00:00Z"},
      {"x": 900, "y": 200, "t": "2026-10-02T00:02:00Z"}
    ]
  ]
}
```

成功响应（HTTP 200）：

```json
{
  "revision": 3,
  "operations": [
    {"index": 0, "action": "update", "region_id": 1, "version": 3},
    {"index": 1, "action": "delete", "region_id": 2, "version": 4},
    {"index": 2, "action": "create", "region_id": 5, "version": 1}
  ],
  "trajectories": [
    {
      "index": 0,
      "safe": true,
      "results": [
        {"region_id": 1, "region_name": "A区-缩小", "crossings": []},
        {"region_id": 5, "region_name": "C区-新作业面", "crossings": []}
      ]
    }
  ]
}
```

* `operations[].index` 回显操作在请求数组中的下标；create 返回新分配的
  `region_id`；update 的 `version` 是修订后的新版本（原版本 +1）；
  delete 回显删除前 version。
* `trajectories[].results` 与 `/api/verify` 的逐区域结构一致（包含边界
  点接触、被 `valid_to` 截断等开闭标记），但区域集合是**变更后**的集合。

不安全时（HTTP 422，整批取消、不留任何痕迹）只列出发生穿越的轨迹与区域：

```json
{
  "error": {
    "code": "unsafe_trajectory",
    "message": "存在轨迹在变更后区域集合下发生穿越，整批变更已取消且不落库",
    "details": [
      {
        "trajectory_index": 0,
        "results": [
          {
            "region_id": 5,
            "region_name": "C区-新作业面",
            "crossings": [
              {
                "entry_time": "2026-10-02T00:01:10Z",
                "exit_time": "2026-10-02T00:01:30Z",
                "entry_inclusive": true,
                "exit_inclusive": true,
                "point_contact": false
              }
            ]
          }
        ]
      }
    ]
  }
}
```

* revision 过期（期间有其他登记/修订/删除已提交）返回 **409**
  `revision_conflict`，`details[0].current_revision` 给出当前值；
  客户端用新 revision 重新提交即可（乐观并发）。
* 引用不存在区域返回 **404** `region_not_found`；同一区域在一批中出现
  多次返回 **422** `duplicate_region`；修订后时段非法等返回 **422**
  `invalid_operation`（均带操作下标）。上述任一失败整批不落库、
  revision 不变。
* 若变更后集合会使某张未取消且未结束的通行凭证不再安全，返回 **409**
  `pass_conflict`（见下节），整批回滚。

### 通行凭证 `POST /api/passes`

为指定通行轨迹签发可取消的凭证。请求体与 `/api/verify` 相同，但首点时刻
必须晚于签发时刻（已开始的轨迹拒绝）：

```json
{
  "points": [
    {"x": -50, "y": 200, "t": "2026-10-01T12:00:00Z"},
    {"x": 350, "y": 200, "t": "2026-10-01T12:05:00Z"}
  ]
}
```

安全时返回 **201**（`revision` 为签发时核验所用的区域集合版本）：

```json
{
  "id": 1,
  "status": "active",
  "revision": 3,
  "trajectory": [
    {"x": -50, "y": 200, "t": "2026-10-01T12:00:00Z"},
    {"x": 350, "y": 200, "t": "2026-10-01T12:05:00Z"}
  ],
  "issued_at": "2026-10-01T11:55:00Z",
  "cancelled_at": null
}
```

不安全时返回 **422** `unsafe_trajectory`，details 直接列出有穿越的区域
（结构与 `/api/verify` 的 `results` 相同，只含违规区域），凭证不落库：

```json
{
  "error": {
    "code": "unsafe_trajectory",
    "message": "轨迹在当前区域集合下发生穿越，凭证未签发且不落库",
    "details": [
      {
        "region_id": 1,
        "region_name": "A区-焊接作业面",
        "crossings": [
          {
            "entry_time": "2026-10-01T12:00:15Z",
            "exit_time": "2026-10-01T12:00:45Z",
            "entry_inclusive": true,
            "exit_inclusive": true,
            "point_contact": false
          }
        ]
      }
    ]
  }
}
```

查询与取消：

```bash
curl http://localhost:8000/api/passes/1          # 查询，未知 ID 返回 404
curl -X POST http://localhost:8000/api/passes/1/cancel   # 取消，幂等
```

取消成功返回同一凭证结构，`status` 变为 `cancelled` 并带 `cancelled_at`；
对已取消的凭证再次取消返回相同结果（幂等），未知 ID 返回 404。

凭证约束区域变更的冲突响应（单区登记/修订/删除与批量变更一致）为
**409** `pass_conflict`，details 逐条列出冲突凭证，整笔回滚：

```json
{
  "error": {
    "code": "pass_conflict",
    "message": "变更后区域集合与 1 张未取消通行凭证冲突，整笔变更已回滚",
    "details": [
      {"pass_id": 1, "message": "凭证 1（未取消且末点时刻未过）的轨迹在变更后区域集合下不再安全"}
    ]
  }
}
```

### 出发时刻调度 `POST /api/schedule`

请求：固定行程用相对首点的秒数（首点 `t=0`、严格递增），候选时段带时区：

```json
{
  "points": [
    {"x": -50, "y": 50, "t": 0},
    {"x": 150, "y": 50, "t": 60}
  ],
  "window_start": "2026-08-31T23:58:00Z",
  "window_end":   "2026-09-01T00:02:00Z"
}
```

假设某区域为 `[0,100]×[0,100]`、仅在 `2026-09-01T00:00:00Z` 至
`2026-09-01T00:01:00Z` 生效，则轨迹在相对 `15s~45s` 与区域接触（含边界），
禁止出发区间为 `[vf-45s, vt-15s) = [2026-08-31T23:59:15Z, 2026-09-01T00:00:45Z)`，
候选窗口被切成两段安全区间：

```json
{
  "revision": 1,
  "window_start": "2026-08-31T23:58:00Z",
  "window_end": "2026-09-01T00:02:00Z",
  "intervals": [
    {
      "start": "2026-08-31T23:58:00Z",
      "end": "2026-08-31T23:59:15Z",
      "start_inclusive": true,
      "end_inclusive": false
    },
    {
      "start": "2026-09-01T00:00:45Z",
      "end": "2026-09-01T00:02:00Z",
      "start_inclusive": true,
      "end_inclusive": false
    }
  ]
}
```

* 区间端点为 UTC，恒为左闭右开；`intervals` 为空表示候选时段内无安全出发时刻。
* 时区处理：`window_start/window_end` 可提交任意带时区的 ISO8601（如
  `2026-09-01T08:00:00+08:00`），服务端归一化为 UTC；不带时区返回 422。
* 几何接触为单点时（如擦过顶点），接触区间退化为 `[a,a]`，禁止区间长度恰为
  区域生效窗口长度；接触恰落在 `valid_to` 的那次出发安全（右端开），
  落在 `valid_from` 的那次出发禁止（左端闭）。

### 错误响应

统一使用 `422`（语义错误）、`404`（区域/凭证不存在）或 `409`（批量提交的
revision 冲突、区域变更与未取消凭证冲突）。除通用的 `validation_error` 外，
轨迹类错误使用专用错误码：`/api/verify` 与 `/api/passes` 为
`invalid_trajectory`，`/api/schedule` 的相对轨迹问题同样返回
`invalid_trajectory`，候选时段不递增返回 `invalid_input`；
`/api/regions/batch` 另用 `revision_conflict`（409）、`region_not_found`（404）、
`duplicate_region`、`invalid_operation`、`unsafe_trajectory`（均 422）；
凭证相关另用 `pass_conflict`（409，区域变更危及未取消凭证）与
`not_found`（404，未知凭证 ID）：

```json
{
  "error": {
    "code": "validation_error",
    "message": "请求参数校验失败",
    "details": [{"loc": ["body", "polygon"], "message": "...", "type": "..."}]
  }
}
```

## 调用示例

```bash
# 端到端 curl 示例：安全出发区间（含单点接触/生效端点）、登记区域、
# 核验穿越、修订与删除
bash examples/curl_examples.sh

# 批量原子变更（登记/修订/删除 + 轨迹核验；含不安全拒绝、revision 冲突重放）
bash examples/curl_batch.sh

# 通行凭证（签发/查询/取消；含 409 pass_conflict 约束、幂等取消、
# 不安全与已开始轨迹拒签）
bash examples/curl_passes.sh

# 仅用 Python 标准库的调用示例（含调度两段安全区间与通行凭证演示）
python3 examples/python_client.py
```

## 测试

```bash
# 纯几何引擎：穿越核验用例，不需要数据库
python3 tests/test_geometry.py

# 纯几何引擎：安全出发区间调度用例（精确端点、单点接触、沿边界、
# 生效窗口端点、多区域并集等），不需要数据库
python3 tests/test_schedule_geometry.py

# HTTP + PostgreSQL 集成测试（需要可连接的数据库，会清空 regions 表）
DATABASE_URL="postgresql://restrict@localhost:5432/restrict" python3 tests/test_api.py

# 批量原子变更 API 集成测试（成功/各种拒绝原因/左闭右开端点/并发竞争，
# 同样会清空 regions 表）
DATABASE_URL="postgresql://restrict@localhost:5432/restrict" python3 tests/test_batch_api.py

# 通行凭证 API 集成测试（签发/查询/取消、凭证约束区域变更、过期放行、
# 签发与区域写入并发串行化，会清空 regions/passes 表）
DATABASE_URL="postgresql://restrict@localhost:5432/restrict" python3 tests/test_passes_api.py
```

## 项目结构

```
app/
  main.py        # FastAPI 路由、事务边界、错误处理（verify、schedule、batch、passes）
  schemas.py     # Pydantic 请求/响应模型与输入校验（含批量操作判别联合）
  geometry.py    # 穿越核验与安全出发区间引擎（插值、裁剪、合并、区间差）
  repository.py  # 区域 CRUD、一致性快照读取、批量原子变更事务、通行凭证签发/取消
  db.py          # psycopg3 连接池、建表初始化（regions/region_meta/passes）、启动重试
examples/        # curl 与 Python 调用示例（含批量原子变更与通行凭证）
tests/           # 几何离线测试与 API/批量变更/通行凭证集成测试
Dockerfile, docker-compose.yml
```
