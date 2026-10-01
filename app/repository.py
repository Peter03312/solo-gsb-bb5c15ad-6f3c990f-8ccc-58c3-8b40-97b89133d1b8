"""区域数据访问：CRUD、一致性快照读取与批量原子变更。"""
from __future__ import annotations

import json
from datetime import datetime, timezone

from psycopg.rows import dict_row
from shapely.geometry import Polygon
from shapely.wkt import dumps as wkt_dumps

from . import geometry
from .schemas import (
    RegionCreateInBatch,
    RegionDeleteInBatch,
    RegionUpdateInBatch,
)


def _epoch(dt: datetime) -> float:
    return dt.timestamp()


# ---------------------------------------------------------------- 批量变更异常

class BatchRevisionConflict(Exception):
    """expected_revision 与库内当前 revision 不符。"""

    def __init__(self, expected: int, current: int):
        super().__init__(f"revision 已过期：expected={expected}, current={current}")
        self.expected = expected
        self.current = current


class BatchRegionsNotFound(Exception):
    """批内引用了不存在的区域。"""

    def __init__(self, missing: list[dict]):
        super().__init__("批内存在不存在的区域")
        self.missing = missing


class BatchDuplicateRegion(Exception):
    """同一现有区域在一批操作中出现多次。"""

    def __init__(self, region_id: int, indices: list[int]):
        super().__init__(f"区域 {region_id} 在同一批中出现了多次")
        self.region_id = region_id
        self.indices = indices


class BatchInvalidOperation(Exception):
    """批内单条操作的数据不合法（如生效时段不递增）。"""

    def __init__(self, index: int, message: str, field: str | None = None):
        super().__init__(message)
        self.index = index
        self.message = message
        self.field = field


class BatchUnsafeTrajectories(Exception):
    """变更后集合下存在不安全轨迹；携带逐轨迹核验明细。"""

    def __init__(self, trajectory_results: list[dict]):
        super().__init__("存在轨迹在变更后区域集合下发生穿越")
        self.trajectory_results = trajectory_results


# ---------------------------------------------------------------- 通行凭证异常

class PassAlreadyStarted(Exception):
    """提交凭证的轨迹首点时刻不晚于签发时刻，拒绝签发。"""


class PassTrajectoryUnsafe(Exception):
    """凭证轨迹在当前完整区域集合下不安全；携带逐区域穿越明细。"""

    def __init__(self, results: list[dict]):
        super().__init__("凭证轨迹在当前区域集合下发生穿越，不予签发")
        self.results = results


class ActivePassConflict(Exception):
    """区域变更会使仍生效的凭证轨迹变得不安全。

    携带逐个冲突凭证的 id 与穿越明细，调用方整笔回滚。
    """

    def __init__(self, conflicts: list[dict]):
        super().__init__("变更后区域集合会使仍生效的通行凭证不再安全")
        self.conflicts = conflicts


def _row_to_region(row: dict) -> dict:
    return {
        "id": row["id"],
        "name": row["name"],
        "polygon": row["polygon"],
        "valid_from": row["valid_from"],
        "valid_to": row["valid_to"],
        "version": row["version"],
        "created_at": row["created_at"],
        "updated_at": row["updated_at"],
    }


def _region_dict(row: dict) -> dict:
    """数据库区域行（含 wkt）→ 几何核验用 region dict。"""
    return {
        "id": row["id"],
        "name": row["name"],
        "valid_from": _epoch(row["valid_from"]),
        "valid_to": _epoch(row["valid_to"]),
        "polygon": geometry.polygon_from_wkt(row["wkt"]),
    }


def get_revision(cur) -> int:
    cur.execute("SELECT revision FROM region_meta WHERE id = 1 FOR UPDATE")
    return cur.fetchone()["revision"]


def get_revision_shared(cur) -> int:
    cur.execute("SELECT revision FROM region_meta WHERE id = 1")
    return cur.fetchone()["revision"]


def bump_revision(cur) -> int:
    """在写事务内推进全局版本号（调用方应已持有该行行锁）。"""
    cur.execute(
        "UPDATE region_meta SET revision = revision + 1 WHERE id = 1 "
        "RETURNING revision"
    )
    return cur.fetchone()["revision"]


def create_region(cur, payload, polygon_vertices: list[list[float]]) -> tuple[dict, int]:
    wkt = wkt_dumps(Polygon(polygon_vertices), rounding_precision=-1)
    # 锁定元数据行，使区域写入（含凭证签发/取消）串行化
    get_revision(cur)

    # 预分配真实 id，使凭证冲突明细中的 region_id 与最终落库一致
    cur.execute("SELECT nextval(pg_get_serial_sequence('regions', 'id')) AS id")
    new_id = int(cur.fetchone()["id"])
    candidate_row = {
        "id": new_id,
        "name": payload.name,
        "wkt": wkt,
        "valid_from": payload.valid_from,
        "valid_to": payload.valid_to,
    }

    # 变更后完整集合 = 当前全部区域 + 新建区域；生效凭证必须仍安全
    current = _load_all_regions_locked(cur)
    candidate = [_region_dict(r) for r in current.values()]
    candidate.append(_region_dict(candidate_row))
    candidate.sort(key=lambda r: r["id"])
    check_active_passes_against(cur, candidate)

    cur.execute(
        """
        INSERT INTO regions (id, name, valid_from, valid_to, polygon, wkt)
        VALUES (%(id)s, %(name)s, %(valid_from)s, %(valid_to)s,
                %(polygon)s::jsonb, %(wkt)s)
        RETURNING id, name, polygon, valid_from, valid_to, version,
                  created_at, updated_at
        """,
        {
            "id": new_id,
            "name": payload.name,
            "valid_from": payload.valid_from,
            "valid_to": payload.valid_to,
            "polygon": json.dumps(polygon_vertices),
            "wkt": wkt,
        },
    )
    row = cur.fetchone()
    revision = bump_revision(cur)
    return _row_to_region(row), revision


def list_regions(cur) -> list[dict]:
    cur.execute(
        """
        SELECT id, name, polygon, valid_from, valid_to, version,
               created_at, updated_at
        FROM regions
        ORDER BY id
        """
    )
    return [_row_to_region(r) for r in cur.fetchall()]


def get_region_for_update(cur, region_id: int) -> dict | None:
    cur.execute(
        """
        SELECT id, name, polygon, valid_from, valid_to, version,
               created_at, updated_at, wkt
        FROM regions WHERE id = %s
        FOR UPDATE
        """,
        (region_id,),
    )
    return cur.fetchone()


def update_region(cur, region_id: int, payload, new_polygon) -> tuple[dict, int] | None:
    # 写路径统一按「先全局 meta 行、后区域行」加锁，与批量变更保持同一加锁顺序，
    # 避免不同写事务之间出现交叉等待（死锁）
    get_revision(cur)
    row = get_region_for_update(cur, region_id)
    if row is None:
        return None

    name = payload.name if payload.name is not None else row["name"]
    valid_from = payload.valid_from if payload.valid_from is not None else row["valid_from"]
    valid_to = payload.valid_to if payload.valid_to is not None else row["valid_to"]
    if valid_from >= valid_to:
        raise ValueError("生效时段必须满足 valid_from < valid_to（左闭右开，起早于止）")

    if new_polygon is not None:
        polygon_json = new_polygon
        wkt = wkt_dumps(Polygon(new_polygon), rounding_precision=-1)
    else:
        polygon_json = row["polygon"]
        wkt = row["wkt"]

    candidate_row = {
        "id": region_id,
        "name": name,
        "polygon": polygon_json,
        "wkt": wkt,
        "valid_from": valid_from,
        "valid_to": valid_to,
    }

    # 构造变更后完整集合（未涉及区域原样保留），校验生效凭证仍安全
    current = _load_all_regions_locked(cur)
    candidate = [
        _region_dict(candidate_row if r["id"] == region_id else r)
        for r in current.values()
    ]
    check_active_passes_against(cur, candidate)

    cur.execute(
        """
        UPDATE regions SET
            name = %(name)s,
            valid_from = %(valid_from)s,
            valid_to = %(valid_to)s,
            polygon = %(polygon)s::jsonb,
            wkt = %(wkt)s,
            version = version + 1,
            updated_at = now()
        WHERE id = %(id)s
        RETURNING id, name, polygon, valid_from, valid_to, version,
                  created_at, updated_at
        """,
        {
            "id": region_id,
            "name": name,
            "valid_from": valid_from,
            "valid_to": valid_to,
            "polygon": json.dumps(polygon_json),
            "wkt": wkt,
        },
    )
    updated = _row_to_region(cur.fetchone())
    revision = bump_revision(cur)
    return updated, revision


def delete_region(cur, region_id: int) -> tuple[bool, int | None]:
    # 与其它写路径一致：先锁全局 meta 行，再锁/删区域行
    get_revision(cur)
    cur.execute(
        """
        SELECT id FROM regions WHERE id = %s
        FOR UPDATE
        """,
        (region_id,),
    )
    existing = cur.fetchone()
    if existing is None:
        return False, None
    # 删除只会移除约束，不可能让已安全的凭证变得不安全，无需凭证核验
    cur.execute("DELETE FROM regions WHERE id = %s", (region_id,))
    revision = bump_revision(cur)
    return True, revision


def load_snapshot(cur) -> tuple[int, list[dict]]:
    """在 REPEATABLE READ 只读事务中读取同一已提交版本的区域集合。

    返回 (revision, regions)；regions 内已构造好 Shapely Polygon。
    """
    revision = get_revision_shared(cur)
    cur.execute(
        """
        SELECT id, name, valid_from, valid_to, wkt
        FROM regions
        ORDER BY id
        """
    )
    rows = cur.fetchall()
    regions = []
    for row in rows:
        regions.append({
            "id": row["id"],
            "name": row["name"],
            "valid_from": _epoch(row["valid_from"]),
            "valid_to": _epoch(row["valid_to"]),
            "polygon": geometry.polygon_from_wkt(row["wkt"]),
        })
    return revision, regions


def configure_cursor(cur) -> None:
    cur.row_factory = dict_row


# ---------------------------------------------------------------- 通行凭证

def _trajectory_json(traj: list[tuple[float, float, float]]) -> list[dict]:
    """已校验轨迹 [(epoch,x,y), ...] → 持久化用 UTC ISO8601 点数组。"""
    return [
        {
            "x": x,
            "y": y,
            "t": datetime.fromtimestamp(t, tz=timezone.utc)
            .isoformat()
            .replace("+00:00", "Z"),
        }
        for t, x, y in traj
    ]


def _trajectory_from_json(points) -> list[tuple[float, float, float]]:
    """持久化点数组 → 几何核验轨迹 [(epoch,x,y), ...]。"""
    return [
        (
            datetime.fromisoformat(p["t"].replace("Z", "+00:00")).timestamp(),
            float(p["x"]),
            float(p["y"]),
        )
        for p in points
    ]


def load_active_passes(cur) -> list[dict]:
    """加载「未取消 且 末点时刻未过」的凭证（调用方应已持有 region_meta 行锁）。

    末点是否已过以数据库当前时刻 now() 判定；凭证签发/取消与区域写入共用
    同一把 meta 行锁，因此读到的凭证集合相对并发的签发/取消是稳定的。
    """
    cur.execute(
        """
        SELECT id, trajectory, region_revision, first_time, last_time,
               created_at, cancelled, cancelled_at
        FROM passes
        WHERE cancelled = FALSE AND last_time > now()
        ORDER BY id
        """
    )
    return list(cur.fetchall())


def check_active_passes_against(cur, regions: list[dict]) -> None:
    """核验全部生效凭证在给定区域集合下仍安全；不安全抛 ActivePassConflict。"""
    active = load_active_passes(cur)
    conflicts: list[dict] = []
    for row in active:
        traj = _trajectory_from_json(row["trajectory"])
        results = geometry.verify_trajectory(traj, regions)
        violated = [r for r in results if r["crossings"]]
        if violated:
            conflicts.append({"pass_id": row["id"], "results": violated})
    if conflicts:
        raise ActivePassConflict(conflicts)


def issue_pass(cur, traj: list[tuple[float, float, float]]) -> dict:
    """在已开启的写事务内签发凭证（调用方通过本函数统一加 meta 行锁）。

    1) 锁定 region_meta 行——与所有区域写入、凭证签发/取消串行化；
    2) 首点须严格晚于数据库当前时刻（已开始的轨迹拒绝且不保存）；
    3) 按当前完整区域集合核验（直线匀速、边界接触即穿越、生效期左闭右开），
       不安全抛 PassTrajectoryUnsafe，不落库；
    4) 安全则持久化并返回凭证行。签发本身不推进区域 revision。
    """
    cur.execute("SELECT revision FROM region_meta WHERE id = 1 FOR UPDATE")
    current_revision = cur.fetchone()["revision"]

    cur.execute("SELECT now() AS now")
    now_ts = cur.fetchone()["now"]
    # traj[0] 为 (epoch, x, y)；首点必须严格晚于签发时刻
    if traj[0][0] <= now_ts.timestamp():
        raise PassAlreadyStarted()

    _revision, regions = load_snapshot(cur)
    results = geometry.verify_trajectory(traj, regions)
    if any(r["crossings"] for r in results):
        raise PassTrajectoryUnsafe(results)

    points = _trajectory_json(traj)
    first_time = datetime.fromtimestamp(traj[0][0], tz=timezone.utc)
    last_time = datetime.fromtimestamp(traj[-1][0], tz=timezone.utc)
    cur.execute(
        """
        INSERT INTO passes (trajectory, region_revision, first_time, last_time)
        VALUES (%(trajectory)s::jsonb, %(revision)s, %(first_time)s, %(last_time)s)
        RETURNING id, trajectory, region_revision, first_time, last_time,
                  cancelled, created_at, cancelled_at
        """,
        {
            "trajectory": json.dumps(points),
            "revision": current_revision,
            "first_time": first_time,
            "last_time": last_time,
        },
    )
    return cur.fetchone()


def get_pass(cur, pass_id: int) -> dict | None:
    cur.execute(
        """
        SELECT id, trajectory, region_revision, first_time, last_time,
               cancelled, created_at, cancelled_at
        FROM passes WHERE id = %s
        """,
        (pass_id,),
    )
    return cur.fetchone()


def cancel_pass(cur, pass_id: int) -> dict | None:
    """取消凭证（幂等：重复取消保持首次取消结果）。

    与签发、区域写入共用 region_meta 行锁串行化；取消本身不推进区域
    revision。未知 id 返回 None。
    """
    cur.execute("SELECT revision FROM region_meta WHERE id = 1 FOR UPDATE")
    row = get_pass(cur, pass_id)
    if row is None:
        return None
    if not row["cancelled"]:
        cur.execute(
            """
            UPDATE passes SET cancelled = TRUE, cancelled_at = now()
            WHERE id = %s AND cancelled = FALSE
            RETURNING id, trajectory, region_revision, first_time, last_time,
                      cancelled, created_at, cancelled_at
            """,
            (pass_id,),
        )
        # meta 行锁由本事务持有，其它签发/取消/区域写入无法并发插入，
        # 因此读到未取消就一定能更新成功
        row = cur.fetchone()
    return row


def db_now(cur) -> datetime:
    """数据库当前时刻（与凭证生效判定的 now() 保持同一时钟）。"""
    cur.execute("SELECT now() AS now")
    return cur.fetchone()["now"]


# ---------------------------------------------------------------- 批量原子变更

def _load_all_regions_locked(cur) -> dict[int, dict]:
    """读取全部区域（调用方应已持有 region_meta 行锁，保证期间无并发写）。"""
    cur.execute(
        """
        SELECT id, name, polygon, valid_from, valid_to, version,
               created_at, updated_at, wkt
        FROM regions
        ORDER BY id
        """
    )
    return {r["id"]: r for r in cur.fetchall()}


def _candidate_regions(
    current: dict[int, dict], op_results: list[dict]
) -> list[dict]:
    """根据已计算好的操作结果构造变更后完整区域集合（供轨迹核验）。"""
    regions: list[dict] = []
    for op in op_results:
        rid = op["region_id"]
        if op["action"] == "delete":
            continue
        row = op["_row"]  # create/update 均为落库后的候选行
        regions.append({
            "id": rid,
            "name": row["name"],
            "valid_from": _epoch(row["valid_from"]),
            "valid_to": _epoch(row["valid_to"]),
            "polygon": geometry.polygon_from_wkt(row["wkt"]),
        })
    # 未被本批涉及的现有区域原样保留
    touched = {op["region_id"] for op in op_results}
    for rid in sorted(current):
        if rid not in touched:
            row = current[rid]
            regions.append({
                "id": rid,
                "name": row["name"],
                "valid_from": _epoch(row["valid_from"]),
                "valid_to": _epoch(row["valid_to"]),
                "polygon": geometry.polygon_from_wkt(row["wkt"]),
            })
    regions.sort(key=lambda r: r["id"])
    return regions


def apply_batch(cur, payload, trajectories: list[list[tuple[float, float, float]]]) -> dict:
    """在一个已开启的读/写事务中原子地：校验 revision → 校验操作 → 核验轨迹 → 落库。

    payload: RegionBatchRequest（多边形/时刻已由 Pydantic 校验归一化）。
    trajectories: 与 payload.trajectories 对应的已校验轨迹
                  [(epoch, x, y), ...]，时刻严格递增。
    成功返回 {"revision", "operations", "trajectory_results"}；
    失败抛 Batch* 异常，调用方负责回滚，整个批次不落库。
    """
    # 1) 锁全局元数据行：与所有单条登记/修订/删除互斥，写操作在此串行化
    cur.execute("SELECT revision FROM region_meta WHERE id = 1 FOR UPDATE")
    current_revision = cur.fetchone()["revision"]
    if current_revision != payload.expected_revision:
        raise BatchRevisionConflict(payload.expected_revision, current_revision)

    operations = payload.operations

    # 2) 同一现有区域在一批中只能出现一次（create 不占现有 id）
    seen: dict[int, int] = {}
    for i, op in enumerate(operations):
        if isinstance(op, (RegionUpdateInBatch, RegionDeleteInBatch)):
            rid = op.region_id
            if rid in seen:
                raise BatchDuplicateRegion(rid, [seen[rid], i])
            seen[rid] = i

    # 3) 按 id 升序锁定本批涉及的现有区域行（与所有单条写路径同一加锁顺序：
    #    先 meta 行、后区域行；区域行一律按 id 升序，杜绝多写事务死锁）
    referenced = sorted(seen)
    if referenced:
        cur.execute(
            "SELECT id FROM regions WHERE id = ANY(%s) ORDER BY id FOR UPDATE",
            (referenced,),
        )
        locked = {r["id"] for r in cur.fetchall()}
    else:
        locked = set()
    missing = [
        {"index": i, "region_id": rid}
        for rid, i in sorted(((rid, seen[rid]) for rid in referenced),
                             key=lambda kv: kv[1])
        if rid not in locked
    ]
    if missing:
        raise BatchRegionsNotFound(missing)

    # 4) 加载当前完整集合（行已锁、meta 行已锁，期间不存在任何并发写）
    current = _load_all_regions_locked(cur)

    # 5) 在内存中构造每条操作落库后的行（先不写库），并给 create 预分配真实 id
    op_results: list[dict] = []
    for i, op in enumerate(operations):
        if isinstance(op, RegionCreateInBatch):
            try:
                op.validate_time_order()
            except ValueError as exc:
                raise BatchInvalidOperation(i, str(exc), "valid_from") from exc
            cur.execute("SELECT nextval(pg_get_serial_sequence('regions', 'id')) AS id")
            new_id = int(cur.fetchone()["id"])
            polygon = op.polygon
            wkt = wkt_dumps(Polygon(polygon), rounding_precision=-1)
            op_results.append({
                "index": i,
                "action": "create",
                "region_id": new_id,
                "version": 1,
                "_row": {
                    "id": new_id,
                    "name": op.name,
                    "polygon": polygon,
                    "wkt": wkt,
                    "valid_from": op.valid_from,
                    "valid_to": op.valid_to,
                    "version": 1,
                },
            })
        elif isinstance(op, RegionUpdateInBatch):
            row = current[op.region_id]
            name = op.name if op.name is not None else row["name"]
            valid_from = op.valid_from if op.valid_from is not None else row["valid_from"]
            valid_to = op.valid_to if op.valid_to is not None else row["valid_to"]
            if valid_from >= valid_to:
                raise BatchInvalidOperation(
                    i,
                    "生效时段必须满足 valid_from < valid_to（左闭右开，起早于止）",
                    "valid_from",
                )
            if op.polygon is not None:
                polygon = op.polygon
                wkt = wkt_dumps(Polygon(polygon), rounding_precision=-1)
            else:
                polygon = row["polygon"]
                wkt = row["wkt"]
            new_version = int(row["version"]) + 1
            op_results.append({
                "index": i,
                "action": "update",
                "region_id": op.region_id,
                "version": new_version,
                "_row": {
                    "id": op.region_id,
                    "name": name,
                    "polygon": polygon,
                    "wkt": wkt,
                    "valid_from": valid_from,
                    "valid_to": valid_to,
                    "version": new_version,
                },
            })
        else:  # delete
            row = current[op.region_id]
            op_results.append({
                "index": i,
                "action": "delete",
                "region_id": op.region_id,
                "version": int(row["version"]),
                "_row": None,
            })

    # 6) 构造变更后完整集合，按现有直线匀速/边界接触即穿越/左闭右开规则核验
    candidate = _candidate_regions(current, op_results)
    all_checks: list[dict] = []
    unsafe: list[dict] = []
    for ti, traj in enumerate(trajectories):
        results = geometry.verify_trajectory(traj, candidate)
        safe = all(len(r["crossings"]) == 0 for r in results)
        check = {"index": ti, "safe": safe, "results": results}
        all_checks.append(check)
        if not safe:
            unsafe.append(check)
    if unsafe:
        # 不落库；region_meta 行锁随回滚释放
        raise BatchUnsafeTrajectories(all_checks)

    # 6.5) 所有「未取消且末点时刻未过」的凭证在变更后集合下必须仍安全；
    #      不安全抛 ActivePassConflict（409，携带冲突凭证 id 与穿越明细），整笔回滚
    check_active_passes_against(cur, candidate)

    # 7) 全部轨迹安全：按操作顺序落库
    for op in op_results:
        if op["action"] == "create":
            row = op["_row"]
            cur.execute(
                """
                INSERT INTO regions (id, name, valid_from, valid_to, polygon, wkt, version)
                VALUES (%(id)s, %(name)s, %(valid_from)s, %(valid_to)s,
                        %(polygon)s::jsonb, %(wkt)s, %(version)s)
                """,
                {
                    "id": row["id"],
                    "name": row["name"],
                    "valid_from": row["valid_from"],
                    "valid_to": row["valid_to"],
                    "polygon": json.dumps(row["polygon"]),
                    "wkt": row["wkt"],
                    "version": row["version"],
                },
            )
        elif op["action"] == "update":
            row = op["_row"]
            cur.execute(
                """
                UPDATE regions SET
                    name = %(name)s,
                    valid_from = %(valid_from)s,
                    valid_to = %(valid_to)s,
                    polygon = %(polygon)s::jsonb,
                    wkt = %(wkt)s,
                    version = %(version)s,
                    updated_at = now()
                WHERE id = %(id)s
                """,
                {
                    "id": row["id"],
                    "name": row["name"],
                    "valid_from": row["valid_from"],
                    "valid_to": row["valid_to"],
                    "polygon": json.dumps(row["polygon"]),
                    "wkt": row["wkt"],
                    "version": row["version"],
                },
            )
        else:  # delete
            cur.execute("DELETE FROM regions WHERE id = %s", (op["region_id"],))

    # 8) 整批全局 revision 只递增一次
    new_revision = bump_revision(cur)

    public_ops = [
        {k: v for k, v in op.items() if not k.startswith("_")}
        for op in op_results
    ]
    return {
        "revision": new_revision,
        "operations": public_ops,
        "trajectory_results": all_checks,
    }


def utcnow() -> datetime:
    return datetime.now(timezone.utc)
