"""
数据库层 - Neon PostgreSQL 数据库初始化与 CRUD 操作
通过 psycopg2 连接 Neon Serverless PostgreSQL
"""

import hashlib
import os
import secrets
import string
import threading
import time
import psycopg2
import psycopg2.extras
from datetime import datetime
from psycopg2.pool import ThreadedConnectionPool, PoolError
from typing import Optional, List, Dict, Any

import bcrypt

from config import DATABASE_URL
from permissions import DEFAULT_PERMISSIONS, PERMISSION_FLAT


# ---------- 密码工具 ----------
def hash_password(password: str) -> str:
    """使用 bcrypt 哈希密码（自动加盐）。
    bcrypt 仅处理前 72 字节，超长密码先做 SHA-256 预哈希以保证安全性。"""
    pwd_bytes = password.encode("utf-8")
    if len(pwd_bytes) > 72:
        pwd_bytes = hashlib.sha256(pwd_bytes).digest()
    return bcrypt.hashpw(pwd_bytes, bcrypt.gensalt()).decode("utf-8")


def is_legacy_hash(hashed: str) -> bool:
    """判断是否为旧版无盐 SHA-256 哈希（64 位十六进制，不以 $2 开头）"""
    return bool(hashed) and len(hashed) == 64 and not hashed.startswith("$2")


def verify_password(password: str, hashed: str) -> bool:
    """校验密码：优先 bcrypt，同时兼容旧版 SHA-256 哈希（存量数据迁移期间）"""
    if not hashed:
        return False
    if is_legacy_hash(hashed):
        return hashlib.sha256(password.encode("utf-8")).hexdigest() == hashed
    try:
        pwd_bytes = password.encode("utf-8")
        if len(pwd_bytes) > 72:
            pwd_bytes = hashlib.sha256(pwd_bytes).digest()
        return bcrypt.checkpw(pwd_bytes, hashed.encode("utf-8"))
    except (ValueError, TypeError):
        return False


def _generate_random_password(length: int = 12) -> str:
    """生成随机强密码（含字母、数字、符号）"""
    alphabet = string.ascii_letters + string.digits + "!@#$%^&*"
    return "".join(secrets.choice(alphabet) for _ in range(length))


# ---------- 常量 ----------
LIFECYCLE_STAGES = ["新线索", "已加企微", "预约试听", "到访", "已试听未成交", "在读", "待续费", "流失"]
SOURCE_CHANNELS = ["自然流量", "转介绍", "地推活动", "线上广告", "社群引流", "其他"]
INTENT_FRUIT_OPTIONS = ["🍎 红苹果", "🍏 青苹果", "🪲 坏苹果"]
GRADE_OPTIONS = ["一年级", "二年级", "三年级", "四年级", "五年级", "六年级", "初一", "初二", "初三", "高一", "高二", "高三"]
PACKAGE_STATUS = ["进行中", "已用完", "已到期"]
COURSE_TYPES = ["1v1", "小班课", "大班课", "陪练课", "其他"]
CLASS_TYPES = ["1v1", "1v多"]
ROLE_LABELS = {
    "admin": "管理员",
    "staff": "学管",
    "teacher": "教师",
    "hr": "人事",
    "finance": "财务",
}


# ---------- 登录安全（防暴力破解） ----------
LOGIN_MAX_ATTEMPTS = 5      # 锁定窗口内允许的最大失败次数
LOGIN_LOCK_MINUTES = 15     # 锁定窗口（分钟）
IP_LOCK_MAX_ATTEMPTS = 15   # 熔断：同一 IP 在 IP_LOCK_MINUTES 分钟内失败达到该值则锁定该 IP
IP_LOCK_MINUTES = 15        # IP 熔断锁定时长（分钟）
RATE_LIMIT_MAX = 20         # 限流：同一 IP 在 RATE_LIMIT_WINDOW_SECONDS 秒内失败达到该值则临时拒绝
RATE_LIMIT_WINDOW_SECONDS = 60  # 限流窗口（秒）
PASSWORD_MIN_LENGTH = 8     # 初始密码/重置密码建议最小长度


# ---------- 动态 SQL 列名白名单 ----------
# update_* 系列函数用 kwargs 的键拼接 SET 子句，字段名必须来自白名单，防止注入
ALLOWED_UPDATE_COLUMNS: Dict[str, set] = {
    "customers": {
        "name", "phone", "wechat", "source", "intent_fruit", "lifecycle_stage",
        "school", "grade", "teacher", "notes", "hours_zeroed_at",
    },
    "users": {
        "username", "display_name", "role", "subjects", "password", "must_change_password",
    },
    "follow_ups": {
        "follow_type", "content", "plan_time", "status",
    },
    "course_packages": {
        "package_name", "total_hours", "used_hours", "purchase_date", "expiry_date",
        "price", "original_price", "discount_amount", "unit_price", "status",
        "notes", "type",
    },
    "classes": {
        "name", "class_type", "course_id", "teacher", "manager",
        "max_students", "status", "start_date", "notes",
    },
}


def _filter_update_fields(table: str, kwargs: Dict[str, Any]) -> Dict[str, Any]:
    """按白名单过滤 update_* 的 kwargs，仅保留合法字段名，防御 SQL 注入"""
    allowed = ALLOWED_UPDATE_COLUMNS.get(table, set())
    return {k: v for k, v in kwargs.items() if k in allowed}


# ---------- 数据库连接池 ----------
_pool: Optional[ThreadedConnectionPool] = None
_pool_lock = threading.Lock()


def _get_pool() -> ThreadedConnectionPool:
    """惰性创建线程安全连接池（避免每次操作都新建连接，大幅减少远程数据库耗时）"""
    global _pool
    if _pool is None:
        with _pool_lock:
            if _pool is None:
                _pool = ThreadedConnectionPool(1, 10, DATABASE_URL)
    return _pool


def _reset_pool() -> None:
    """连接池整体失效（如连接被服务端回收）时重建"""
    global _pool
    with _pool_lock:
        if _pool is not None:
            try:
                _pool.closeall()
            except Exception:
                pass
            _pool = None


def get_connection() -> psycopg2.extensions.connection:
    """从连接池获取连接（连接失效时自动重建，由业务层显式 commit）"""
    for attempt in range(3):
        try:
            conn = _get_pool().getconn()
        except PoolError:
            _reset_pool()
            continue
        try:
            conn.autocommit = False
            with conn.cursor() as cur:
                cur.execute("SET timezone = 'Asia/Shanghai'")
            return conn
        except (psycopg2.OperationalError, psycopg2.InterfaceError):
            # 连接已失效（如数据库空闲回收），丢弃并重试
            try:
                _get_pool().putconn(conn, close=True)
            except Exception:
                pass
            if attempt == 2:
                _reset_pool()
    raise psycopg2.OperationalError("无法获取数据库连接")


def release_connection(conn) -> None:
    """将连接归还连接池（替代原来的连接关闭逻辑，保留连接复用）"""
    if conn is None:
        return
    try:
        _get_pool().putconn(conn)
    except Exception:
        try:
            psycopg2.extensions.connection.close(conn)
        except Exception:
            pass


# ---------- 低频数据 TTL 缓存 ----------
# 用户列表、课程列表、单课时设置等低频变化数据，在页面每次 rerun 时避免重复查询。
# 缓存为进程内共享（带锁），写操作会主动失效，TTL 过期也会自动刷新。
_query_cache: Dict[str, tuple] = {}
_query_cache_lock = threading.Lock()
_QUERY_CACHE_TTL = 60  # 秒


def _cache_get(key: str):
    with _query_cache_lock:
        item = _query_cache.get(key)
    if item and item[0] > time.time():
        return item[1]
    return None


def _cache_set(key: str, value) -> None:
    with _query_cache_lock:
        _query_cache[key] = (time.time() + _QUERY_CACHE_TTL, value)


def _cache_clear(key: str) -> None:
    with _query_cache_lock:
        _query_cache.pop(key, None)


# ==================== 表结构初始化 ====================

def init_db():
    """初始化数据库表结构（如果表不存在则创建；已存在则自动补充新列）
    全部 DDL/DML 在单个事务中执行，任一步失败整体回滚，避免半初始化状态。"""
    conn = get_connection()
    try:
        _init_db_impl(conn)
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        release_connection(conn)


def _init_db_impl(conn):
    """执行建表/补列/初始化数据的全部 SQL（事务由 init_db() 统一提交/回滚）"""
    cursor = conn.cursor()

    # --- 客户表 ---
    cursor.execute("""
        CREATE TABLE IF NOT EXISTS customers (
            id              SERIAL PRIMARY KEY,
            name            TEXT    NOT NULL,
            phone           TEXT    DEFAULT '',
            wechat          TEXT    DEFAULT '',
            source          TEXT    DEFAULT '自然流量',
            intent_fruit    TEXT    DEFAULT '🍏 青苹果',
            lifecycle_stage TEXT    DEFAULT '新线索',
            school          TEXT    DEFAULT '',
            grade           TEXT    DEFAULT '',
            teacher         TEXT    DEFAULT '',
            notes           TEXT    DEFAULT '',
            created_at      TEXT    NOT NULL,
            updated_at      TEXT    NOT NULL
        )
    """)

    # --- 数据库迁移：为旧表补充字段 ---
    cursor.execute("""
        SELECT column_name FROM information_schema.columns
        WHERE table_name = 'customers'
    """)
    existing_cols = [r[0] for r in cursor.fetchall()]
    if "intent_fruit" not in existing_cols:
        cursor.execute("ALTER TABLE customers ADD COLUMN IF NOT EXISTS intent_fruit TEXT DEFAULT '🍏 青苹果'")
    if "school" not in existing_cols:
        cursor.execute("ALTER TABLE customers ADD COLUMN IF NOT EXISTS school TEXT DEFAULT ''")
    if "grade" not in existing_cols:
        cursor.execute("ALTER TABLE customers ADD COLUMN IF NOT EXISTS grade TEXT DEFAULT ''")
    if "teacher" not in existing_cols:
        cursor.execute("ALTER TABLE customers ADD COLUMN IF NOT EXISTS teacher TEXT DEFAULT ''")
    # 课时归零计时：每日巡检记录首次归零时间，连续归零超过 7 天自动转为流失
    if "hours_zeroed_at" not in existing_cols:
        cursor.execute("ALTER TABLE customers ADD COLUMN IF NOT EXISTS hours_zeroed_at TEXT")
    # 已废弃字段：删除意向等级（intent_level）
    if "intent_level" in existing_cols:
        cursor.execute("ALTER TABLE customers DROP COLUMN IF EXISTS intent_level")

    # --- 用户表（学管/管理员） ---
    cursor.execute("""
        CREATE TABLE IF NOT EXISTS users (
            id              SERIAL PRIMARY KEY,
            username        TEXT    NOT NULL UNIQUE,
            password        TEXT    NOT NULL,
            display_name    TEXT    NOT NULL,
            role            TEXT    NOT NULL DEFAULT 'staff',
            created_at      TEXT    NOT NULL
        )
    """)

    # --- 初始化默认管理员 ---
    # 密码优先读取环境变量 MUSHANG_ADMIN_PASSWORD；未设置则生成随机强密码并打印到控制台
    cursor.execute("SELECT username, password FROM users WHERE username = 'admin'")
    admin_row = cursor.fetchone()
    if admin_row is None:
        admin_pwd = os.environ.get("MUSHANG_ADMIN_PASSWORD") or _generate_random_password()
        cursor.execute("""
            INSERT INTO users (username, password, display_name, role, must_change_password, created_at)
            VALUES (%s, %s, %s, %s, TRUE, TO_CHAR(NOW(), 'YYYY-MM-DD HH24:MI:SS'))
        """, ("admin", hash_password(admin_pwd), "系统管理员", "admin"))
        print("[安全提示] 已创建默认管理员账号 admin。初始密码：%s" % admin_pwd)
        print("[安全提示] 也可通过环境变量 MUSHANG_ADMIN_PASSWORD 自定义初始密码；首次登录需修改密码。")
    elif is_legacy_hash(admin_row[1]) and admin_row[1] == hashlib.sha256(b"admin123").hexdigest():
        # 存量库中 admin 仍使用默认弱口令 → 强制下次登录修改
        cursor.execute("UPDATE users SET must_change_password = TRUE WHERE username = 'admin'")
        print("[安全提示] 检测到管理员 admin 仍在使用默认弱口令，已强制其下次登录时修改密码。")

    # --- 用户表兼容迁移：教师可带科目（逗号分隔，排 1v1 课时从中选择） ---
    cursor.execute("ALTER TABLE users ADD COLUMN IF NOT EXISTS subjects TEXT")
    # --- 用户表兼容迁移：首次登录强制修改密码标记（安全策略） ---
    cursor.execute("ALTER TABLE users ADD COLUMN IF NOT EXISTS must_change_password BOOLEAN NOT NULL DEFAULT FALSE")

    # --- 跟进记录表 ---
    cursor.execute("""
        CREATE TABLE IF NOT EXISTS follow_ups (
            id              SERIAL PRIMARY KEY,
            customer_id     INTEGER NOT NULL,
            follow_type     TEXT    DEFAULT '电话',
            content         TEXT    DEFAULT '',
            plan_time       TEXT    NOT NULL,
            status          TEXT    DEFAULT '待跟进',
            created_at      TEXT    NOT NULL,
            updated_at      TEXT    NOT NULL DEFAULT '',
            FOREIGN KEY (customer_id) REFERENCES customers(id) ON DELETE CASCADE
        )
    """)
    cursor.execute(
        "ALTER TABLE follow_ups ADD COLUMN IF NOT EXISTS updated_at TEXT NOT NULL DEFAULT ''"
    )

    # --- 课时包表 ---
    cursor.execute("""
        CREATE TABLE IF NOT EXISTS course_packages (
            id              SERIAL PRIMARY KEY,
            customer_id     INTEGER NOT NULL,
            package_name    TEXT    NOT NULL,
            total_hours     NUMERIC(10,2) NOT NULL DEFAULT 0,
            used_hours      NUMERIC(10,2) NOT NULL DEFAULT 0,
            purchase_date   TEXT    NOT NULL,
            expiry_date     TEXT    NOT NULL,
            price           NUMERIC(10,2) DEFAULT 0,
            original_price  NUMERIC(10,2) DEFAULT 0,
            discount_amount NUMERIC(10,2) DEFAULT 0,
            unit_price      NUMERIC(10,2) DEFAULT 0,
            status          TEXT    DEFAULT '进行中',
            notes           TEXT    DEFAULT '',
            created_at      TEXT    NOT NULL,
            updated_at      TEXT    NOT NULL,
            FOREIGN KEY (customer_id) REFERENCES customers(id) ON DELETE CASCADE
        )
    """)
    # 兼容已有库：补充课时包价格明细字段（原价/优惠/实际单课时价格）
    cursor.execute("ALTER TABLE course_packages ADD COLUMN IF NOT EXISTS original_price NUMERIC(10,2) DEFAULT 0")
    cursor.execute("ALTER TABLE course_packages ADD COLUMN IF NOT EXISTS discount_amount NUMERIC(10,2) DEFAULT 0")
    cursor.execute("ALTER TABLE course_packages ADD COLUMN IF NOT EXISTS unit_price NUMERIC(10,2) DEFAULT 0")
    # 课时包类型（1v1 一对一 / 1v多 一对多）：旧数据默认按 1v1 处理
    cursor.execute("ALTER TABLE course_packages ADD COLUMN IF NOT EXISTS type TEXT DEFAULT '1v1'")

    # --- 课时包模板表（转在读时选择报名课时用） ---
    cursor.execute("""
        CREATE TABLE IF NOT EXISTS course_package_templates (
            id          SERIAL PRIMARY KEY,
            name        TEXT    NOT NULL UNIQUE,
            total_hours NUMERIC(10,2) NOT NULL DEFAULT 0,
            price       NUMERIC(10,2) NOT NULL DEFAULT 0,
            grade       TEXT    DEFAULT '',
            status      TEXT    DEFAULT '启用',
            created_at  TEXT    NOT NULL,
            updated_at  TEXT    NOT NULL
        )
    """)
    # 兼容已有库：补充课时包模板年级字段（留空表示所有年级通用）
    cursor.execute("ALTER TABLE course_package_templates ADD COLUMN IF NOT EXISTS grade TEXT DEFAULT ''")
    # 课时包模板类型（1v1 一对一 / 1v多 一对多）
    cursor.execute("ALTER TABLE course_package_templates ADD COLUMN IF NOT EXISTS type TEXT DEFAULT '1v1'")

    # --- 登录失败记录表（防暴力破解） ---
    cursor.execute("""
        CREATE TABLE IF NOT EXISTS login_attempts (
            id           SERIAL PRIMARY KEY,
            username     TEXT NOT NULL,
            ip_address   VARCHAR(45) DEFAULT '',
            attempted_at TIMESTAMP NOT NULL DEFAULT NOW()
        )
    """)
    # 兼容已有库：补充 ip_address 列（IP 级限流/熔断用）
    cursor.execute("ALTER TABLE login_attempts ADD COLUMN IF NOT EXISTS ip_address VARCHAR(45) DEFAULT ''")
    # 定期清理超过 24 小时的失败记录，防止表无限增长
    cursor.execute("DELETE FROM login_attempts WHERE attempted_at < NOW() - INTERVAL '24 hours'")

    # --- 消课记录表 ---
    cursor.execute("""
        CREATE TABLE IF NOT EXISTS course_records (
            id              SERIAL PRIMARY KEY,
            package_id      INTEGER NOT NULL,
            customer_id     INTEGER NOT NULL,
            record_date     TEXT    NOT NULL,
            hours_used      NUMERIC(10,2) NOT NULL DEFAULT 1,
            course_type     TEXT    DEFAULT '1v1',
            teacher         TEXT    DEFAULT '',
            notes           TEXT    DEFAULT '',
            schedule_id     INTEGER,
            created_at      TEXT    NOT NULL,
            FOREIGN KEY (package_id) REFERENCES course_packages(id) ON DELETE CASCADE,
            FOREIGN KEY (customer_id) REFERENCES customers(id) ON DELETE CASCADE
        )
    """)
    # 兼容已有库：补充 schedule_id 列（反馈自动扣课时防重复用）
    cursor.execute("ALTER TABLE course_records ADD COLUMN IF NOT EXISTS schedule_id INTEGER")
    # 幂等性兜底：同一课表只允许一条自动消课记录。
    # 部分唯一索引（仅对非空 schedule_id 生效，手动消课存 NULL 不受限）。
    # 先清理历史重复（保留 id 最小的一条），再建索引，避免旧库建索引失败。
    cursor.execute("""
        DELETE FROM course_records a
        USING course_records b
        WHERE a.schedule_id IS NOT NULL
          AND a.schedule_id = b.schedule_id
          AND a.id > b.id
    """)
    cursor.execute("""
        CREATE UNIQUE INDEX IF NOT EXISTS uq_course_records_schedule
            ON course_records(schedule_id)
            WHERE schedule_id IS NOT NULL
    """)

    # --- 课程表 ---
    cursor.execute("""
        CREATE TABLE IF NOT EXISTS courses (
            id          SERIAL PRIMARY KEY,
            name        TEXT    NOT NULL UNIQUE,
            description TEXT    DEFAULT '',
            created_at  TEXT    NOT NULL
        )
    """)

    # --- 班级表 ---
    cursor.execute("""
        CREATE TABLE IF NOT EXISTS classes (
            id           SERIAL PRIMARY KEY,
            name         TEXT    NOT NULL,
            class_type   TEXT    NOT NULL DEFAULT '1v多',
            course_id    INTEGER,
            teacher      TEXT    DEFAULT '',
            manager      TEXT    DEFAULT '',
            max_students INTEGER DEFAULT 0,
            status       TEXT    DEFAULT '进行中',
            start_date   TEXT    DEFAULT '',
            notes        TEXT    DEFAULT '',
            created_at   TEXT    NOT NULL,
            updated_at   TEXT    NOT NULL,
            FOREIGN KEY (course_id) REFERENCES courses(id) ON DELETE CASCADE
        )
    """)

    # --- 班级学员表 ---
    cursor.execute("""
        CREATE TABLE IF NOT EXISTS class_students (
            id          SERIAL PRIMARY KEY,
            class_id    INTEGER NOT NULL,
            customer_id INTEGER NOT NULL,
            joined_at   TEXT    NOT NULL,
            UNIQUE (class_id, customer_id),
            FOREIGN KEY (class_id) REFERENCES classes(id) ON DELETE CASCADE,
            FOREIGN KEY (customer_id) REFERENCES customers(id) ON DELETE CASCADE
        )
    """)

    # --- 课表表 ---
    cursor.execute("""
        CREATE TABLE IF NOT EXISTS schedules (
            id          SERIAL PRIMARY KEY,
            class_id    INTEGER,
            course_id   INTEGER,
            customer_id INTEGER,
            title       TEXT    NOT NULL,
            teacher     TEXT    DEFAULT '',
            start_time  TEXT    NOT NULL,
            end_time    TEXT    NOT NULL,
            location    TEXT    DEFAULT '',
            notes       TEXT    DEFAULT '',
            created_at  TEXT    NOT NULL,
            FOREIGN KEY (class_id) REFERENCES classes(id) ON DELETE SET NULL,
            FOREIGN KEY (course_id) REFERENCES courses(id) ON DELETE CASCADE
        )
    """)

    # --- 课堂反馈表 ---
    cursor.execute("""
        CREATE TABLE IF NOT EXISTS schedule_feedback (
            id          SERIAL PRIMARY KEY,
            schedule_id INTEGER NOT NULL UNIQUE,
            teacher     TEXT    DEFAULT '',
            content     TEXT    DEFAULT '',
            created_at  TEXT    NOT NULL,
            updated_at  TEXT    NOT NULL,
            FOREIGN KEY (schedule_id) REFERENCES schedules(id) ON DELETE CASCADE
        )
    """)

    # --- 全局设置表（单课时时长等系统配置） ---
    cursor.execute("""
        CREATE TABLE IF NOT EXISTS app_settings (
            key         TEXT PRIMARY KEY,
            value       TEXT NOT NULL DEFAULT ''
        )
    """)

    # --- 角色权限表 ---
    cursor.execute("""
        CREATE TABLE IF NOT EXISTS role_permissions (
            id           SERIAL PRIMARY KEY,
            role         TEXT NOT NULL,
            resource_key TEXT NOT NULL,
            allowed      BOOLEAN NOT NULL DEFAULT TRUE,
            UNIQUE (role, resource_key)
        )
    """)
    # 初始填充默认权限：新增资源自动补默认值；
    # 默认「应允许」但当前为「禁止」的行补回允许（保证内置默认权限集变更后生效），
    # 不覆盖管理员显式开启的「默认禁止」资源（保持管理员调整结果）。
    # 已初始化（行数达标）时跳过，避免每次启动都执行上百次插入
    cursor.execute("SELECT COUNT(*) FROM role_permissions")
    if cursor.fetchone()[0] < len(DEFAULT_PERMISSIONS) * len(PERMISSION_FLAT):
        _batch = []
        for _role in DEFAULT_PERMISSIONS:
            _defaults = DEFAULT_PERMISSIONS[_role]
            for _key in PERMISSION_FLAT:
                _batch.append((_role, _key, _key in _defaults))
        psycopg2.extras.execute_batch(
            cursor,
            "INSERT INTO role_permissions (role, resource_key, allowed) "
            "VALUES (%s, %s, %s) "
            "ON CONFLICT (role, resource_key) DO UPDATE SET allowed = EXCLUDED.allowed "
            "WHERE role_permissions.allowed = FALSE AND EXCLUDED.allowed = TRUE",
            _batch,
            page_size=500,
        )

    # 清理权限目录中已移除的资源（如已废弃的课时包管理权限）
    if PERMISSION_FLAT:
        cursor.execute(
            "DELETE FROM role_permissions WHERE resource_key NOT IN %s",
            (tuple(PERMISSION_FLAT),),
        )

    # --- 兼容改造：课程可空（班级/课表不再强制关联课程）---
    # 1v1 排课不再经过课程/班级，课表可直接挂学员（schedules.customer_id）
    cursor.execute("ALTER TABLE classes ALTER COLUMN course_id DROP NOT NULL")
    cursor.execute("ALTER TABLE schedules ALTER COLUMN course_id DROP NOT NULL")
    cursor.execute("ALTER TABLE schedules ADD COLUMN IF NOT EXISTS customer_id INTEGER")
    # 排课可指定使用的课时包（1v1 排课时选择；NULL 表示自动组合同类型课时包）
    cursor.execute("ALTER TABLE schedules ADD COLUMN IF NOT EXISTS package_id INTEGER")

    # 自动标记已过期的课时包（幂等，随初始化缓存每天最多执行一次）
    cursor.execute(
        "UPDATE course_packages SET status='已到期', updated_at=TO_CHAR(NOW(), 'YYYY-MM-DD HH24:MI:SS') "
        "WHERE status='进行中' AND expiry_date < TO_CHAR(CURRENT_DATE, 'YYYY-MM-DD')"
    )
    # 事务由 init_db() 统一 commit/rollback


def get_role_permissions(role: str) -> Dict[str, bool]:
    """查询角色权限映射 {resource_key: allowed}；管理员恒为全允许"""
    if role == "admin":
        return {k: True for k in PERMISSION_FLAT}
    conn = get_connection()
    cursor = conn.cursor()
    cursor.execute(
        "SELECT resource_key, allowed FROM role_permissions WHERE role = %s",
        (role,),
    )
    mapping = {k: bool(v) for k, v in cursor.fetchall()}
    release_connection(conn)
    # 补齐尚未初始化的资源（使用内置默认值），保证权限目录升级后行为一致
    defaults = DEFAULT_PERMISSIONS.get(role, set())
    for key in PERMISSION_FLAT:
        mapping.setdefault(key, key in defaults)
    return mapping


def save_role_permissions(role: str, mapping: Dict[str, bool]) -> None:
    """全量保存角色权限映射（先删后插，单事务，失败整体回滚）"""
    conn = get_connection()
    cursor = conn.cursor()
    try:
        cursor.execute("DELETE FROM role_permissions WHERE role = %s", (role,))
        for key, allowed in mapping.items():
            if key not in PERMISSION_FLAT:
                continue
            cursor.execute(
                "INSERT INTO role_permissions (role, resource_key, allowed) VALUES (%s, %s, %s)",
                (role, key, bool(allowed)),
            )
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        release_connection(conn)


# ==================== 客户 CRUD ====================

def add_customer(name: str, phone: str = "", wechat: str = "", source: str = "自然流量",
                 lifecycle_stage: str = "新线索",
                 notes: str = "", intent_fruit: str = "🍏 青苹果",
                 school: str = "", grade: str = "", teacher: str = "") -> int:
    """新增客户，返回新客户ID（时间戳由数据库 NOW() 生成，确保时区一致；单事务）"""
    conn = get_connection()
    cursor = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
    try:
        cursor.execute("""
            INSERT INTO customers (name, phone, wechat, source, intent_fruit, lifecycle_stage,
                                   school, grade, teacher, notes, created_at, updated_at)
            VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s,
                    TO_CHAR(NOW(), 'YYYY-MM-DD HH24:MI:SS'), TO_CHAR(NOW(), 'YYYY-MM-DD HH24:MI:SS'))
            RETURNING id
        """, (name, phone, wechat, source, intent_fruit, lifecycle_stage,
              school, grade, teacher, notes))
        new_id = cursor.fetchone()["id"]
        conn.commit()
        return new_id
    except Exception:
        conn.rollback()
        raise
    finally:
        release_connection(conn)


def update_customer(customer_id: int, **kwargs) -> bool:
    """更新客户信息，kwargs 传入需要更新的字段（字段名受白名单约束，updated_at 由数据库 NOW() 自动设置）"""
    if not kwargs:
        return False
    data = _filter_update_fields("customers", kwargs)
    if not data:
        return False
    set_clause = ", ".join([f"{k} = %s" for k in data.keys()])
    values = list(data.values()) + [customer_id]
    conn = get_connection()
    try:
        conn.cursor().execute(
            f"UPDATE customers SET {set_clause}, updated_at = TO_CHAR(NOW(), 'YYYY-MM-DD HH24:MI:SS') WHERE id = %s", values
        )
        conn.commit()
        return True
    except Exception:
        conn.rollback()
        raise
    finally:
        release_connection(conn)


def delete_customer(customer_id: int) -> bool:
    """删除客户及其关联跟进记录（级联删除由外键 ON DELETE CASCADE 自动处理；单事务）"""
    conn = get_connection()
    try:
        conn.cursor().execute("DELETE FROM customers WHERE id = %s", (customer_id,))
        conn.commit()
        return True
    except Exception:
        conn.rollback()
        raise
    finally:
        release_connection(conn)


def get_all_customers(search: str = "", stage_filter: str = "",
                      fruit_filter: str = "", teacher: str = "") -> List[Dict[str, Any]]:
    """查询客户列表，支持姓名搜索、阶段筛选、苹果意向筛选、学管过滤
    teacher 传具体用户名时仅返回该学管名下客户；空字符串返回全部（管理员）"""
    conn = get_connection()
    cursor = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
    query = "SELECT * FROM customers WHERE 1=1"
    params: List[Any] = []
    if search:
        query += " AND (name LIKE %s OR phone LIKE %s)"
        params.extend([f"%{search}%", f"%{search}%"])
    if stage_filter:
        query += " AND lifecycle_stage = %s"
        params.append(stage_filter)
    if fruit_filter:
        query += " AND intent_fruit = %s"
        params.append(fruit_filter)
    if teacher:
        query += " AND teacher = %s"
        params.append(teacher)
    query += " ORDER BY updated_at DESC"
    cursor.execute(query, params)
    rows = cursor.fetchall()
    release_connection(conn)
    return [dict(r) for r in rows]


def get_customer_by_id(customer_id: int) -> Optional[Dict[str, Any]]:
    """按ID获取单个客户"""
    conn = get_connection()
    cursor = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
    cursor.execute("SELECT * FROM customers WHERE id = %s", (customer_id,))
    row = cursor.fetchone()
    release_connection(conn)
    return dict(row) if row else None


# ==================== 用户（学管/管理员）管理 ====================

def add_user(username: str, password: str, display_name: str, role: str = "staff",
             subjects: str = "") -> bool:
    """新增用户，用户名唯一，返回是否成功；subjects 为教师可带科目（逗号分隔）"""
    conn = get_connection()
    cursor = conn.cursor()
    try:
        cursor.execute("""
            INSERT INTO users (username, password, display_name, role, subjects, must_change_password, created_at)
            VALUES (%s, %s, %s, %s, %s, TRUE, TO_CHAR(NOW(), 'YYYY-MM-DD HH24:MI:SS'))
        """, (username, hash_password(password), display_name, role, subjects or ""))
        conn.commit()
        _cache_clear("all_users")
        return True
    except psycopg2.errors.UniqueViolation:
        conn.rollback()
        return False
    except Exception:
        conn.rollback()
        raise
    finally:
        release_connection(conn)


def update_user(user_id: int, **kwargs) -> bool:
    """更新用户信息（密码、显示名、角色、强制改密标记），密码需传明文自动哈希（单事务）"""
    if not kwargs:
        return False
    data = _filter_update_fields("users", kwargs)
    if "password" in data and data["password"]:
        data["password"] = hash_password(data["password"])
    if not data:
        return False
    set_clause = ", ".join([f"{k} = %s" for k in data.keys()])
    values = list(data.values()) + [user_id]
    conn = get_connection()
    try:
        conn.cursor().execute(f"UPDATE users SET {set_clause} WHERE id = %s", values)
        conn.commit()
        _cache_clear("all_users")
        return True
    except Exception:
        conn.rollback()
        raise
    finally:
        release_connection(conn)


def delete_user(user_id: int) -> bool:
    """删除用户，且不能删除 admin 管理员（单事务：删用户 + 清空其名下客户学管）"""
    conn = get_connection()
    cursor = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
    try:
        cursor.execute("SELECT username FROM users WHERE id = %s", (user_id,))
        row = cursor.fetchone()
        if not row or row["username"] == "admin":
            conn.rollback()
            return False
        cursor.execute("DELETE FROM users WHERE id = %s", (user_id,))
        # 删除用户后，将其名下客户学管置空，避免孤儿数据
        cursor.execute("UPDATE customers SET teacher = '' WHERE teacher = %s", (row["username"],))
        conn.commit()
        _cache_clear("all_users")
        return True
    except Exception:
        conn.rollback()
        raise
    finally:
        release_connection(conn)


def get_all_users() -> List[Dict[str, Any]]:
    """获取全部用户（不含密码哈希）——结果按 TTL 缓存，用户写操作后自动失效"""
    cached = _cache_get("all_users")
    if cached is not None:
        return cached
    conn = get_connection()
    cursor = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
    cursor.execute("""
        SELECT id, username, display_name, role, subjects, created_at
        FROM users ORDER BY role DESC, id ASC
    """)
    rows = cursor.fetchall()
    release_connection(conn)
    result = [dict(r) for r in rows]
    _cache_set("all_users", result)
    return result


def verify_login(username: str, password: str) -> Optional[Dict[str, Any]]:
    """校验用户名密码，成功返回用户信息（不含密码哈希），失败返回 None。
    存量 SHA-256 哈希验证通过后自动升级为 bcrypt。"""
    conn = get_connection()
    cursor = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
    try:
        cursor.execute(
            "SELECT id, username, display_name, role, password, must_change_password "
            "FROM users WHERE username = %s",
            (username,),
        )
        row = cursor.fetchone()
        if not row:
            return None
        stored = row.get("password") or ""
        if not verify_password(password, stored):
            return None
        # 旧版 SHA-256 哈希自动升级为 bcrypt
        if is_legacy_hash(stored):
            cursor.execute(
                "UPDATE users SET password = %s WHERE id = %s",
                (hash_password(password), row["id"]),
            )
            conn.commit()
        result = dict(row)
        result.pop("password", None)
        result["must_change_password"] = bool(result.get("must_change_password"))
        return result
    except Exception:
        conn.rollback()
        raise
    finally:
        release_connection(conn)


# ---------- 登录失败限速 / 锁定（防暴力破解） ----------

def check_login_locked(username: str) -> bool:
    """检查用户是否处于锁定状态（最近 LOGIN_LOCK_MINUTES 分钟内失败次数达到上限）"""
    conn = get_connection()
    cursor = conn.cursor()
    try:
        cursor.execute(
            "SELECT COUNT(*) FROM login_attempts WHERE username = %s "
            "AND attempted_at > NOW() - make_interval(mins => %s)",
            (username, LOGIN_LOCK_MINUTES),
        )
        return cursor.fetchone()[0] >= LOGIN_MAX_ATTEMPTS
    finally:
        release_connection(conn)


def check_ip_locked(ip: str) -> bool:
    """熔断：同一 IP 在 IP_LOCK_MINUTES 分钟内失败次数达到上限则锁定该 IP。
    IP 为空时跳过（降级为纯账号级防护）。"""
    if not ip:
        return False
    conn = get_connection()
    cursor = conn.cursor()
    try:
        cursor.execute(
            "SELECT COUNT(*) FROM login_attempts WHERE ip_address = %s "
            "AND attempted_at > NOW() - make_interval(mins => %s)",
            (ip, IP_LOCK_MINUTES),
        )
        return cursor.fetchone()[0] >= IP_LOCK_MAX_ATTEMPTS
    finally:
        release_connection(conn)


def check_login_rate(ip: str) -> bool:
    """限流：同一 IP 在 RATE_LIMIT_WINDOW_SECONDS 秒内失败次数达到上限则临时拒绝。
    IP 为空时跳过（降级为纯账号级防护）。"""
    if not ip:
        return False
    conn = get_connection()
    cursor = conn.cursor()
    try:
        cursor.execute(
            "SELECT COUNT(*) FROM login_attempts WHERE ip_address = %s "
            "AND attempted_at > NOW() - make_interval(secs => %s)",
            (ip, RATE_LIMIT_WINDOW_SECONDS),
        )
        return cursor.fetchone()[0] >= RATE_LIMIT_MAX
    finally:
        release_connection(conn)


def record_failed_login(username: str, ip: str = "") -> None:
    """记录一次登录失败（含来源 IP，单事务）"""
    conn = get_connection()
    cursor = conn.cursor()
    try:
        cursor.execute(
            "INSERT INTO login_attempts (username, ip_address) VALUES (%s, %s)",
            (username, ip or ""),
        )
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        release_connection(conn)


def clear_failed_logins(username: str, ip: str = "") -> None:
    """登录成功后清除该用户的失败记录（单事务）。
    注：IP 维度失败计数独立保留，避免同网段某人成功登录即清空共享 IP 的熔断累计。"""
    conn = get_connection()
    cursor = conn.cursor()
    try:
        cursor.execute("DELETE FROM login_attempts WHERE username = %s", (username,))
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        release_connection(conn)


# ==================== 跟进记录 CRUD ====================

def add_follow_up(customer_id: int, follow_type: str, content: str, plan_time: str,
                  status: str = "待跟进") -> int:
    """新增跟进记录（时间戳由数据库 NOW() 生成），同时刷新客户更新时间（单事务）"""
    conn = get_connection()
    cursor = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
    try:
        cursor.execute("""
            INSERT INTO follow_ups (customer_id, follow_type, content, plan_time, status,
                                    created_at, updated_at)
            VALUES (%s, %s, %s, %s, %s,
                    TO_CHAR(NOW(), 'YYYY-MM-DD HH24:MI:SS'),
                    TO_CHAR(NOW(), 'YYYY-MM-DD HH24:MI:SS'))
            RETURNING id
        """, (customer_id, follow_type, content, plan_time, status))
        new_id = cursor.fetchone()["id"]
        # 同步刷新客户更新时间
        cursor.execute(
            "UPDATE customers SET updated_at = TO_CHAR(NOW(), 'YYYY-MM-DD HH24:MI:SS') WHERE id = %s",
            (customer_id,)
        )
        conn.commit()
        return new_id
    except Exception:
        conn.rollback()
        raise
    finally:
        release_connection(conn)


def update_follow_up(follow_id: int, **kwargs) -> bool:
    """更新跟进记录，自动刷新跟进记录和对应客户的 updated_at"""
    if not kwargs:
        return False
    data = _filter_update_fields("follow_ups", kwargs)
    if not data:
        return False
    conn = get_connection()
    cursor = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
    # 先查出关联的客户ID
    cursor.execute("SELECT customer_id FROM follow_ups WHERE id = %s", (follow_id,))
    row = cursor.fetchone()
    if not row:
        release_connection(conn)
        return False
    customer_id = row["customer_id"]

    set_clause = ", ".join([f"{k} = %s" for k in data.keys()])
    set_clause += ", updated_at = TO_CHAR(NOW(), 'YYYY-MM-DD HH24:MI:SS')"
    values = list(data.values()) + [follow_id]
    try:
        cursor.execute(f"UPDATE follow_ups SET {set_clause} WHERE id = %s", values)
        # 同步刷新客户更新时间
        cursor.execute(
            "UPDATE customers SET updated_at = TO_CHAR(NOW(), 'YYYY-MM-DD HH24:MI:SS') WHERE id = %s",
            (customer_id,)
        )
        conn.commit()
        return True
    except Exception:
        conn.rollback()
        raise
    finally:
        release_connection(conn)


def delete_follow_up(follow_id: int) -> bool:
    """删除跟进记录（单事务）"""
    conn = get_connection()
    try:
        conn.cursor().execute("DELETE FROM follow_ups WHERE id = %s", (follow_id,))
        conn.commit()
        return True
    except Exception:
        conn.rollback()
        raise
    finally:
        release_connection(conn)


def get_follow_ups(customer_id: Optional[int] = None, status: str = "",
                   date_from: str = "", date_to: str = "", teacher: str = "") -> List[Dict[str, Any]]:
    """查询跟进记录，支持按客户、状态、日期范围、学管过滤
    teacher 传具体用户名时仅返回该学管名下客户的记录；空字符串返回全部（管理员）"""
    conn = get_connection()
    cursor = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
    query = """
        SELECT f.*, c.name as customer_name, c.lifecycle_stage, c.teacher
        FROM follow_ups f
        LEFT JOIN customers c ON f.customer_id = c.id
        WHERE 1=1
    """
    params: List[Any] = []
    if customer_id:
        query += " AND f.customer_id = %s"
        params.append(customer_id)
    if status:
        query += " AND f.status = %s"
        params.append(status)
    if date_from:
        query += " AND f.plan_time >= %s"
        params.append(date_from)
    if date_to:
        query += " AND f.plan_time <= %s"
        params.append(date_to + " 23:59:59")
    if teacher:
        query += " AND c.teacher = %s"
        params.append(teacher)
    query += " ORDER BY f.plan_time ASC"
    cursor.execute(query, params)
    rows = cursor.fetchall()
    release_connection(conn)
    return [dict(r) for r in rows]


# ==================== 课时包 CRUD ====================

def add_course_package(customer_id: int, package_name: str, total_hours: float,
                        purchase_date: str, expiry_date: str, price: float = 0,
                        notes: str = "", status: str = "进行中",
                        original_price: float = 0, discount_amount: float = 0,
                        unit_price: float = 0, pkg_type: str = "1v1") -> int:
    """新增课时包，返回ID（时间戳由数据库 NOW() 生成）
    price          : 实收价格（元）
    original_price : 原价（元）
    discount_amount: 优惠价格（元）
    unit_price     : 实际单课时价格（元/节）
    pkg_type       : 课时包类型（1v1 一对一 / 1v多 一对多）"""
    conn = get_connection()
    cursor = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
    try:
        cursor.execute("""
            INSERT INTO course_packages (customer_id, package_name, total_hours, used_hours,
                purchase_date, expiry_date, price, original_price, discount_amount, unit_price,
                status, notes, type, created_at, updated_at)
            VALUES (%s, %s, %s, 0, %s, %s, %s, %s, %s, %s, %s, %s, %s,
                    TO_CHAR(NOW(), 'YYYY-MM-DD HH24:MI:SS'), TO_CHAR(NOW(), 'YYYY-MM-DD HH24:MI:SS'))
            RETURNING id
        """, (customer_id, package_name, total_hours, purchase_date, expiry_date,
              price, original_price, discount_amount, unit_price, status, notes, pkg_type))
        new_id = cursor.fetchone()["id"]
        conn.commit()
        return new_id
    except Exception:
        conn.rollback()
        raise
    finally:
        release_connection(conn)


def update_course_package(package_id: int, **kwargs) -> bool:
    """更新课时包（字段名受白名单约束，updated_at 由数据库 NOW() 自动设置；单事务）"""
    if not kwargs:
        return False
    data = _filter_update_fields("course_packages", kwargs)
    if not data:
        return False
    set_clause = ", ".join([f"{k} = %s" for k in data.keys()])
    values = list(data.values()) + [package_id]
    conn = get_connection()
    try:
        conn.cursor().execute(
            f"UPDATE course_packages SET {set_clause}, updated_at = TO_CHAR(NOW(), 'YYYY-MM-DD HH24:MI:SS') WHERE id = %s", values
        )
        conn.commit()
        return True
    except Exception:
        conn.rollback()
        raise
    finally:
        release_connection(conn)


def delete_course_package(package_id: int) -> bool:
    """删除课时包（级联删除消课记录由外键 ON DELETE CASCADE 自动处理；单事务）"""
    conn = get_connection()
    try:
        conn.cursor().execute("DELETE FROM course_packages WHERE id = %s", (package_id,))
        conn.commit()
        return True
    except Exception:
        conn.rollback()
        raise
    finally:
        release_connection(conn)


def get_course_packages(customer_id: int = 0, status: str = "", teacher: str = "") -> List[Dict[str, Any]]:
    """查询课时包列表，支持按客户、状态、学管筛选"""
    conn = get_connection()
    cursor = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
    query = """
        SELECT p.*, c.name as customer_name, c.lifecycle_stage, c.teacher,
               (p.total_hours - p.used_hours) as remaining_hours
        FROM course_packages p
        LEFT JOIN customers c ON p.customer_id = c.id
        WHERE 1=1
    """
    params: List[Any] = []
    if customer_id:
        query += " AND p.customer_id = %s"
        params.append(customer_id)
    if status:
        query += " AND p.status = %s"
        params.append(status)
    if teacher:
        query += " AND c.teacher = %s"
        params.append(teacher)
    query += " ORDER BY p.expiry_date ASC"
    cursor.execute(query, params)
    rows = cursor.fetchall()
    release_connection(conn)
    return [dict(r) for r in rows]


def get_package_by_id(package_id: int) -> Optional[Dict[str, Any]]:
    """按ID获取课时包"""
    conn = get_connection()
    cursor = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
    cursor.execute("""
        SELECT p.*, c.name as customer_name,
               (p.total_hours - p.used_hours) as remaining_hours
        FROM course_packages p
        LEFT JOIN customers c ON p.customer_id = c.id
        WHERE p.id = %s
    """, (package_id,))
    row = cursor.fetchone()
    release_connection(conn)
    return dict(row) if row else None


def get_active_packages_for_customers(customer_ids: List[int]) -> Dict[int, List[Dict[str, Any]]]:
    """批量查询多个学员的「进行中」课时包，返回 {customer_id: [package, ...]}，避免逐个学员查询"""
    if not customer_ids:
        return {}
    conn = get_connection()
    cursor = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
    cursor.execute("""
        SELECT p.*, c.name as customer_name, c.lifecycle_stage, c.teacher,
               (p.total_hours - p.used_hours) as remaining_hours
        FROM course_packages p
        LEFT JOIN customers c ON p.customer_id = c.id
        WHERE p.customer_id = ANY(%s) AND p.status = '进行中'
        ORDER BY p.id ASC
    """, (list(customer_ids),))
    rows = cursor.fetchall()
    release_connection(conn)
    result: Dict[int, List[Dict[str, Any]]] = {}
    for r in rows:
        result.setdefault(r["customer_id"], []).append(dict(r))
    return result


def get_customers_hour_balance(customer_ids: List[int]) -> Dict[int, Dict[str, Any]]:
    """批量计算学员课时余额，返回 {customer_id: {remaining_hours, scheduled_hours, available_hours,
    remaining_1v1, remaining_multi, scheduled_1v1, scheduled_multi, available_1v1, available_multi}}：
    - remaining_hours : 剩余课时 = 全部「进行中」课时包剩余合计
    - remaining_1v1   : 1v1 课时包剩余合计；remaining_multi : 1v多 课时包剩余合计
    - scheduled_hours : 已排课时 = 今天 0 点及之后的课表时长 ÷ 单课时时长（班级课表 + 1v1 课表合计）
    - scheduled_1v1   : 其中 1v1（直接挂学员）课表折算；scheduled_multi : 班级课表折算
    - available_hours : 合计可排；available_1v1 / available_multi : 按类型可排（同类型课时包组合使用）
    供排课界面展示「剩余/已排/可排」并限制排课数量。"""
    if not customer_ids:
        return {}
    lesson_minutes = get_lesson_minutes()
    # 已排课时口径：统计今天 0 点及之后的课表（今天 + 未来）。
    # 过去日期课表视为已上完/应通过消课反馈扣课时，不占用「可排」名额，
    # 保证「排课→可排减少，删课→可排加回」即时生效。
    today_start = datetime.now().strftime("%Y-%m-%d 00:00:00")
    conn = get_connection()
    cursor = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
    result: Dict[int, Dict[str, Any]] = {}
    # 1) 剩余课时（全部进行中课时包，按类型拆分）
    cursor.execute("""
        SELECT customer_id,
               COALESCE(SUM(total_hours - used_hours) FILTER (WHERE type = '1v1'), 0)::float AS remaining_1v1,
               COALESCE(SUM(total_hours - used_hours) FILTER (WHERE type = '1v多'), 0)::float AS remaining_multi,
               COALESCE(SUM(total_hours - used_hours), 0)::float AS remaining_hours
        FROM course_packages
        WHERE customer_id = ANY(%s) AND status = '进行中'
        GROUP BY customer_id
    """, (list(customer_ids),))
    for r in cursor.fetchall():
        remaining = round(float(r["remaining_hours"]), 2)
        result[int(r["customer_id"])] = {
            "remaining_hours": remaining,
            "remaining_1v1": round(float(r["remaining_1v1"]), 2),
            "remaining_multi": round(float(r["remaining_multi"]), 2),
            "scheduled_hours": 0.0,
            "scheduled_1v1": 0.0,
            "scheduled_multi": 0.0,
            "available_hours": remaining,
            "available_1v1": round(float(r["remaining_1v1"]), 2),
            "available_multi": round(float(r["remaining_multi"]), 2),
        }
    # 2) 已排课时（今天 0 点及之后的课表：班级课表=1v多，直接挂学员课表=1v1）
    cursor.execute("""
        SELECT customer_id,
               COALESCE(SUM(total_minutes) FILTER (WHERE is_class), 0) AS multi_minutes,
               COALESCE(SUM(total_minutes) FILTER (WHERE NOT is_class), 0) AS one_minutes
        FROM (
            SELECT cs.customer_id, TRUE AS is_class,
                   EXTRACT(EPOCH FROM (s.end_time::timestamp - s.start_time::timestamp)) / 60.0 AS total_minutes
            FROM class_students cs
            JOIN schedules s ON s.class_id = cs.class_id
            WHERE cs.customer_id = ANY(%s) AND s.start_time >= %s
            UNION ALL
            SELECT s.customer_id, FALSE AS is_class,
                   EXTRACT(EPOCH FROM (s.end_time::timestamp - s.start_time::timestamp)) / 60.0 AS total_minutes
            FROM schedules s
            WHERE s.customer_id = ANY(%s) AND s.start_time >= %s
        ) t
        GROUP BY customer_id
    """, (list(customer_ids), today_start, list(customer_ids), today_start))
    for r in cursor.fetchall():
        cid = int(r["customer_id"])
        item = result.setdefault(cid, {
            "remaining_hours": 0.0, "remaining_1v1": 0.0, "remaining_multi": 0.0,
            "scheduled_hours": 0.0, "scheduled_1v1": 0.0, "scheduled_multi": 0.0,
            "available_hours": 0.0, "available_1v1": 0.0, "available_multi": 0.0,
        })
        one = round(float(r["one_minutes"]) / lesson_minutes, 2)
        multi = round(float(r["multi_minutes"]) / lesson_minutes, 2)
        item["scheduled_1v1"] = one
        item["scheduled_multi"] = multi
        item["scheduled_hours"] = round(one + multi, 2)
        item["available_hours"] = max(0.0, round(item["remaining_hours"] - item["scheduled_hours"], 2))
        item["available_1v1"] = max(0.0, round(item["remaining_1v1"] - one, 2))
        item["available_multi"] = max(0.0, round(item["remaining_multi"] - multi, 2))
    release_connection(conn)
    return result


# ==================== 课时包模板 CRUD ====================

def get_course_package_templates(status: str = "", grade: str = "",
                                 pkg_type: str = "") -> List[Dict[str, Any]]:
    """查询课时包模板列表（学员转「在读」时选择报名课时用），可按状态、适用年级、类型筛选
    grade 传入学员年级时，仅返回该年级或未设置年级（通用）的启用模板
    pkg_type 传入「1v1」或「1v多」时，仅返回对应类型的模板"""
    conn = get_connection()
    cursor = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
    query = "SELECT * FROM course_package_templates WHERE 1=1"
    params: List[Any] = []
    if status:
        query += " AND status = %s"
        params.append(status)
    if grade:
        query += " AND (grade = %s OR grade = '' OR grade IS NULL)"
        params.append(grade)
    if pkg_type:
        query += " AND type = %s"
        params.append(pkg_type)
    query += " ORDER BY total_hours ASC, id ASC"
    cursor.execute(query, params)
    rows = cursor.fetchall()
    release_connection(conn)
    return [dict(r) for r in rows]


def add_course_package_template(name: str, total_hours: float, price: float = 0,
                                status: str = "启用", grade: str = "",
                                pkg_type: str = "1v1") -> int:
    """新增课时包模板，名称重复时抛出 UniqueViolation 由调用方处理，返回新模板ID
    grade: 适用年级，留空表示所有年级通用
    pkg_type: 课时包类型（1v1 一对一 / 1v多 一对多）"""
    conn = get_connection()
    cursor = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
    try:
        cursor.execute("""
            INSERT INTO course_package_templates (name, total_hours, price, grade, status, type, created_at, updated_at)
            VALUES (%s, %s, %s, %s, %s, %s, TO_CHAR(NOW(), 'YYYY-MM-DD HH24:MI:SS'), TO_CHAR(NOW(), 'YYYY-MM-DD HH24:MI:SS'))
            RETURNING id
        """, (name, total_hours, price, grade, status, pkg_type))
        new_id = cursor.fetchone()["id"]
        conn.commit()
        return new_id
    except Exception:
        conn.rollback()
        raise
    finally:
        release_connection(conn)


def delete_course_package_template(template_id: int) -> bool:
    """删除课时包模板（单事务）"""
    conn = get_connection()
    try:
        conn.cursor().execute("DELETE FROM course_package_templates WHERE id = %s", (template_id,))
        conn.commit()
        return True
    except Exception:
        conn.rollback()
        raise
    finally:
        release_connection(conn)


# ==================== 消课记录 CRUD ====================

def _refresh_customer_stage_by_hours(cursor, customer_id: int, renew: bool = False):
    """根据客户全部「进行中」课时包的剩余课时，同步其生命周期阶段（在调用方事务内执行，不负责 commit）。
    与消课流程共用阈值：剩余 ≤ 0 → 流失；0 < 剩余 < 10 → 待续费；剩余 ≥ 10 → 在读。
    renew=False（消课后调用）：仅做降级校正——归零转「流失」（限在读/待续费）、
       课时不足转「待续费」（仅限在读），课时充足时不干预，保留人工设置的状态。
    renew=True（续费后调用）：强制校正——课时充足转回「在读」、不足转「待续费」、
       归零转「流失」（均限在读/待续费，不覆盖其他人工阶段）。"""
    cursor.execute("""
        SELECT COALESCE(SUM(total_hours - used_hours), 0)::float AS remaining
        FROM course_packages
        WHERE customer_id = %s AND status = '进行中'
    """, (customer_id,))
    row = cursor.fetchone()
    if row is None:
        return
    remaining = float(row["remaining"])
    if remaining <= 0:
        new_stage, scope = "流失", "('在读', '待续费')"
    elif remaining < 10:
        new_stage = "待续费"
        scope = "('在读', '待续费')" if renew else "('在读',)"
    else:
        if not renew:
            return  # 消课场景课时充足时不干预，保留人工状态
        new_stage, scope = "在读", "('在读', '待续费')"
    cursor.execute(f"""
        UPDATE customers SET lifecycle_stage = %s,
            updated_at = TO_CHAR(NOW(), 'YYYY-MM-DD HH24:MI:SS')
        WHERE id = %s AND lifecycle_stage IN {scope}
    """, (new_stage, customer_id))


def _insert_course_record(cursor, package_id: int, customer_id: int, record_date: str,
                          hours_used: float, course_type: str, teacher: str,
                          notes: str, schedule_id: int = 0) -> int:
    """在指定 cursor 上执行消课的全部 SQL（写记录 + 更新课时包 + 状态 + 生命周期）。
    不负责 commit/rollback，由调用方统一管理事务——
    这样手动消课（add_course_record）与课堂反馈批量扣减（auto_consume_hours_by_feedback）
    可以共享同一段核心逻辑，且批量扣减时全班学员在同一个事务内原子提交/回滚。"""
    # 1. 写入消课记录
    cursor.execute("""
        INSERT INTO course_records (package_id, customer_id, record_date, hours_used,
            course_type, teacher, notes, schedule_id, created_at)
        VALUES (%s, %s, %s, %s, %s, %s, %s, %s, TO_CHAR(NOW(), 'YYYY-MM-DD HH24:MI:SS'))
        RETURNING id
    """, (package_id, customer_id, record_date, hours_used, course_type, teacher, notes,
          schedule_id if schedule_id else None))
    record_id = cursor.fetchone()["id"]

    # 2. 更新课时包已消耗课时（乐观锁：条件更新防超扣。
    #    WHERE 限定 used_hours + 本次 <= total_hours，若并发导致剩余不足
    #    或课时包已被删除则影响 0 行，抛错由调用方回滚/降级。）
    cursor.execute("""
        UPDATE course_packages SET used_hours = used_hours + %s,
            updated_at = TO_CHAR(NOW(), 'YYYY-MM-DD HH24:MI:SS')
        WHERE id = %s AND used_hours + %s <= total_hours + 0.0001
    """, (hours_used, package_id, hours_used))
    if cursor.rowcount == 0:
        raise RuntimeError(f"课时包剩余课时不足（本次需扣 {hours_used} 节）")

    # 3. 检查是否用完，自动更新状态
    cursor.execute(
        "SELECT total_hours, used_hours FROM course_packages WHERE id = %s", (package_id,)
    )
    pkg = cursor.fetchone()
    if pkg and pkg["used_hours"] >= pkg["total_hours"]:
        cursor.execute(
            "UPDATE course_packages SET status = '已用完', updated_at = TO_CHAR(NOW(), 'YYYY-MM-DD HH24:MI:SS') WHERE id = %s",
            (package_id,)
        )

    # 4. 课时变化即时更新生命周期（消课瞬间触发，与续费共用同一套阈值判定）
    _refresh_customer_stage_by_hours(cursor, customer_id)
    return record_id


def refresh_customer_stage_by_hours(customer_id: int) -> str:
    """续费完成后调用：按全部「进行中」课时包剩余课时校正学员生命周期阶段（单事务）。
    剩余 ≥ 10 → 在读；0 < 剩余 < 10 → 待续费；剩余 ≤ 0 → 流失（仅针对在读/待续费学员）。
    返回校正后的阶段名（学员不存在时返回空串）。"""
    conn = get_connection()
    cursor = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
    try:
        _refresh_customer_stage_by_hours(cursor, customer_id, renew=True)
        conn.commit()
        cursor.execute("SELECT lifecycle_stage FROM customers WHERE id = %s", (customer_id,))
        row = cursor.fetchone()
        return row["lifecycle_stage"] if row else ""
    except Exception:
        conn.rollback()
        raise
    finally:
        release_connection(conn)


def add_course_record(package_id: int, customer_id: int, record_date: str,
                       hours_used: float = 1, course_type: str = "1v1",
                       teacher: str = "", notes: str = "",
                       schedule_id: int = 0) -> int:
    """新增消课记录，同时自动更新课时包的 used_hours（单事务）
    schedule_id: 关联课表 ID（课堂反馈自动扣课时时传入，用于防重复扣减；0 表示手动消课）
    """
    conn = get_connection()
    cursor = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
    try:
        record_id = _insert_course_record(
            cursor, package_id, customer_id, record_date, hours_used,
            course_type, teacher, notes, schedule_id,
        )
        conn.commit()
        return record_id
    except Exception:
        conn.rollback()
        raise
    finally:
        release_connection(conn)


def delete_course_record(record_id: int) -> bool:
    """删除消课记录，同时退回课时包的 used_hours（单事务）"""
    conn = get_connection()
    cursor = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
    try:
        # 获取记录信息
        cursor.execute(
            "SELECT package_id, hours_used FROM course_records WHERE id = %s", (record_id,)
        )
        record = cursor.fetchone()
        if not record:
            conn.rollback()
            return False

        # 退回课时（PostgreSQL 用 GREATEST 替代 SQLite 的 MAX）
        cursor.execute("""
            UPDATE course_packages SET used_hours = GREATEST(0, used_hours - %s),
                status = CASE WHEN status = '已用完' THEN '进行中' ELSE status END,
                updated_at = TO_CHAR(NOW(), 'YYYY-MM-DD HH24:MI:SS')
            WHERE id = %s
        """, (record["hours_used"], record["package_id"]))

        # 删除记录
        cursor.execute("DELETE FROM course_records WHERE id = %s", (record_id,))
        conn.commit()
        return True
    except Exception:
        conn.rollback()
        raise
    finally:
        release_connection(conn)


def get_course_records(package_id: int = 0, customer_id: int = 0,
                        date_from: str = "", date_to: str = "", teacher: str = "") -> List[Dict[str, Any]]:
    """查询消课记录，支持学管过滤"""
    conn = get_connection()
    cursor = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
    query = """
        SELECT r.*, p.package_name, c.name as customer_name, c.teacher
        FROM course_records r
        LEFT JOIN course_packages p ON r.package_id = p.id
        LEFT JOIN customers c ON r.customer_id = c.id
        WHERE 1=1
    """
    params: List[Any] = []
    if package_id:
        query += " AND r.package_id = %s"
        params.append(package_id)
    if customer_id:
        query += " AND r.customer_id = %s"
        params.append(customer_id)
    if date_from:
        query += " AND r.record_date >= %s"
        params.append(date_from)
    if date_to:
        query += " AND r.record_date <= %s"
        params.append(date_to)
    if teacher:
        query += " AND c.teacher = %s"
        params.append(teacher)
    query += " ORDER BY r.record_date DESC, r.created_at DESC"
    cursor.execute(query, params)
    rows = cursor.fetchall()
    release_connection(conn)
    return [dict(r) for r in rows]


# ==================== 续费倒计时 & 课时统计 ====================

def get_renewal_alerts(teacher: str = "") -> List[Dict[str, Any]]:
    """
    续费倒计时预警
    返回即将到期（30天内）或即将用完（剩余<5课时）的课时包列表
    包含：剩余天数、剩余课时、预警等级，支持学管过滤

    PostgreSQL 日期差计算：
        (expiry_date::date - today::date) 返回整数天数
        等价于原 SQLite 的 CAST(julianday(...) - julianday(...) AS INTEGER)
    """
    today = datetime.now().strftime("%Y-%m-%d")
    conn = get_connection()
    cursor = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
    query = """
        SELECT p.*, c.name as customer_name, c.phone, c.lifecycle_stage, c.teacher,
               (p.total_hours - p.used_hours) as remaining_hours,
               (p.expiry_date::date - %s::date) as days_left
        FROM course_packages p
        LEFT JOIN customers c ON p.customer_id = c.id
        WHERE p.status = '进行中'
          AND (
              (p.expiry_date::date - %s::date) <= 30
              OR (p.total_hours - p.used_hours) <= 5
          )
    """
    params: List[Any] = [today, today]
    if teacher:
        query += " AND c.teacher = %s"
        params.append(teacher)
    query += " ORDER BY days_left ASC, remaining_hours ASC"
    cursor.execute(query, params)
    rows = cursor.fetchall()
    release_connection(conn)

    alerts = []
    for row in rows:
        r = dict(row)
        days = r["days_left"]
        remain = r["remaining_hours"]

        # 预警等级
        if days <= 0:
            r["alert_level"] = "🔴 已过期"
        elif days <= 7 or remain <= 1:
            r["alert_level"] = "🟠 紧急"
        elif days <= 30 or remain <= 5:
            r["alert_level"] = "🟡 提醒"
        else:
            r["alert_level"] = "🟢 正常"

        alerts.append(r)
    return alerts


def get_all_course_packages_for_renewal(teacher: str = "") -> List[Dict[str, Any]]:
    """获取所有进行中的课时包及其续费信息（用于续费倒计时面板），支持学管过滤"""
    today = datetime.now().strftime("%Y-%m-%d")
    conn = get_connection()
    cursor = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
    query = """
        SELECT p.*, c.name as customer_name, c.phone, c.lifecycle_stage, c.teacher,
               (p.total_hours - p.used_hours) as remaining_hours,
               (p.expiry_date::date - %s::date) as days_left
        FROM course_packages p
        LEFT JOIN customers c ON p.customer_id = c.id
        WHERE p.status = '进行中'
    """
    params: List[Any] = [today]
    if teacher:
        query += " AND c.teacher = %s"
        params.append(teacher)
    query += " ORDER BY days_left ASC, remaining_hours ASC"
    cursor.execute(query, params)
    rows = cursor.fetchall()
    release_connection(conn)
    return [dict(r) for r in rows]


def get_course_hours_statistics(teacher: str = "") -> Dict[str, Any]:
    """课时总览统计，支持学管过滤"""
    conn = get_connection()
    cursor = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
    query = """
        SELECT
            COUNT(*) as total_packages,
            COALESCE(SUM(p.total_hours), 0) as total_hours,
            COALESCE(SUM(p.used_hours), 0) as total_used,
            COALESCE(SUM(p.total_hours - p.used_hours), 0) as total_remaining,
            COUNT(*) FILTER (WHERE p.status = '进行中') as active_packages,
            COUNT(*) FILTER (WHERE p.status = '已用完') as finished_packages,
            COUNT(*) FILTER (WHERE p.status = '已到期') as expired_packages
        FROM course_packages p
        LEFT JOIN customers c ON p.customer_id = c.id
        WHERE 1=1
    """
    params: List[Any] = []
    if teacher:
        query += " AND c.teacher = %s"
        params.append(teacher)
    cursor.execute(query, params)
    row = cursor.fetchone()
    release_connection(conn)
    return dict(row) if row else {}


def get_monthly_course_records(year: int = 0, month: int = 0, teacher: str = "") -> List[Dict[str, Any]]:
    """按月查询消课记录汇总（用 TO_CHAR 做年月匹配），支持学管过滤"""
    if not year:
        year = datetime.now().year
    if not month:
        month = datetime.now().month
    date_prefix = f"{year}-{month:02d}"
    conn = get_connection()
    cursor = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
    query = """
        SELECT r.record_date, SUM(r.hours_used) as daily_hours, COUNT(*) as record_count
        FROM course_records r
        LEFT JOIN customers c ON r.customer_id = c.id
        WHERE TO_CHAR(r.record_date::date, 'YYYY-MM') = %s
    """
    params: List[Any] = [date_prefix]
    if teacher:
        query += " AND c.teacher = %s"
        params.append(teacher)
    query += " GROUP BY r.record_date ORDER BY r.record_date DESC"
    cursor.execute(query, params)
    rows = cursor.fetchall()
    release_connection(conn)
    return [dict(r) for r in rows]


# ==================== 统计查询 ====================

def get_stage_statistics(teacher: str = "") -> Dict[str, int]:
    """获取各生命周期阶段的客户数量（用于漏斗图），支持学管过滤"""
    conn = get_connection()
    cursor = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
    query = "SELECT lifecycle_stage, COUNT(*) as cnt FROM customers WHERE 1=1"
    params: List[Any] = []
    if teacher:
        query += " AND teacher = %s"
        params.append(teacher)
    query += " GROUP BY lifecycle_stage"
    cursor.execute(query, params)
    rows = cursor.fetchall()
    release_connection(conn)
    result = {stage: 0 for stage in LIFECYCLE_STAGES}
    for row in rows:
        stage = row["lifecycle_stage"]
        if stage in result:
            result[stage] = row["cnt"]
    return result


def get_fruit_statistics(teacher: str = "") -> Dict[str, int]:
    """获取各苹果意向的客户数量，支持学管过滤"""
    conn = get_connection()
    cursor = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
    query = "SELECT intent_fruit, COUNT(*) as cnt FROM customers WHERE 1=1"
    params: List[Any] = []
    if teacher:
        query += " AND teacher = %s"
        params.append(teacher)
    query += " GROUP BY intent_fruit"
    cursor.execute(query, params)
    rows = cursor.fetchall()
    release_connection(conn)
    result = {f: 0 for f in INTENT_FRUIT_OPTIONS}
    for row in rows:
        fruit = row["intent_fruit"]
        if fruit in result:
            result[fruit] = row["cnt"]
    return result


def get_source_statistics(teacher: str = "") -> Dict[str, int]:
    """获取各来源渠道的客户数量，支持学管过滤"""
    conn = get_connection()
    cursor = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
    query = "SELECT source, COUNT(*) as cnt FROM customers WHERE 1=1"
    params: List[Any] = []
    if teacher:
        query += " AND teacher = %s"
        params.append(teacher)
    query += " GROUP BY source"
    cursor.execute(query, params)
    rows = cursor.fetchall()
    release_connection(conn)
    return {row["source"]: row["cnt"] for row in rows}


def get_total_customers(teacher: str = "") -> int:
    """获取客户总数，支持学管过滤"""
    conn = get_connection()
    cursor = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
    query = "SELECT COUNT(*) as cnt FROM customers WHERE 1=1"
    params: List[Any] = []
    if teacher:
        query += " AND teacher = %s"
        params.append(teacher)
    cursor.execute(query, params)
    row = cursor.fetchone()
    release_connection(conn)
    return row["cnt"] if row else 0


def get_pending_follow_ups_count(teacher: str = "") -> int:
    """获取待跟进数量，支持学管过滤"""
    conn = get_connection()
    cursor = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
    query = """
        SELECT COUNT(*) as cnt FROM follow_ups f
        LEFT JOIN customers c ON f.customer_id = c.id
        WHERE f.status = '待跟进'
    """
    params: List[Any] = []
    if teacher:
        query += " AND c.teacher = %s"
        params.append(teacher)
    cursor.execute(query, params)
    row = cursor.fetchone()
    release_connection(conn)
    return row["cnt"] if row else 0


def get_stale_customers(days: int = 5, teacher: str = "") -> List[Dict[str, Any]]:
    """
    获取超过指定天数未跟进的客户（含从未跟进过的客户），支持学管过滤
    单次查询，所有聚合和过滤在数据库端完成，仅返回必要字段，减少网络传输
    以跟进记录的 updated_at 作为"上次跟进时间"判断依据；
    无跟进记录时以客户 updated_at 为回退基准（而非 created_at）

    返回字段：id, name, phone, lifecycle_stage,
            updated_at, last_follow_time, days_since_touch
    """
    conn = get_connection()
    cursor = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
    query = """
        SELECT
            c.id, c.name, c.phone, c.lifecycle_stage, c.teacher,
            c.updated_at,
            COALESCE(max_fu, '从未跟进') as last_follow_time,
            CURRENT_DATE - COALESCE(max_fu_date, c.updated_at::date) as days_since_touch
        FROM customers c
        LEFT JOIN (
            SELECT customer_id,
                   MAX(updated_at) as max_fu,
                   MAX(updated_at::date) as max_fu_date
            FROM follow_ups
            GROUP BY customer_id
        ) fu ON c.id = fu.customer_id
        WHERE COALESCE(max_fu_date, c.updated_at::date) < CURRENT_DATE - %s
    """
    params: List[Any] = [days]
    if teacher:
        query += " AND c.teacher = %s"
        params.append(teacher)
    query += " ORDER BY days_since_touch DESC NULLS LAST"
    cursor.execute(query, params)
    rows = cursor.fetchall()
    release_connection(conn)
    return [dict(r) for r in rows]


# ==================== 课程 CRUD ====================

def add_course(name: str, description: str = "") -> bool:
    """新增课程，课程名重复时返回 False"""
    conn = get_connection()
    cursor = conn.cursor()
    try:
        cursor.execute("""
            INSERT INTO courses (name, description, created_at)
            VALUES (%s, %s, TO_CHAR(NOW(), 'YYYY-MM-DD HH24:MI:SS'))
        """, (name, description))
        conn.commit()
        _cache_clear("all_courses")
        return True
    except psycopg2.errors.UniqueViolation:
        conn.rollback()
        return False
    except Exception:
        conn.rollback()
        raise
    finally:
        release_connection(conn)


def delete_course(course_id: int) -> bool:
    """删除课程（级联删除关联班级、班级学员、课表；单事务）"""
    conn = get_connection()
    try:
        conn.cursor().execute("DELETE FROM courses WHERE id = %s", (course_id,))
        conn.commit()
        _cache_clear("all_courses")
        return True
    except Exception:
        conn.rollback()
        raise
    finally:
        release_connection(conn)


def get_all_courses() -> List[Dict[str, Any]]:
    """查询全部课程及关联班级数量——结果按 TTL 缓存，课程写操作后自动失效"""
    cached = _cache_get("all_courses")
    if cached is not None:
        return cached
    conn = get_connection()
    cursor = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
    cursor.execute("""
        SELECT co.id, co.name, co.description, co.created_at,
               COUNT(cl.id) as class_count
        FROM courses co
        LEFT JOIN classes cl ON co.id = cl.course_id
        GROUP BY co.id
        ORDER BY co.created_at DESC, co.id DESC
    """)
    rows = cursor.fetchall()
    release_connection(conn)
    result = [dict(r) for r in rows]
    _cache_set("all_courses", result)
    return result


def get_course_by_id(course_id: int) -> Optional[Dict[str, Any]]:
    conn = get_connection()
    cursor = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
    cursor.execute("SELECT * FROM courses WHERE id = %s", (course_id,))
    row = cursor.fetchone()
    release_connection(conn)
    return dict(row) if row else None


# ==================== 班级 CRUD ====================

def add_class(name: str, class_type: str = "1v多", course_id: int = 0, teacher: str = "",
              manager: str = "", max_students: int = 0, status: str = "进行中",
              start_date: str = "", notes: str = "") -> int:
    """新增班级，返回班级 ID；course_id 可空（课程体系已废弃，班级名即课程名）"""
    conn = get_connection()
    cursor = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
    try:
        cursor.execute("""
            INSERT INTO classes (name, class_type, course_id, teacher, manager,
                                 max_students, status, start_date, notes, created_at, updated_at)
            VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s,
                    TO_CHAR(NOW(), 'YYYY-MM-DD HH24:MI:SS'),
                    TO_CHAR(NOW(), 'YYYY-MM-DD HH24:MI:SS'))
            RETURNING id
        """, (name, class_type, course_id or None, teacher, manager,
              max_students, status, start_date, notes))
        new_id = cursor.fetchone()["id"]
        conn.commit()
        return new_id
    except Exception:
        conn.rollback()
        raise
    finally:
        release_connection(conn)


def update_class(class_id: int, **kwargs) -> bool:
    """更新班级信息（字段名受白名单约束，updated_at 由数据库 NOW() 自动设置；单事务）"""
    if not kwargs:
        return False
    data = _filter_update_fields("classes", kwargs)
    if not data:
        return False
    set_clause = ", ".join([f"{k} = %s" for k in data.keys()])
    values = list(data.values()) + [class_id]
    conn = get_connection()
    try:
        conn.cursor().execute(
            f"UPDATE classes SET {set_clause}, updated_at = TO_CHAR(NOW(), 'YYYY-MM-DD HH24:MI:SS') WHERE id = %s",
            values,
        )
        conn.commit()
        return True
    except Exception:
        conn.rollback()
        raise
    finally:
        release_connection(conn)


def delete_class(class_id: int) -> bool:
    """删除班级（级联删除班级学员，课表 class_id 置空；单事务）"""
    conn = get_connection()
    try:
        conn.cursor().execute("DELETE FROM classes WHERE id = %s", (class_id,))
        conn.commit()
        return True
    except Exception:
        conn.rollback()
        raise
    finally:
        release_connection(conn)


def get_all_classes(class_type: str = "", course_id: int = 0, teacher: str = "",
                    manager: str = "", status: str = "",
                    viewer_role: str = "", viewer_name: str = "") -> List[Dict[str, Any]]:
    """查询班级列表
    teacher: 按任课教师用户名筛选；manager: 按学管用户名筛选
    viewer_role/viewer_name: 权限范围，staff 仅看自己管理的班级，teacher 仅看自己任课的班级
    """
    conn = get_connection()
    cursor = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
    query = """
        SELECT cl.*, co.name as course_name,
               (SELECT COUNT(*) FROM class_students cs WHERE cs.class_id = cl.id) as student_count
        FROM classes cl
        LEFT JOIN courses co ON cl.course_id = co.id
        WHERE 1=1
    """
    params: List[Any] = []
    if class_type:
        query += " AND cl.class_type = %s"
        params.append(class_type)
    if course_id:
        query += " AND cl.course_id = %s"
        params.append(course_id)
    if teacher:
        query += " AND cl.teacher = %s"
        params.append(teacher)
    if manager:
        query += " AND cl.manager = %s"
        params.append(manager)
    if status:
        query += " AND cl.status = %s"
        params.append(status)
    if viewer_role == "staff":
        query += " AND cl.manager = %s"
        params.append(viewer_name)
    elif viewer_role == "teacher":
        query += " AND cl.teacher = %s"
        params.append(viewer_name)
    query += " ORDER BY cl.created_at DESC, cl.id DESC"
    cursor.execute(query, params)
    rows = cursor.fetchall()
    release_connection(conn)
    return [dict(r) for r in rows]


def get_class_by_id(class_id: int) -> Optional[Dict[str, Any]]:
    conn = get_connection()
    cursor = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
    cursor.execute("""
        SELECT cl.*, co.name as course_name
        FROM classes cl
        LEFT JOIN courses co ON cl.course_id = co.id
        WHERE cl.id = %s
    """, (class_id,))
    row = cursor.fetchone()
    release_connection(conn)
    return dict(row) if row else None


# ==================== 班级学员 CRUD ====================

def add_class_student(class_id: int, customer_id: int) -> bool:
    """往班级添加学员（重复添加自动忽略；单事务）"""
    conn = get_connection()
    cursor = conn.cursor()
    try:
        cursor.execute("""
            INSERT INTO class_students (class_id, customer_id, joined_at)
            VALUES (%s, %s, TO_CHAR(NOW(), 'YYYY-MM-DD HH24:MI:SS'))
            ON CONFLICT (class_id, customer_id) DO NOTHING
        """, (class_id, customer_id))
        conn.commit()
        return True
    except Exception:
        conn.rollback()
        raise
    finally:
        release_connection(conn)


def remove_class_student(class_id: int, customer_id: int) -> bool:
    """将学员移出班级（单事务）"""
    conn = get_connection()
    try:
        conn.cursor().execute(
            "DELETE FROM class_students WHERE class_id = %s AND customer_id = %s",
            (class_id, customer_id),
        )
        conn.commit()
        return True
    except Exception:
        conn.rollback()
        raise
    finally:
        release_connection(conn)


def get_class_students(class_id: int) -> List[Dict[str, Any]]:
    """查询班级学员（含学员基本信息）"""
    conn = get_connection()
    cursor = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
    cursor.execute("""
        SELECT cs.id, cs.customer_id, cs.joined_at,
               c.name, c.phone, c.school, c.grade, c.teacher as c_teacher
        FROM class_students cs
        LEFT JOIN customers c ON cs.customer_id = c.id
        WHERE cs.class_id = %s
        ORDER BY cs.id ASC
    """, (class_id,))
    rows = cursor.fetchall()
    release_connection(conn)
    return [dict(r) for r in rows]


def get_customer_classes(customer_id: int) -> List[Dict[str, Any]]:
    """查询客户所在班级"""
    conn = get_connection()
    cursor = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
    cursor.execute("""
        SELECT cl.id, cl.name, cl.class_type, cl.status
        FROM class_students cs
        LEFT JOIN classes cl ON cs.class_id = cl.id
        WHERE cs.customer_id = %s
        ORDER BY cl.created_at DESC
    """, (customer_id,))
    rows = cursor.fetchall()
    release_connection(conn)
    return [dict(r) for r in rows]


# ==================== 课表 CRUD ====================

def add_schedule(class_id: int = 0, course_id: int = 0, title: str = "", teacher: str = "",
                 start_time: str = "", end_time: str = "", location: str = "",
                 notes: str = "", customer_id: int = 0,
                 package_id: int = 0) -> int:
    """新增课表记录，返回课表 ID。
    class_id 挂班级（1v多）；customer_id 直接挂学员（1v1，无需课程/班级）。
    package_id: 排课时指定的课时包（1v1 可选；0 表示自动组合同类型课时包）。"""
    conn = get_connection()
    cursor = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
    try:
        cursor.execute("""
            INSERT INTO schedules (class_id, course_id, customer_id, title, teacher,
                                   start_time, end_time, location, notes, package_id, created_at)
            VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s,
                    TO_CHAR(NOW(), 'YYYY-MM-DD HH24:MI:SS'))
            RETURNING id
        """, (class_id or None, course_id or None, customer_id or None, title, teacher,
              start_time, end_time, location, notes, package_id or None))
        new_id = cursor.fetchone()["id"]
        conn.commit()
        return new_id
    except Exception:
        conn.rollback()
        raise
    finally:
        release_connection(conn)


def add_schedules_bulk(schedules: List[Dict[str, Any]]) -> int:
    """批量新增课表记录（单事务 execute_batch），返回成功条数。

    每条记录字段与 add_schedule 一致：class_id, course_id, customer_id, title, teacher,
    start_time, end_time, location, notes, package_id（排课指定课时包，0/缺省表示自动组合）
    """
    if not schedules:
        return 0
    conn = get_connection()
    try:
        cursor = conn.cursor()
        data = [
            (
                s.get("class_id") or None,
                s.get("course_id") or None,
                s.get("customer_id") or None,
                s.get("title") or "",
                s.get("teacher") or "",
                s.get("start_time"),
                s.get("end_time"),
                s.get("location") or "",
                s.get("notes") or "",
                s.get("package_id") or None,
            )
            for s in schedules
        ]
        psycopg2.extras.execute_batch(
            cursor,
            """INSERT INTO schedules (class_id, course_id, customer_id, title, teacher,
                                     start_time, end_time, location, notes, package_id, created_at)
               VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s,
                       TO_CHAR(NOW(), 'YYYY-MM-DD HH24:MI:SS'))""",
            data,
        )
        conn.commit()
        return len(data)
    except Exception:
        conn.rollback()
        raise
    finally:
        release_connection(conn)


def delete_schedule(schedule_id: int) -> bool:
    """删除课表记录（单事务）"""
    conn = get_connection()
    try:
        conn.cursor().execute("DELETE FROM schedules WHERE id = %s", (schedule_id,))
        conn.commit()
        return True
    except Exception:
        conn.rollback()
        raise
    finally:
        release_connection(conn)


def get_schedules(date_from: str = "", date_to: str = "", teacher: str = "",
                  class_type: str = "", manager: str = "", title: str = "",
                  viewer_role: str = "", viewer_name: str = "",
                  staff_scope: str = "") -> List[Dict[str, Any]]:
    """查询课表，支持按日期范围、任课教师、班型、学管、课程标题模糊筛选
    title: 课程标题关键字（ILIKE 模糊匹配）
    viewer_role/viewer_name: 权限范围，teacher 仅看自己授课课表；staff 可查看全部课表（只读权限由页面控制）
    staff_scope: 学管数据范围，传学管用户名后仅返回其名下学员课表
                 （1v1 按归属学管 customers.teacher，班级课表按班级学管 classes.manager，OR 关系）
    """
    conn = get_connection()
    cursor = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
    query = """
        SELECT s.*, co.name as course_name,
               cl.name as class_name, cl.class_type, cl.manager,
               cu.name as customer_name,
               COALESCE(cl.class_type, '1v1') as sched_kind
        FROM schedules s
        LEFT JOIN classes cl ON s.class_id = cl.id
        LEFT JOIN courses co ON s.course_id = co.id
        LEFT JOIN customers cu ON s.customer_id = cu.id
        WHERE 1=1
    """
    params: List[Any] = []
    if date_from:
        query += " AND s.start_time >= %s"
        params.append(date_from)
    if date_to:
        query += " AND s.start_time <= %s"
        params.append(date_to + " 23:59:59")
    if teacher:
        query += " AND s.teacher = %s"
        params.append(teacher)
    if class_type:
        query += " AND COALESCE(cl.class_type, '1v1') = %s"
        params.append(class_type)
    if manager:
        query += " AND cl.manager = %s"
        params.append(manager)
    if title:
        query += " AND s.title ILIKE %s"
        params.append(f"%{title}%")
    if viewer_role == "teacher":
        query += " AND s.teacher = %s"
        params.append(viewer_name)
    if staff_scope:
        query += " AND (cl.manager = %s OR cu.teacher = %s)"
        params.extend([staff_scope, staff_scope])
    query += " ORDER BY s.start_time ASC, s.id ASC"
    cursor.execute(query, params)
    rows = cursor.fetchall()
    release_connection(conn)
    return [dict(r) for r in rows]


# ==================== 课堂反馈 ====================

def get_all_schedule_feedback() -> Dict[int, str]:
    """查询全部课堂反馈，返回 {schedule_id: content}"""
    conn = get_connection()
    cursor = conn.cursor()
    cursor.execute("SELECT schedule_id, content FROM schedule_feedback")
    rows = cursor.fetchall()
    release_connection(conn)
    return {sid: content for sid, content in rows}


def get_schedule_feedback(schedule_id: int) -> str:
    """查询单节课的课堂反馈内容"""
    conn = get_connection()
    cursor = conn.cursor()
    cursor.execute("SELECT content FROM schedule_feedback WHERE schedule_id = %s", (schedule_id,))
    row = cursor.fetchone()
    release_connection(conn)
    return row[0] if row else ""


def save_schedule_feedback(schedule_id: int, teacher: str, content: str) -> bool:
    """保存课堂反馈（已存在则更新；单事务）"""
    conn = get_connection()
    cursor = conn.cursor()
    try:
        cursor.execute("""
            INSERT INTO schedule_feedback (schedule_id, teacher, content, created_at, updated_at)
            VALUES (%s, %s, %s, TO_CHAR(NOW(), 'YYYY-MM-DD HH24:MI:SS'), TO_CHAR(NOW(), 'YYYY-MM-DD HH24:MI:SS'))
            ON CONFLICT (schedule_id) DO UPDATE SET
                content = EXCLUDED.content,
                teacher = EXCLUDED.teacher,
                updated_at = TO_CHAR(NOW(), 'YYYY-MM-DD HH24:MI:SS')
        """, (schedule_id, teacher, content))
        conn.commit()
        return True
    except Exception:
        conn.rollback()
        raise
    finally:
        release_connection(conn)


# ==================== 全局设置（单课时时长等） ====================

def get_setting(key: str, default: str = "") -> str:
    """读取全局设置项，不存在时返回默认值"""
    conn = get_connection()
    cursor = conn.cursor()
    cursor.execute("SELECT value FROM app_settings WHERE key = %s", (key,))
    row = cursor.fetchone()
    release_connection(conn)
    return row[0] if row else default


def set_setting(key: str, value: str) -> None:
    """写入全局设置项（UPSERT；单事务）"""
    conn = get_connection()
    cursor = conn.cursor()
    try:
        cursor.execute("""
            INSERT INTO app_settings (key, value) VALUES (%s, %s)
            ON CONFLICT (key) DO UPDATE SET value = EXCLUDED.value
        """, (key, value))
        conn.commit()
        if key == "lesson_minutes":
            _cache_clear("lesson_minutes")
    except Exception:
        conn.rollback()
        raise
    finally:
        release_connection(conn)


def get_lesson_minutes() -> int:
    """单课时时长（分钟），默认 60——结果按 TTL 缓存，修改设置后自动失效"""
    cached = _cache_get("lesson_minutes")
    if cached is not None:
        return cached
    try:
        value = max(1, int(float(get_setting("lesson_minutes", "60"))))
    except (TypeError, ValueError):
        value = 60
    _cache_set("lesson_minutes", value)
    return value


def get_schedule_by_id(schedule_id: int) -> Optional[Dict[str, Any]]:
    """按ID查询单节课（含课程/班级信息）"""
    conn = get_connection()
    cursor = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
    cursor.execute("""
        SELECT s.*, co.name as course_name,
               cl.name as class_name, cl.class_type, cl.manager,
               cu.name as customer_name,
               COALESCE(cl.class_type, '1v1') as sched_kind
        FROM schedules s
        LEFT JOIN classes cl ON s.class_id = cl.id
        LEFT JOIN courses co ON s.course_id = co.id
        LEFT JOIN customers cu ON s.customer_id = cu.id
        WHERE s.id = %s
    """, (schedule_id,))
    row = cursor.fetchone()
    release_connection(conn)
    return dict(row) if row else None


def auto_consume_hours_by_feedback(schedule_id: int) -> Dict[str, Any]:
    """课堂反馈提交后，按课表时长自动扣减学员课时：
    1. 课时数 = 课表时长（分钟）÷ 单课时时长（分钟）
    2. 对课表关联班级的全部学员，从各自"进行中"的课时包中扣减
    3. 防重复：同一课表只自动扣减一次（以 course_records.schedule_id 为凭据）
    返回结果 dict：
    {"ok": bool, "message": str, "minutes": int, "hours": float,
     "customers": [{"name": str, "package": str, "hours": float}],
     "skipped": [{"name": str, "reason": str}]}
    """
    s = get_schedule_by_id(schedule_id)
    if not s:
        return {"ok": False, "message": "课表不存在", "minutes": 0, "hours": 0,
                "customers": [], "skipped": []}

    # 时长计算
    try:
        start_dt = datetime.strptime((s.get("start_time") or "")[:16], "%Y-%m-%d %H:%M")
        end_dt = datetime.strptime((s.get("end_time") or "")[:16], "%Y-%m-%d %H:%M")
    except (ValueError, TypeError):
        return {"ok": False, "message": "课表时间格式有误，无法计算课时", "minutes": 0, "hours": 0,
                "customers": [], "skipped": []}
    minutes = int((end_dt - start_dt).total_seconds() // 60)
    if minutes <= 0:
        return {"ok": False, "message": "课表结束时间须晚于开始时间", "minutes": 0, "hours": 0,
                "customers": [], "skipped": []}
    lesson_minutes = get_lesson_minutes()
    hours = round(minutes / lesson_minutes, 2)
    if hours <= 0:
        return {"ok": False, "message": "计算课时为 0，无需扣减", "minutes": minutes, "hours": 0,
                "customers": [], "skipped": []}

    # 扣减对象：1v1 课表直接挂学员；1v多 课表挂班级，扣全班学员
    class_id = s.get("class_id")
    customer_id = s.get("customer_id")
    if class_id:
        students = get_class_students(class_id)
    elif customer_id:
        stu = get_customer_by_id(customer_id)
        students = [{"customer_id": customer_id, "name": stu.get("name", "") if stu else ""}]
    else:
        return {"ok": False, "message": "该课表未关联班级或学员，无法自动扣减课时",
                "minutes": minutes, "hours": hours, "customers": [], "skipped": []}
    if not students:
        return {"ok": False, "message": "未找到可扣减的学员，未自动扣减课时",
                "minutes": minutes, "hours": hours, "customers": [], "skipped": []}

    record_date = (s.get("start_time") or "")[:10]
    course_type = s.get("class_type") or "1v1"
    teacher = s.get("teacher") or ""
    title = s.get("title") or ""
    notes = f"课堂反馈自动消课：{title}"

    customers: List[Dict[str, Any]] = []
    skipped: List[Dict[str, Any]] = []
    # 批量获取全部学员的「进行中」课时包，避免逐学员查询
    pkg_map = get_active_packages_for_customers([stu.get("customer_id") for stu in students])
    prefer_pkg_id = s.get("package_id") or 0  # 排课时指定的课时包（0=自动组合）

    # 单事务：防重复检查 + 全班学员扣减在同一个事务中执行，
    # 任一学员扣减失败则整体回滚，避免"部分学员已扣、部分未扣"的数据不一致。
    conn = get_connection()
    cursor = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
    try:
        # 防重复：同一课表已自动扣减过则跳过
        cursor.execute("SELECT id FROM course_records WHERE schedule_id = %s LIMIT 1", (schedule_id,))
        if cursor.fetchone():
            conn.rollback()
            return {"ok": False, "message": "该课表已自动扣减过课时，本次不再重复扣减",
                    "minutes": minutes, "hours": hours, "customers": [], "skipped": []}

        # 悲观锁：锁定本课表全部学员涉及的「进行中」课时包行（SELECT ... FOR UPDATE），
        # 串行化并发扣减。并发事务会在此处阻塞等待，锁内重读保证基于最新剩余量计算。
        pkg_ids = sorted({p["id"] for pkgs in pkg_map.values() for p in pkgs})
        if pkg_ids:
            cursor.execute(
                "SELECT id, total_hours, used_hours FROM course_packages WHERE id = ANY(%s) FOR UPDATE",
                (pkg_ids,),
            )
            locked = {r["id"]: r for r in cursor.fetchall()}
            # 锁内重读：用加锁后的最新数据校正剩余课时，避免基于旧快照计算导致超扣
            for pkgs in pkg_map.values():
                for p in pkgs:
                    live = locked.get(p["id"])
                    p["remaining_hours"] = (
                        float(live["total_hours"] - live["used_hours"]) if live else 0.0
                    )

        for stu in students:
            cid = stu.get("customer_id")
            name = stu.get("name") or f"学员#{cid}"
            packages = pkg_map.get(cid) or []
            if not packages:
                skipped.append({"name": name, "reason": "无进行中课时包"})
                continue
            # 课时包类型匹配：优先用与课程同类型（1v1/1v多）的课时包；
            # 兼容旧数据（历史课时包无类型、默认 1v1）：若学员没有同类型课时包，则回退用全部进行中课时包
            course_type_safe = course_type if course_type in ("1v1", "1v多") else "1v1"
            same_type = [p for p in packages if (p.get("type") or "1v1") == course_type_safe]
            pool = same_type if same_type else packages
            # 扣减顺序：排课指定课时包优先 → 剩余足够且最早到期 → 剩余最多
            if prefer_pkg_id:
                ordered = ([p for p in pool if p["id"] == prefer_pkg_id]
                           + [p for p in pool if p["id"] != prefer_pkg_id])
            else:
                ordered = sorted(
                    pool,
                    key=lambda p: (0 if float(p.get("remaining_hours") or 0) >= hours else 1,
                                   p.get("expiry_date") or "",
                                   -float(p.get("remaining_hours") or 0)),
                )
            # 同类型课时包组合扣减：依次扣减，直到扣满本次课时
            remain_need = hours
            parts: List[Dict[str, Any]] = []
            for p in ordered:
                if remain_need <= 0:
                    break
                rem = float(p.get("remaining_hours") or 0)
                if rem <= 0:
                    continue
                take = min(rem, remain_need)
                parts.append({"pkg": p, "take": round(take, 2)})
                remain_need = round(remain_need - take, 2)
            if not parts:
                skipped.append({"name": name, "reason": "同类型课时包剩余不足"})
                continue
            # 降级：用 SAVEPOINT 包裹该学员的扣减，若并发导致课时不足/课时包失效，
            # 仅回滚该学员，不影响已扣减的其他学员（partial degrade）。
            try:
                cursor.execute("SAVEPOINT sp_auto_consume")
                for part in parts:
                    _insert_course_record(
                        cursor, package_id=part["pkg"]["id"], customer_id=cid,
                        record_date=record_date, hours_used=part["take"],
                        course_type=course_type, teacher=teacher,
                        notes=notes, schedule_id=schedule_id,
                    )
                cursor.execute("RELEASE SAVEPOINT sp_auto_consume")
            except (RuntimeError, psycopg2.Error):
                cursor.execute("ROLLBACK TO SAVEPOINT sp_auto_consume")
                skipped.append({"name": name, "reason": "课时包剩余不足或已失效，未扣减"})
                continue
            customers.append({
                "name": name,
                "package": " + ".join(p["pkg"].get("package_name") or "" for p in parts),
                "hours": round(sum(p["take"] for p in parts), 2),
            })
        conn.commit()
    except psycopg2.errors.UniqueViolation:
        # 并发竞态兜底：数据库唯一索引拦截了重复的 schedule_id，返回与主动查重一致的提示
        conn.rollback()
        return {"ok": False, "message": "该课表已自动扣减过课时，本次不再重复扣减",
                "minutes": minutes, "hours": hours, "customers": [], "skipped": []}
    except Exception:
        conn.rollback()
        raise
    finally:
        release_connection(conn)

    return {
        "ok": True,
        "message": f"按课表时长 {minutes} 分钟 ÷ 单课时 {lesson_minutes} 分钟 = {hours} 课时，已自动扣减 {len(customers)} 名学员",
        "minutes": minutes, "hours": hours,
        "customers": customers, "skipped": skipped,
    }
