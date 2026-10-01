"""可取消通行凭证 API 集成测试，需要可连接的 PostgreSQL（DATABASE_URL）。

运行：
    DATABASE_URL=postgresql://restrict@localhost:5432/restrict \
        python3 tests/test_passes_api.py
脚本会清空 regions/passes/region_meta 后跑全套用例（含并发串行化）。
"""
from __future__ import annotations

import os
import sys
import threading
import time
from datetime import datetime, timedelta, timezone

from fastapi.testclient import TestClient

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app import db, geometry, repository  # noqa: E402
from app.main import app  # noqa: E402

failures: list[str] = []


def check(label, cond, detail=""):
    print(f"[{'PASS' if cond else 'FAIL'}] {label}{(' — ' + detail) if detail and not cond else ''}")
    if not cond:
        failures.append(label)


db.init_pool()
client = TestClient(app)

# 清空数据
with db.pool.connection() as conn:
    with conn.cursor() as cur:
        cur.execute("TRUNCATE regions RESTART IDENTITY CASCADE")
        cur.execute("TRUNCATE passes RESTART IDENTITY")
        cur.execute("UPDATE region_meta SET revision = 0 WHERE id = 1")
    conn.commit()

# 统一使用「远未来」绝对时刻，避免用例受运行时刻影响
FUTURE = "2026-11-15T00:00:00Z"
FUTURE2 = "2026-11-16T00:00:00Z"

# 区域 A：[0,100]×[0,100]，11 月整月生效
r = client.post("/api/regions", json={
    "name": "A区",
    "polygon": [[0, 0], [100, 0], [100, 100], [0, 100]],
    "valid_from": "2026-11-01T00:00:00Z",
    "valid_to": "2026-12-01T00:00:00Z",
})
check("登记 A 区 201 revision=1", r.status_code == 201 and r.json()["revision"] == 1, r.text)

# 轨迹素材：
# 安全轨迹（y=200 从区域上方经过）
SAFE_TRAJ = [
    {"x": -50, "y": 200, "t": FUTURE},
    {"x": 150, "y": 200, "t": "2026-11-15T00:01:00Z"},
]
# 穿越轨迹（y=50，15s 进、45s 离开）
CROSS_TRAJ = [
    {"x": -50, "y": 50, "t": FUTURE},
    {"x": 150, "y": 50, "t": "2026-11-15T00:01:00Z"},
]

# ================================================================ 签发：安全
r = client.post("/api/passes", json={"points": SAFE_TRAJ})
check("安全轨迹签发 201", r.status_code == 201, r.text)
p = r.json()
pid_safe = p["id"]
check("返回凭证 id", isinstance(p["id"], int) and p["id"] >= 1)
check("状态 active", p["status"] == "active", str(p))
check("返回签发时 region_revision=1", p["region_revision"] == 1, str(p))
check("回显轨迹（2 点、UTC Z）",
      p["trajectory"] == [
          {"x": -50.0, "y": 200.0, "t": FUTURE},
          {"x": 150.0, "y": 200.0, "t": "2026-11-15T00:01:00Z"}],
      str(p["trajectory"]))
check("首末点时刻回显", p["first_time"] == FUTURE
      and p["last_time"] == "2026-11-15T00:01:00Z")
check("created_at 有值、cancelled_at 为空",
      p["created_at"] is not None and p["cancelled_at"] is None)
check("签发不推进区域 revision",
      client.get("/api/revision").json()["revision"] == 1)

# 时区归一化：+08:00 提交等价于同一 UTC 轨迹
r = client.post("/api/passes", json={"points": [
    {"x": -50, "y": 200, "t": "2026-11-15T08:00:00+08:00"},
    {"x": 150, "y": 200, "t": "2026-11-15T08:01:00+08:00"},
]})
check("带 +08:00 时区签发并归一化为 UTC",
      r.status_code == 201
      and r.json()["trajectory"][0]["t"] == FUTURE, r.text)
pid_tz = r.json()["id"]

# ================================================================ 签发：不安全
r = client.post("/api/passes", json={"points": CROSS_TRAJ})
check("穿越轨迹签发 422 unsafe_trajectory",
      r.status_code == 422 and r.json()["error"]["code"] == "unsafe_trajectory", r.text)
det = r.json()["error"]["details"]
check("不安全明细为逐区域穿越列表",
      [x["region_id"] for x in det] == [1] and len(det[0]["crossings"]) == 1, str(det))
cr = det[0]["crossings"][0]
check("插值穿越 00:00:15 → 00:00:45",
      cr["entry_time"] == "2026-11-15T00:00:15Z"
      and cr["exit_time"] == "2026-11-15T00:00:45Z"
      and cr["point_contact"] is False, str(cr))

# 不安全不保存：新凭证 id 不增长（下一张签发 id 仍是下一个序列值）
rev_before = client.get("/api/revision").json()["revision"]

# ================================================================ 签发：非法 / 已开始
r = client.post("/api/passes", json={"points": [
    {"x": -50, "y": 200, "t": "2026-11-15T00:01:00Z"},
    {"x": 150, "y": 200, "t": "2026-11-15T00:00:00Z"},
]})
check("逆序时刻 422 invalid_trajectory",
      r.status_code == 422 and r.json()["error"]["code"] == "invalid_trajectory", r.text)

r = client.post("/api/passes", json={"points": [
    {"x": -50, "y": 200, "t": FUTURE},
    {"x": 150, "y": 200, "t": FUTURE},
]})
check("重复时刻 422 invalid_trajectory",
      r.status_code == 422 and r.json()["error"]["code"] == "invalid_trajectory", r.text)

r = client.post("/api/passes", json={"points": [
    {"x": -50, "y": 200, "t": FUTURE},
]})
check("单点轨迹 422", r.status_code == 422, r.text)

r = client.post("/api/passes", json={"points": [
    {"x": -50, "y": 200, "t": "2026-11-15T00:00:00"},
    {"x": 150, "y": 200, "t": "2026-11-15T00:01:00Z"},
]})
check("无时区时刻 422", r.status_code == 422, r.text)

r = client.post("/api/passes", json={"points": SAFE_TRAJ, "extra": 1})
check("多余字段 422", r.status_code == 422, r.text)

# 首点不晚于签发时刻（当前时间是 2026-10，远未来 11 月才合法）
r = client.post("/api/passes", json={"points": [
    {"x": -50, "y": 200, "t": "2020-01-01T00:00:00Z"},
    {"x": 150, "y": 200, "t": "2020-01-01T00:01:00Z"},
]})
check("首点已过 422 trajectory_already_started",
      r.status_code == 422
      and r.json()["error"]["code"] == "trajectory_already_started", r.text)

# 首点恰为「此刻」也必须拒绝（严格晚于）
now_iso = datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")
later_iso = (datetime.now(timezone.utc) + timedelta(hours=1)).isoformat().replace("+00:00", "Z")
r = client.post("/api/passes", json={"points": [
    {"x": 1000.0, "y": 1000.0, "t": now_iso},
    {"x": 1100.0, "y": 1000.0, "t": later_iso},
]})
check("首点恰为当前时刻 422 trajectory_already_started",
      r.status_code == 422
      and r.json()["error"]["code"] == "trajectory_already_started", r.text)

# 已开始/不安全/非法均不保存：已成功 2 张（safe + tz），再签发安全轨迹 id 应连续
r = client.post("/api/passes", json={"points": [
    {"x": -50, "y": 200, "t": FUTURE2},
    {"x": 150, "y": 200, "t": "2026-11-16T00:01:00Z"},
]})
check("拒绝后仍可正常签发", r.status_code == 201, r.text)
pid_watch = r.json()["id"]
check("被拒绝的签发未占用凭证 id",
      pid_watch == pid_tz + 1, f"{pid_watch} != {pid_tz + 1}")
check("所有拒绝均未推进 revision",
      client.get("/api/revision").json()["revision"] == rev_before == 1)

# ================================================================ 查询
r = client.get(f"/api/passes/{pid_safe}")
check("GET 凭证 200 active", r.status_code == 200 and r.json()["status"] == "active", r.text)
check("GET 未知凭证 404 not_found",
      client.get("/api/passes/99999").status_code == 404
      and client.get("/api/passes/99999").json()["error"]["code"] == "not_found")

# 末点已过的凭证状态为 expired（历史数据场景：直接构造一张过期凭证）
from psycopg.rows import dict_row

with db.pool.connection() as conn:
    with conn.cursor() as cur:
        cur.row_factory = dict_row
        cur.execute(
            """
            INSERT INTO passes (trajectory, region_revision, first_time, last_time)
            VALUES (%s::jsonb, 1, %s, %s)
            RETURNING id
            """,
            ('[{"x": 1, "y": 1, "t": "2020-01-01T00:00:00Z"},'
             ' {"x": 2, "y": 1, "t": "2020-01-01T00:01:00Z"}]',
             datetime(2020, 1, 1, tzinfo=timezone.utc),
             datetime(2020, 1, 1, 0, 1, tzinfo=timezone.utc)),
        )
        expired_id = cur.fetchone()["id"]
    conn.commit()
r = client.get(f"/api/passes/{expired_id}")
check("末点已过的凭证状态 expired",
      r.status_code == 200 and r.json()["status"] == "expired", r.text)

# ================================================================ 取消
r = client.post(f"/api/passes/{pid_safe}/cancel")
check("取消 200 cancelled", r.status_code == 200 and r.json()["status"] == "cancelled", r.text)
check("取消回显 cancelled_at", r.json()["cancelled_at"] is not None)
first_cancelled_at = r.json()["cancelled_at"]

r = client.post(f"/api/passes/{pid_safe}/cancel")
check("重复取消 200 且状态不变", r.status_code == 200 and r.json()["status"] == "cancelled")
check("重复取消不改变 cancelled_at",
      r.json()["cancelled_at"] == first_cancelled_at, str(r.json()))

r = client.get(f"/api/passes/{pid_safe}")
check("GET 已取消凭证为 cancelled", r.json()["status"] == "cancelled")

r = client.post("/api/passes/99999/cancel")
check("取消未知凭证 404 not_found",
      r.status_code == 404 and r.json()["error"]["code"] == "not_found", r.text)

# ================================================================ 凭证约束区域变更
# 当前生效凭证：
#   pid_tz    : 11-15 08:00+08 == 00:00Z, y=200 安全经过
#   pid_watch : 11-16 y=200 安全经过
#   expired   : 2020 年，已过期，不约束
#   pid_safe  : 已取消，不约束

# (1) 单区 PATCH：把 A 区向北扩到 y=300 → y=200 轨迹被穿越 => 409
r = client.patch("/api/regions/1", json={
    "polygon": [[0, 0], [100, 0], [100, 300], [0, 300]],
})
check("扩大区域使生效凭证不安全：409 pass_conflict",
      r.status_code == 409 and r.json()["error"]["code"] == "pass_conflict", r.text)
conflict_ids = {d["pass_id"] for d in r.json()["error"]["details"]}
check("冲突明细列出两张未取消未过期凭证",
      conflict_ids == {pid_tz, pid_watch}, str(conflict_ids))
c0 = next(d for d in r.json()["error"]["details"] if d["pass_id"] == pid_tz)
check("冲突明细含穿越起止时刻",
      c0["results"][0]["region_id"] == 1
      and c0["results"][0]["crossings"][0]["entry_time"]
      == "2026-11-15T00:00:15Z"
      and c0["results"][0]["crossings"][0]["exit_time"]
      == "2026-11-15T00:00:45Z", str(c0))
check("409 整笔回滚：revision 不变",
      client.get("/api/revision").json()["revision"] == 1)
check("409 整笔回滚：区域形态未变",
      client.get("/api/regions/1").json()["polygon"][2] == [100.0, 100.0])
check("409 整笔回滚：version 不变", client.get("/api/regions/1").json()["version"] == 1)

# (2) 不改变几何/生效期语义的改名允许提交
r = client.patch("/api/regions/1", json={"name": "A区-改名"})
check("不影响凭证安全的改名 200", r.status_code == 200 and r.json()["revision"] == 2, r.text)

# (3) PATCH 生效期到凭证时间窗之外（缩短到 11-10 前结束）：凭证时刻区域不生效 => 安全
r = client.patch("/api/regions/1", json={"valid_to": "2026-11-10T00:00:00Z"})
check("缩短生效期使凭证时刻无区域：200", r.status_code == 200, r.text)

# 恢复 11 月窗口
r = client.patch("/api/regions/1", json={"valid_to": "2026-12-01T00:00:00Z"})
check("恢复生效期 200 revision=4", r.status_code == 200 and r.json()["revision"] == 4, r.text)

# (4) 新建挡路区域 => 409，且新区域不落库（序列预取不影响数据行）
r = client.post("/api/regions", json={
    "name": "B区-挡路",
    "polygon": [[0, 150], [100, 150], [100, 250], [0, 250]],
    "valid_from": "2026-11-01T00:00:00Z",
    "valid_to": "2026-12-01T00:00:00Z",
})
check("新建挡路区域 409 pass_conflict",
      r.status_code == 409 and r.json()["error"]["code"] == "pass_conflict", r.text)
check("新建被回滚：仍只有 1 个区域", len(client.get("/api/regions").json()) == 1)
check("新建被回滚：revision 不变", client.get("/api/revision").json()["revision"] == 4)

# (5) 删除区域不会引入不安全：允许
r = client.post("/api/regions", json={
    "name": "临时区",
    "polygon": [[1000, 1000], [1010, 1000], [1010, 1010], [1000, 1010]],
    "valid_from": "2026-11-01T00:00:00Z",
    "valid_to": "2026-12-01T00:00:00Z",
})
tmp_id = r.json()["region"]["id"]
r = client.delete(f"/api/regions/{tmp_id}")
check("删除区域始终允许（只移除约束）", r.status_code == 200, r.text)

# (6) 已取消凭证不约束：取消 pid_tz 后，向北扩大只威胁 pid_watch（11-16），
#     把区域扩到 y=300 仍 409，但冲突列表只剩 pid_watch
client.post(f"/api/passes/{pid_tz}/cancel")
r = client.patch("/api/regions/1", json={
    "polygon": [[0, 0], [100, 0], [100, 300], [0, 300]],
})
check("取消一张后冲突只剩另一张",
      r.status_code == 409
      and {d["pass_id"] for d in r.json()["error"]["details"]} == {pid_watch}, r.text)

# (7) 全部相关凭证取消后变更成功
client.post(f"/api/passes/{pid_watch}/cancel")
r = client.patch("/api/regions/1", json={
    "polygon": [[0, 0], [100, 0], [100, 300], [0, 300]],
})
check("冲突凭证均取消后扩大成功", r.status_code == 200, r.text)
rev_after = r.json()["revision"]

# (8) 过期凭证从不约束：A 现已覆盖 y=200，给过期凭证任意改动都应成功
r = client.patch("/api/regions/1", json={"name": "A区-再改名"})
check("只剩过期凭证时改名成功", r.status_code == 200, r.text)

# ================================================================ 批量变更的凭证约束
# 恢复标准 A 区几何，登记一张新的生效凭证（安全线 y=200）
r = client.patch("/api/regions/1", json={
    "name": "A区", "polygon": [[0, 0], [100, 0], [100, 100], [0, 100]]})
base_rev = r.json()["revision"]
check("恢复 A 区几何", r.status_code == 200, r.text)

r = client.post("/api/passes", json={"points": [
    {"x": -50, "y": 200, "t": "2026-11-20T00:00:00Z"},
    {"x": 150, "y": 200, "t": "2026-11-20T00:01:00Z"},
]})
pid_batch = r.json()["id"]
check("批量约束用凭证签发成功", r.status_code == 201, r.text)

# 批次自带轨迹安全（y=500），但变更后集合威胁存量凭证 => 409 pass_conflict
r = client.post("/api/regions/batch", json={
    "expected_revision": base_rev,
    "operations": [{
        "action": "update", "id": 1,
        "polygon": [[0, 0], [100, 0], [100, 300], [0, 300]],
    }],
    "trajectories": [[
        {"x": -50, "y": 500, "t": "2026-11-21T00:00:00Z"},
        {"x": 150, "y": 500, "t": "2026-11-21T00:01:00Z"},
    ]],
})
check("批量：自带轨迹安全但存量凭证冲突 → 409 pass_conflict",
      r.status_code == 409 and r.json()["error"]["code"] == "pass_conflict", r.text)
check("批量 409 明细给出凭证 id",
      [d["pass_id"] for d in r.json()["error"]["details"]] == [pid_batch])
check("批量 409 整笔回滚 revision 不变",
      client.get("/api/revision").json()["revision"] == base_rev)
check("批量 409 区域形态未变",
      client.get("/api/regions/1").json()["polygon"][2] == [100.0, 100.0])

# revision 冲突优先于凭证冲突（revision 不匹配先返回 409 revision_conflict）
r = client.post("/api/regions/batch", json={
    "expected_revision": 99999,
    "operations": [{
        "action": "update", "id": 1,
        "polygon": [[0, 0], [100, 0], [100, 300], [0, 300]],
    }],
    "trajectories": [[
        {"x": -50, "y": 500, "t": "2026-11-21T00:00:00Z"},
        {"x": 150, "y": 500, "t": "2026-11-21T00:01:00Z"},
    ]],
})
check("批量：revision 冲突优先返回 revision_conflict",
      r.status_code == 409 and r.json()["error"]["code"] == "revision_conflict", r.text)

# 凭证取消后同一批次成功
client.post(f"/api/passes/{pid_batch}/cancel")
r = client.post("/api/regions/batch", json={
    "expected_revision": base_rev,
    "operations": [{
        "action": "update", "id": 1,
        "polygon": [[0, 0], [100, 0], [100, 300], [0, 300]],
    }],
    "trajectories": [[
        {"x": -50, "y": 500, "t": "2026-11-21T00:00:00Z"},
        {"x": 150, "y": 500, "t": "2026-11-21T00:01:00Z"},
    ]],
})
check("凭证取消后批量成功", r.status_code == 200, r.text)
check("批量成功 revision +1", r.json()["revision"] == base_rev + 1)

# ================================================================ 并发串行化
# 场景：一张生效凭证走 y=200；并发发起 1 个「扩大区域到 y=300」的 PATCH
# 与 1 个凭证取消。二者经同一把 meta 行锁串行化：
#   若 PATCH 先提交 → 必须 409；之后取消成功；
#   若取消先提交 → PATCH 必须 200。
# 任一顺序都不允许出现「PATCH 200 但凭证仍 active 且轨迹已不安全」的漏检。

# 重新准备：恢复 A 区 [0,100]，签发 y=200 的凭证
client.patch("/api/regions/1", json={
    "polygon": [[0, 0], [100, 0], [100, 100], [0, 100]]})
r = client.post("/api/passes", json={"points": [
    {"x": -50, "y": 200, "t": "2026-11-25T00:00:00Z"},
    {"x": 150, "y": 200, "t": "2026-11-25T00:01:00Z"},
]})
pid_race = r.json()["id"]
rev_before_race = client.get("/api/revision").json()["revision"]

# 让 PATCH 事务在「持有 meta 行锁、凭证核验之前」等待，制造确定性交错
orig_load = repository.load_active_passes
gate = threading.Event()
started = threading.Event()


def slow_load(cur):
    started.set()
    gate.wait(5)
    return orig_load(cur)


repository.load_active_passes = slow_load
outcomes: dict = {}
try:
    def do_patch():
        outcomes["patch"] = client.patch("/api/regions/1", json={
            "polygon": [[0, 0], [100, 0], [100, 300], [0, 300]]})

    th = threading.Thread(target=do_patch)
    th.start()
    started.wait(5)
    # PATCH 已持锁；此时取消必须阻塞等待
    cancel_result = {}

    def do_cancel():
        cancel_result["r"] = client.post(f"/api/passes/{pid_race}/cancel")

    thc = threading.Thread(target=do_cancel)
    thc.start()
    time.sleep(0.5)
    check("取消在 meta 行锁上等待（PATCH 持锁期间未完成）",
          "r" not in cancel_result)
    # 放行 PATCH：它在凭证仍 active 时完成核验，必须 409
    gate.set()
    th.join(timeout=10)
    thc.join(timeout=10)
finally:
    repository.load_active_passes = orig_load

pr = outcomes["patch"]
cr2 = cancel_result["r"]
check("持锁期间凭证仍 active：PATCH 409 pass_conflict",
      pr.status_code == 409 and pr.json()["error"]["code"] == "pass_conflict", pr.text)
check("等待中的取消随后成功",
      cr2.status_code == 200 and cr2.json()["status"] == "cancelled", cr2.text)
check("409 PATCH 未推进 revision",
      client.get("/api/revision").json()["revision"] == rev_before_race)
# 取消之后再发同样 PATCH：凭证已取消，必须成功
r = client.patch("/api/regions/1", json={
    "polygon": [[0, 0], [100, 0], [100, 300], [0, 300]]})
check("取消后 PATCH 成功", r.status_code == 200, r.text)

# 反向交错：取消先拿到锁并提交，后到的 PATCH 必须放行（无漏检/无误拒）
client.patch("/api/regions/1", json={
    "polygon": [[0, 0], [100, 0], [100, 100], [0, 100]]})
r = client.post("/api/passes", json={"points": [
    {"x": -50, "y": 200, "t": "2026-11-26T00:00:00Z"},
    {"x": 150, "y": 200, "t": "2026-11-26T00:01:00Z"},
]})
pid_race2 = r.json()["id"]
rc = client.post(f"/api/passes/{pid_race2}/cancel")
rp = client.patch("/api/regions/1", json={
    "polygon": [[0, 0], [100, 0], [100, 300], [0, 300]]})
check("取消先提交后 PATCH 200",
      rc.status_code == 200 and rp.status_code == 200, rp.text)

# 并发：同一 revision 的多个扩大 PATCH 互斥串行；至少凭证冲突时全部拒绝，
# 且绝不出现区域已扩大但凭证轨迹不安全的落库状态
client.patch("/api/regions/1", json={
    "polygon": [[0, 0], [100, 0], [100, 100], [0, 100]]})
r = client.post("/api/passes", json={"points": [
    {"x": -50, "y": 200, "t": "2026-11-27T00:00:00Z"},
    {"x": 150, "y": 200, "t": "2026-11-27T00:01:00Z"},
]})
pid_concurrent = r.json()["id"]
barrier = threading.Barrier(4)
patch_codes: list = []
plock = threading.Lock()


def concurrent_patch(_i):
    barrier.wait()
    rr = client.patch("/api/regions/1", json={
        "polygon": [[0, 0], [100, 0], [100, 300], [0, 300]]})
    with plock:
        patch_codes.append(rr.status_code)


threads = [threading.Thread(target=concurrent_patch, args=(i,)) for i in range(4)]
for t in threads:
    t.start()
for t in threads:
    t.join(timeout=30)
check("凭证 active 时并发扩大 PATCH 全部 409（无漏检）",
      patch_codes == [409] * 4, str(patch_codes))
check("全部拒绝后区域几何未变",
      client.get("/api/regions/1").json()["polygon"][2] == [100.0, 100.0])

# ================================================================ 既有规则沿用：边界接触 / 左闭右开
client.post(f"/api/passes/{pid_concurrent}/cancel")
# 擦角点 (100,100) 的轨迹；新区域生效窗口覆盖该时刻 => 签发不安全（边界即违规）
touch_traj = [
    {"x": 50, "y": 150, "t": "2026-11-28T00:00:00Z"},
    {"x": 150, "y": 50, "t": "2026-11-28T00:00:10Z"},
]
r = client.post("/api/passes", json={"points": touch_traj})
check("签发核验沿用边界接触即违规",
      r.status_code == 422 and r.json()["error"]["code"] == "unsafe_trajectory"
      and r.json()["error"]["details"][0]["crossings"][0]["point_contact"] is True,
      r.text)

# 同一接触时刻若所有区域均已失效（valid_to 右端开），则签发安全
r = client.post("/api/regions", json={
    "name": "点接触窗口区",
    "polygon": [[0, 0], [100, 0], [100, 100], [0, 100]],
    "valid_from": "2026-11-01T00:00:00Z",
    "valid_to": "2026-11-28T00:00:05Z",  # 接触恰在 valid_to：右端开，不参与
})
vt_region = r.json()["region"]["id"]
r = client.post("/api/passes", json={"points": touch_traj})
# A 区几何为 [0,100]×[0,100] 且 11 月生效 → 擦的是同一个角点，仍会撞 A 区。
# 先删掉 A 区，仅留窗口区，再验证右端开
client.delete("/api/regions/1")
r = client.post("/api/passes", json={"points": touch_traj})
check("接触恰在 valid_to（右端开）：凭证签发安全",
      r.status_code == 201, r.text)
pid_edge = r.json()["id"]
# 把窗口区扩为从接触时刻（左端闭）起生效到 12 月 => 凭证应被区域变更拦截
r = client.patch(f"/api/regions/{vt_region}", json={
    "valid_from": "2026-11-28T00:00:05Z",
    "valid_to": "2026-12-01T00:00:00Z",
})
check("左闭端点变更使已发凭证不安全：409 且点接触明细",
      r.status_code == 409 and r.json()["error"]["code"] == "pass_conflict"
      and r.json()["error"]["details"][0]["results"][0]["crossings"][0]
      ["point_contact"] is True, r.text)
check("端点变更 409 回滚",
      client.get(f"/api/regions/{vt_region}").json()["valid_from"]
      == "2026-11-01T00:00:00Z")

print()
if failures:
    print(f"{len(failures)} 个失败")
    sys.exit(1)
print("全部通行凭证 API 集成用例通过")
