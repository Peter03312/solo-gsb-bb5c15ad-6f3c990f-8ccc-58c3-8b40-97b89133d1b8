"""PostgreSQL 连接池与初始化。

使用 psycopg3 的连接池；应用启动时自动建表并初始化全局版本号。
容器编排中数据库可能晚于应用启动，因此连接建立带重试。
"""
from __future__ import annotations

import logging
import os
import time

from psycopg import Cursor
from psycopg_pool import ConnectionPool

logger = logging.getLogger(__name__)

DATABASE_URL = os.environ.get(
    "DATABASE_URL",
    "postgresql://restrict:restrict@localhost:5432/restrict",
)
DB_CONNECT_RETRIES = int(os.environ.get("DB_CONNECT_RETRIES", "30"))
DB_CONNECT_INTERVAL = float(os.environ.get("DB_CONNECT_INTERVAL", "1.0"))

pool: ConnectionPool | None = None

_SCHEMA = """
CREATE TABLE IF NOT EXISTS region_meta (
    id INTEGER PRIMARY KEY DEFAULT 1,
    revision BIGINT NOT NULL DEFAULT 0,
    CONSTRAINT region_meta_singleton CHECK (id = 1)
);

INSERT INTO region_meta (id, revision)
VALUES (1, 0)
ON CONFLICT (id) DO NOTHING;

CREATE TABLE IF NOT EXISTS regions (
    id          BIGSERIAL PRIMARY KEY,
    name        TEXT NOT NULL,
    valid_from  TIMESTAMPTZ NOT NULL,
    valid_to    TIMESTAMPTZ NOT NULL,
    -- GeoJSON 风格的多边形环：[[x, y], ...]，首尾点相同（闭合）
    polygon     JSONB NOT NULL,
    wkt         TEXT NOT NULL,
    version     BIGINT NOT NULL DEFAULT 1,
    created_at  TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at  TIMESTAMPTZ NOT NULL DEFAULT now(),
    CONSTRAINT regions_time_half_open CHECK (valid_from < valid_to)
);

CREATE INDEX IF NOT EXISTS idx_regions_valid
    ON regions (valid_from, valid_to);

-- 可取消通行凭证：安全员对一条带时区绝对时刻连续轨迹签发的通行许可。
-- trajectory 以归一化后的 [{x,y,t(UTC ISO)}] JSONB 持久化；
-- region_revision 记录签发时核验所用区域集合的 revision。
-- 只有「未取消 且 末点时刻未过」的凭证会约束后续区域变更。
CREATE TABLE IF NOT EXISTS passes (
    id              BIGSERIAL PRIMARY KEY,
    trajectory      JSONB NOT NULL,
    region_revision BIGINT NOT NULL,
    first_time      TIMESTAMPTZ NOT NULL,
    last_time       TIMESTAMPTZ NOT NULL,
    cancelled       BOOLEAN NOT NULL DEFAULT FALSE,
    created_at      TIMESTAMPTZ NOT NULL DEFAULT now(),
    cancelled_at    TIMESTAMPTZ
);

CREATE INDEX IF NOT EXISTS idx_passes_active
    ON passes (cancelled, last_time);
"""


def _init_schema(cur: Cursor) -> None:
    # CREATE TABLE 与 INSERT 分开执行，避免在一个多语句调用里混入副作用
    for statement in _SCHEMA.split(";"):
        stmt = statement.strip()
        if stmt:
            cur.execute(stmt)


def init_pool() -> ConnectionPool:
    global pool
    last_exc: Exception | None = None
    for attempt in range(1, DB_CONNECT_RETRIES + 1):
        pool = ConnectionPool(DATABASE_URL, min_size=1, max_size=10, open=False)
        try:
            pool.open()
            with pool.connection() as conn:
                with conn.cursor() as cur:
                    _init_schema(cur)
            logger.info("数据库连接池已就绪，schema 已初始化")
            return pool
        except Exception as exc:  # 数据库未就绪时重试
            last_exc = exc
            logger.warning("等待数据库就绪（第 %s/%s 次）：%s",
                           attempt, DB_CONNECT_RETRIES, exc)
            pool.close()
            time.sleep(DB_CONNECT_INTERVAL)
    raise RuntimeError(f"无法在 {DB_CONNECT_RETRIES} 次重试后连接数据库: {last_exc}")


def close_pool() -> None:
    global pool
    if pool is not None:
        pool.close()
        pool = None
