"""FastAPI 入口：临时限行核验 HTTP API。"""
from __future__ import annotations

import logging
from contextlib import asynccontextmanager
from datetime import datetime, timezone

from fastapi import FastAPI, Path, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from psycopg.errors import CheckViolation
from pydantic import ValidationError
from starlette.exceptions import HTTPException as StarletteHTTPException

from . import db, geometry, repository
from .repository import (
    BatchDuplicateRegion,
    BatchInvalidOperation,
    BatchRegionsNotFound,
    BatchRevisionConflict,
    BatchUnsafeTrajectories,
    PassConflict,
    PassUnsafe,
)
from .schemas import (
    PassIssueRequest,
    PassOut,
    RegionBatchRequest,
    RegionBatchResponse,
    RegionCreate,
    RegionCreatedResponse,
    RegionOut,
    RegionResultOut,
    RegionUpdate,
    RevisionOut,
    ScheduleRequest,
    ScheduleResponse,
    VerifyRequest,
    VerifyResponse,
)

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
logger = logging.getLogger("restriction-service")


@asynccontextmanager
async def lifespan(app: FastAPI):
    db.init_pool()
    yield
    db.close_pool()


app = FastAPI(
    title="临时限行核验服务",
    version="1.3.0",
    description="登记平面多边形限行区域、对连续轨迹进行穿越核验，并计算固定行程的安全出发区间；支持多区域变更与轨迹核验一次原子提交；可为指定轨迹签发可取消的通行凭证，未取消且未结束的凭证约束后续区域变更。",
    lifespan=lifespan,
)


# ---------------------------------------------------------------- 错误处理

class DomainError(Exception):
    """业务语义错误（HTTP 422）。"""

    def __init__(self, message: str, loc: list[str] | None = None):
        super().__init__(message)
        self.message = message
        self.loc = loc or []


def _error_response(status: int, code: str, message: str, details=None) -> JSONResponse:
    return JSONResponse(
        status_code=status,
        content={
            "error": {
                "code": code,
                "message": message,
                "details": details or [],
            }
        },
    )


def _crossings_to_out(results: list[dict]) -> list[dict]:
    """把几何层结果（穿越时刻为 epoch 秒）转成 API 输出（UTC datetime）。"""
    out_results = []
    for r in results:
        crossings = []
        for c in r["crossings"]:
            crossings.append({
                "entry_time": datetime.fromtimestamp(c["entry_time"], tz=timezone.utc),
                "exit_time": datetime.fromtimestamp(c["exit_time"], tz=timezone.utc),
                "entry_inclusive": c["entry_inclusive"],
                "exit_inclusive": c["exit_inclusive"],
                "point_contact": c["point_contact"],
            })
        out_results.append({
            "region_id": r["region_id"],
            "region_name": r["region_name"],
            "crossings": crossings,
        })
    return out_results


def _pass_row_to_out(row: dict) -> dict:
    """把 passes 表行（trajectory 为 JSONB，t 为 epoch 秒）转成 API 输出。"""
    return {
        "id": row["id"],
        "status": row["status"],
        "revision": row["issued_revision"],
        "trajectory": [
            {
                "x": p["x"],
                "y": p["y"],
                "t": datetime.fromtimestamp(p["t"], tz=timezone.utc),
            }
            for p in row["trajectory"]
        ],
        "issued_at": row["created_at"],
        "cancelled_at": row["cancelled_at"],
    }


@app.exception_handler(ValidationError)
async def _on_pydantic_error(_request: Request, exc: ValidationError):
    details = []
    for err in exc.errors():
        details.append({
            "loc": [str(x) for x in err.get("loc", [])],
            "message": err.get("msg", ""),
            "type": err.get("type", ""),
        })
    return _error_response(422, "validation_error", "请求参数校验失败", details)


@app.exception_handler(RequestValidationError)
async def _on_request_validation_error(_request: Request, exc: RequestValidationError):
    details = []
    for err in exc.errors():
        details.append({
            "loc": [str(x) for x in err.get("loc", [])],
            "message": err.get("msg", ""),
            "type": err.get("type", ""),
        })
    return _error_response(422, "validation_error", "请求参数校验失败", details)


@app.exception_handler(StarletteHTTPException)
async def _on_http_exception(_request: Request, exc: StarletteHTTPException):
    # 404/405 等也使用统一错误信封；业务侧已把 {"error": {...}} 放在 detail 中
    if isinstance(exc.detail, dict) and "error" in exc.detail:
        body = exc.detail
    else:
        body = {"error": {"code": "http_error", "message": str(exc.detail), "details": []}}
    return JSONResponse(status_code=exc.status_code, content=body)


@app.exception_handler(DomainError)
async def _on_domain_error(_request: Request, exc: DomainError):
    details = [{"loc": exc.loc, "message": exc.message}] if exc.loc else []
    return _error_response(422, "invalid_input", exc.message, details)


@app.exception_handler(geometry.RegionError)
async def _on_region_error(_request: Request, exc: geometry.RegionError):
    return _error_response(422, "invalid_polygon", str(exc))


@app.exception_handler(geometry.TrajectoryError)
async def _on_trajectory_error(_request: Request, exc: geometry.TrajectoryError):
    return _error_response(422, "invalid_trajectory", str(exc))


@app.exception_handler(BatchRevisionConflict)
async def _on_batch_revision_conflict(_request: Request, exc: BatchRevisionConflict):
    return _error_response(
        409,
        "revision_conflict",
        str(exc),
        [{"loc": ["expected_revision"],
          "message": f"预期 revision={exc.expected}，当前 revision={exc.current}",
          "current_revision": exc.current}],
    )


@app.exception_handler(BatchRegionsNotFound)
async def _on_batch_regions_not_found(_request: Request, exc: BatchRegionsNotFound):
    return _error_response(
        404,
        "region_not_found",
        str(exc),
        [{"loc": ["operations", str(m["index"]), "id"],
          "message": f"区域 {m['region_id']} 不存在",
          "operation_index": m["index"],
          "region_id": m["region_id"]}
         for m in exc.missing],
    )


@app.exception_handler(BatchDuplicateRegion)
async def _on_batch_duplicate_region(_request: Request, exc: BatchDuplicateRegion):
    return _error_response(
        422,
        "duplicate_region",
        str(exc),
        [{"loc": ["operations"],
          "message": f"区域 {exc.region_id} 在同一批中出现了多次",
          "region_id": exc.region_id,
          "operation_indices": exc.indices}],
    )


@app.exception_handler(BatchInvalidOperation)
async def _on_batch_invalid_operation(_request: Request, exc: BatchInvalidOperation):
    loc = ["operations", str(exc.index)] + ([exc.field] if exc.field else [])
    return _error_response(
        422,
        "invalid_operation",
        exc.message,
        [{"loc": loc, "message": exc.message, "operation_index": exc.index}],
    )


@app.exception_handler(BatchUnsafeTrajectories)
async def _on_batch_unsafe(_request: Request, exc: BatchUnsafeTrajectories):
    details = []
    for t in exc.trajectory_results:
        if t["safe"]:
            continue
        # 只回显有穿越的区域，突出违规明细；经响应模型序列化（UTC Z 时刻）
        violated = [r for r in t["results"] if r["crossings"]]
        dumped = [
            RegionResultOut.model_validate(r).model_dump(mode="json")
            for r in _crossings_to_out(violated)
        ]
        details.append({
            "trajectory_index": t["index"],
            "results": dumped,
        })
    return _error_response(
        422,
        "unsafe_trajectory",
        "存在轨迹在变更后区域集合下发生穿越，整批变更已取消且不落库",
        details,
    )


@app.exception_handler(PassUnsafe)
async def _on_pass_unsafe(_request: Request, exc: PassUnsafe):
    # 只回显有穿越的区域；经响应模型序列化（UTC Z 时刻）
    violated = [r for r in exc.results if r["crossings"]]
    details = [
        RegionResultOut.model_validate(r).model_dump(mode="json")
        for r in _crossings_to_out(violated)
    ]
    return _error_response(
        422,
        "unsafe_trajectory",
        "轨迹在当前区域集合下发生穿越，凭证未签发且不落库",
        details,
    )


@app.exception_handler(PassConflict)
async def _on_pass_conflict(_request: Request, exc: PassConflict):
    return _error_response(
        409,
        "pass_conflict",
        str(exc),
        [{"pass_id": pid,
          "message": f"凭证 {pid}（未取消且末点时刻未过）的轨迹在变更后区域集合下不再安全"}
         for pid in exc.pass_ids],
    )


# ---------------------------------------------------------------- 区域管理

@app.post("/api/regions", response_model=RegionCreatedResponse, status_code=201,
          tags=["regions"])
def create_region(payload: RegionCreate):
    """登记一个限行区域；非法多边形或不递增时段返回 422 且不保存。"""
    try:
        payload.validate_time_order()
    except ValueError as exc:
        raise DomainError(str(exc), ["valid_from", "valid_to"]) from exc
    with db.pool.connection() as conn:
        with conn.cursor() as cur:
            repository.configure_cursor(cur)
            try:
                region, revision = repository.create_region(cur, payload, payload.polygon)
            except CheckViolation as exc:
                raise DomainError("生效时段必须满足 valid_from < valid_to（左闭右开）") from exc
        conn.commit()
    return RegionCreatedResponse(region=RegionOut.model_validate(region), revision=revision)


@app.get("/api/regions", response_model=list[RegionOut], tags=["regions"])
def list_regions():
    with db.pool.connection() as conn:
        with conn.cursor() as cur:
            repository.configure_cursor(cur)
            rows = repository.list_regions(cur)
    return [RegionOut.model_validate(r) for r in rows]


@app.get("/api/regions/{region_id}", response_model=RegionOut, tags=["regions"])
def get_region(region_id: int = Path(ge=1)):
    with db.pool.connection() as conn:
        with conn.cursor() as cur:
            repository.configure_cursor(cur)
            cur.execute(
                """SELECT id, name, polygon, valid_from, valid_to, version,
                          created_at, updated_at
                   FROM regions WHERE id = %s""",
                (region_id,),
            )
            row = cur.fetchone()
    if row is None:
        raise StarletteHTTPException(status_code=404, detail={"error": {"code": "not_found",
                                                               "message": f"区域 {region_id} 不存在"}})
    return RegionOut.model_validate(row)


@app.patch("/api/regions/{region_id}", response_model=RegionCreatedResponse, tags=["regions"])
def update_region(payload: RegionUpdate, region_id: int = Path(ge=1)):
    """修订区域（部分更新）；每次修订 version 加 1 并推进全局 revision。"""
    with db.pool.connection() as conn:
        with conn.cursor() as cur:
            repository.configure_cursor(cur)
            try:
                result = repository.update_region(
                    cur, region_id, payload, payload.polygon
                )
            except ValueError as exc:
                raise DomainError(str(exc), ["valid_from", "valid_to"]) from exc
            except CheckViolation as exc:
                raise DomainError("生效时段必须满足 valid_from < valid_to（左闭右开）") from exc
            if result is None:
                raise StarletteHTTPException(status_code=404, detail={"error": {"code": "not_found",
                                                                       "message": f"区域 {region_id} 不存在"}})
            region, revision = result
        conn.commit()
    return RegionCreatedResponse(region=RegionOut.model_validate(region), revision=revision)


@app.delete("/api/regions/{region_id}", response_model=RevisionOut, tags=["regions"])
def remove_region(region_id: int = Path(ge=1)):
    with db.pool.connection() as conn:
        with conn.cursor() as cur:
            repository.configure_cursor(cur)
            deleted, revision = repository.delete_region(cur, region_id)
            if not deleted:
                raise StarletteHTTPException(status_code=404, detail={"error": {"code": "not_found",
                                                                       "message": f"区域 {region_id} 不存在"}})
        conn.commit()
    return RevisionOut(revision=revision)


@app.get("/api/revision", response_model=RevisionOut, tags=["regions"])
def current_revision():
    """当前区域集合版本号；每次登记/修订/删除后单调递增。"""
    with db.pool.connection() as conn:
        with conn.cursor() as cur:
            repository.configure_cursor(cur)
            revision = repository.get_revision_shared(cur)
    return RevisionOut(revision=revision)


@app.post("/api/regions/batch", response_model=RegionBatchResponse, status_code=200,
          responses={
              409: {"description": "expected_revision 已过期（revision_conflict）"},
              404: {"description": "批内引用了不存在的区域（region_not_found）"},
          },
          tags=["regions"])
def batch_change_regions(payload: RegionBatchRequest):
    """把多处区域登记/修订/删除作为**一次原子变更**提交，并保证指定轨迹在
    变更后的完整区域集合下仍然安全。

    处理顺序（全部在一个读/写事务内，行锁按「meta 行 → 区域行（id 升序）」获取）：

    1. 锁定全局 `region_meta` 行并比对 `expected_revision`；不符返回
       409 `revision_conflict`（携带当前 revision），整批不落库。
    2. 同一现有区域在一批中只能出现一次（update/delete 共用 id 维度）；
       重复返回 422 `duplicate_region`。
    3. 按 id 升序锁定涉及的现有区域；任一不存在返回 404 `region_not_found`
       （列出操作下标与区域 id），整批不落库。
    4. 单条操作数据非法（如修订后 `valid_from >= valid_to`）返回
       422 `invalid_operation`（带操作下标）；多边形/时区等结构问题在请求
       校验阶段返回 422 `validation_error`/`invalid_polygon`。
    5. 在内存中构造**变更后完整区域集合**（未涉及区域原样保留），对每条
       带时区绝对时刻轨迹按现有规则（直线匀速、边界接触即穿越、生效时段
       左闭右开）核验；任一轨迹不安全返回 422 `unsafe_trajectory`，明细
       列出对应轨迹下标与穿越区间，整批不落库。
    6. 未取消且末点时刻未过的通行凭证，其轨迹在变更后完整集合下仍须安全；
       任一冲突返回 409 `pass_conflict`（details 列出冲突凭证 ID），
       整批回滚，version/revision 不变。
    7. 仅当 revision 匹配、操作均合法、全部轨迹与全部约束凭证安全时，
       一次性落库：修订沿用 version +1，整批全局 revision 只递增一次。

    并发区域变更（含单个登记/修订/删除）与凭证签发/取消因同一把 meta 行锁
    在此串行化，无法绕过 revision 比对、轨迹核验与凭证约束检查。
    """
    # 轨迹合法性（点数、有限坐标、时刻严格递增）在几何层统一报错
    trajectories = [
        geometry.validate_trajectory(points, lambda p: p.t)
        for points in payload.trajectories
    ]

    with db.pool.connection() as conn:
        conn.rollback()  # 确保从连接池拿到的连接没有残留事务
        with conn.cursor() as cur:
            repository.configure_cursor(cur)
            try:
                result = repository.apply_batch(cur, payload, trajectories)
            except Exception:
                conn.rollback()
                raise
            else:
                conn.commit()

    out_trajectories = []
    for check in result["trajectory_results"]:
        out_trajectories.append({
            "index": check["index"],
            "safe": check["safe"],
            "results": _crossings_to_out(check["results"]),
        })

    return RegionBatchResponse(
        revision=result["revision"],
        operations=result["operations"],
        trajectories=out_trajectories,
    )


# ---------------------------------------------------------------- 通行凭证

@app.post("/api/passes", response_model=PassOut, status_code=201,
          responses={
              422: {"description": "轨迹非法、已开始或在当前区域集合下不安全"},
          },
          tags=["passes"])
def issue_pass(payload: PassIssueRequest):
    """为指定通行轨迹签发可取消的通行凭证。

    轨迹要求与 `/api/verify` 相同（至少 2 点、带时区绝对时刻、时刻严格递增、
    相邻点直线匀速），且**首点时刻必须晚于签发时刻**（以服务端拿到写锁后的
    当前时间为准）。签发在持有全局元数据行锁的写事务内完成：与取消及所有
    区域写入串行化，按当前完整区域集合核验（边界接触即违规、生效期
    左闭右开）。

    * 安全：持久化并返回 201，携带凭证 ID、签发时的区域 `revision` 与轨迹；
    * 不安全：422 `unsafe_trajectory`，details 列出穿越区域与起止明细，不落库；
    * 非法或已开始：422 `invalid_trajectory`/`validation_error`，不落库。
    """
    # 时刻严格递增等结构校验在几何层统一报错；先快速失败避免无谓的数据库读取
    traj = geometry.validate_trajectory(payload.points, lambda p: p.t)

    with db.pool.connection() as conn:
        conn.rollback()  # 确保从连接池拿到的连接没有残留事务
        with conn.cursor() as cur:
            repository.configure_cursor(cur)
            try:
                row = repository.issue_pass(cur, traj)
            except Exception:
                conn.rollback()
                raise
            else:
                conn.commit()
    return _pass_row_to_out(row)


@app.get("/api/passes/{pass_id}", response_model=PassOut, tags=["passes"])
def get_pass(pass_id: int = Path(ge=1)):
    """按 ID 查询凭证（含状态）；未知 ID 返回 404。"""
    with db.pool.connection() as conn:
        with conn.cursor() as cur:
            repository.configure_cursor(cur)
            row = repository.get_pass(cur, pass_id)
    if row is None:
        raise StarletteHTTPException(status_code=404, detail={"error": {"code": "not_found",
                                                               "message": f"凭证 {pass_id} 不存在"}})
    return _pass_row_to_out(row)


@app.post("/api/passes/{pass_id}/cancel", response_model=PassOut, tags=["passes"])
def cancel_pass(pass_id: int = Path(ge=1)):
    """取消凭证并返回状态；重复取消幂等（结果不变），未知 ID 返回 404。

    取消与签发、所有区域写入在同一把全局元数据行锁上串行化。
    """
    with db.pool.connection() as conn:
        conn.rollback()  # 确保从连接池拿到的连接没有残留事务
        with conn.cursor() as cur:
            repository.configure_cursor(cur)
            try:
                row = repository.cancel_pass(cur, pass_id)
            except Exception:
                conn.rollback()
                raise
            else:
                if row is None:
                    conn.rollback()
                else:
                    conn.commit()
    if row is None:
        raise StarletteHTTPException(status_code=404, detail={"error": {"code": "not_found",
                                                               "message": f"凭证 {pass_id} 不存在"}})
    return _pass_row_to_out(row)


# ---------------------------------------------------------------- 轨迹核验

@app.post("/api/verify", response_model=VerifyResponse, tags=["verification"])
def verify(payload: VerifyRequest):
    """对整段连续轨迹核验。

    整个核验在一个 REPEATABLE READ 只读事务中完成，因此并发修改区域时，
    本次核验使用的是同一已提交版本的区域集合（响应中带 revision）。
    """
    # 时刻严格递增在几何层统一报错；先快速失败避免无谓的数据库读取
    traj = geometry.validate_trajectory(payload.points, lambda p: p.t)

    with db.pool.connection() as conn:
        # REPEATABLE READ 只读事务：事务内所有读取共享同一快照，
        # 即使核验期间有区域被并发提交修改，本请求仍使用同一已提交版本的集合
        conn.rollback()  # 确保从连接池拿到的连接没有残留事务
        with conn.cursor() as cur:
            cur.execute("BEGIN ISOLATION LEVEL REPEATABLE READ READ ONLY")
            repository.configure_cursor(cur)
            try:
                revision, regions = repository.load_snapshot(cur)
                # 几何计算在事务内完成：快照一直持有到本次核验结束
                results = geometry.verify_trajectory(traj, regions)
            except Exception:
                conn.rollback()
                raise
            else:
                conn.commit()

    checked_at = datetime.now(timezone.utc)
    safe = all(len(r["crossings"]) == 0 for r in results)

    return VerifyResponse(
        safe=safe,
        revision=revision,
        checked_at=checked_at,
        results=_crossings_to_out(results),
    )


@app.post("/api/schedule", response_model=ScheduleResponse, tags=["scheduling"])
def schedule(payload: ScheduleRequest):
    """计算固定行程在候选出发时段内的全部安全出发区间。

    轨迹点用相对首点的秒数给出（首点必须为 0、其余严格递增）；候选时段
    [window_start, window_end) 带时区，服务端归一化到 UTC。计算为精确的
    区间运算（不做离散抽样）：对每个区域、每段几何接触 [a,b]（闭）与生效
    窗口 [vf, vt)（左闭右开），其禁止出发时刻恰为半开区间 [vf-b, vt-a)；
    全部禁止区间取并集后从候选窗口中扣除。

    整个计算在一个 REPEATABLE READ 只读事务中完成，与 /api/verify 一样，
    并发修改区域时本请求使用同一已提交快照（响应中带 revision）。
    """
    traj = geometry.validate_relative_trajectory(
        payload.points, lambda p: (p.x, p.y), lambda p: p.t
    )
    if payload.window_start >= payload.window_end:
        raise DomainError(
            "候选时段必须满足 window_start < window_end（左闭右开，起早于止）",
            ["window_start", "window_end"],
        )

    window_start = payload.window_start.timestamp()
    window_end = payload.window_end.timestamp()

    with db.pool.connection() as conn:
        conn.rollback()  # 确保从连接池拿到的连接没有残留事务
        with conn.cursor() as cur:
            cur.execute("BEGIN ISOLATION LEVEL REPEATABLE READ READ ONLY")
            repository.configure_cursor(cur)
            try:
                revision, regions = repository.load_snapshot(cur)
                # 几何计算在事务内完成：快照一直持有到本次计算结束
                intervals = geometry.safe_departure_intervals(
                    traj, regions, window_start, window_end
                )
            except Exception:
                conn.rollback()
                raise
            else:
                conn.commit()

    out_intervals = [
        {
            "start": datetime.fromtimestamp(iv["start"], tz=timezone.utc),
            "end": datetime.fromtimestamp(iv["end"], tz=timezone.utc),
            "start_inclusive": iv["start_inclusive"],
            "end_inclusive": iv["end_inclusive"],
        }
        for iv in intervals
    ]

    return ScheduleResponse(
        revision=revision,
        window_start=payload.window_start,
        window_end=payload.window_end,
        intervals=out_intervals,
    )


@app.get("/health", tags=["meta"])
def health():
    with db.pool.connection() as conn:
        with conn.cursor() as cur:
            cur.execute("SELECT 1")
            cur.fetchone()
    return {"status": "ok"}


@app.get("/", tags=["meta"])
def root():
    return {
        "service": "临时限行核验服务",
        "docs": "/docs",
        "openapi": "/openapi.json",
        "endpoints": [
            "POST /api/regions", "GET /api/regions", "GET /api/regions/{id}",
            "PATCH /api/regions/{id}", "DELETE /api/regions/{id}",
            "POST /api/regions/batch",
            "GET /api/revision", "POST /api/verify", "POST /api/schedule",
            "POST /api/passes", "GET /api/passes/{id}",
            "POST /api/passes/{id}/cancel",
            "GET /health",
        ],
    }
