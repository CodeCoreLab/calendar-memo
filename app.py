"""日历备忘录 —— 本地独立实现（Flask + SQLite）

前端页面与样式取自原站（cpcheng.top/memo.html），保持 1:1 视觉与交互；
本文件实现与原站等价的 HTTP 接口，数据落本地 SQLite，不依赖原站任何服务。

鉴权双通道：
  1) Cookie 会话 —— 前端页面用（原站行为，未改动）
  2) Bearer Token —— AI / 脚本 / MCP 用（/api/ai/token 签发）
两类身份在 current_user() 处统一，接口实现无需区分。
"""

import io
import json
import os
import re
import sqlite3
import secrets
import sys
import threading
import time
import calendar as pycal
from datetime import datetime, timedelta, date

from flask import Flask, g, jsonify, request, send_from_directory, session
from werkzeug.security import check_password_hash, generate_password_hash

def resource_dir():
    """只读资源目录（web 页面等）。打包后指向解压出的临时目录。"""
    if getattr(sys, "frozen", False):
        return getattr(sys, "_MEIPASS", os.path.dirname(os.path.abspath(sys.executable)))
    return os.path.dirname(os.path.abspath(__file__))


def data_dir():
    """可写数据目录。打包后必须落在 exe 旁边，不能放进临时解压目录——
    否则程序退出时 数据库随临时目录一起被删除，用户数据全部丢失。

    可用 MEMO_DATA_DIR 覆盖（便携 U 盘场景、或指定到共享位置）。
    """
    override = os.environ.get("MEMO_DATA_DIR")
    if override:
        return os.path.abspath(override)
    if getattr(sys, "frozen", False):
        return os.path.join(os.path.dirname(os.path.abspath(sys.executable)), "data")
    return os.path.join(os.path.dirname(os.path.abspath(__file__)), "data")


BASE_DIR = resource_dir()
WEB_DIR = os.path.join(BASE_DIR, "web")
DATA_DIR = data_dir()
DB_PATH = os.path.join(DATA_DIR, "memo.db")
UPLOAD_DIR = os.path.join(DATA_DIR, "uploads")

os.makedirs(DATA_DIR, exist_ok=True)
os.makedirs(UPLOAD_DIR, exist_ok=True)

app = Flask(__name__, static_folder=os.path.join(WEB_DIR, "static"), static_url_path="/static")
app.secret_key = os.environ.get("MEMO_SECRET", "memo-local-dev-secret-please-change")
app.config["MAX_CONTENT_LENGTH"] = 8 * 1024 * 1024

DT_FMT = "%Y-%m-%d %H:%M"
DATE_FMT = "%Y-%m-%d"

TAG_COLORS = ["#7F77DD", "#4f46e5", "#06b6d4", "#10b981", "#f59e0b",
              "#ef4444", "#8b5cf6", "#ec4899", "#0ea5e9", "#84cc16"]

POINT_COST = {"daily": 5, "weekly": 12}
MAX_PROOF_KB = 100
FREE_TAG_LIMIT = 5
TEAM_MAX_MEMBERS = 20        # 与原站新版文案「最多 20 人」对齐
PWD_MIN_LEN = 8
PWD_MAX_LEN = 72
PROOF_EXTS = {".jpg", ".jpeg", ".png", ".gif", ".webp"}
UPLOAD_TTL_DAYS = 30

# 常见弱口令（小写比对）；内网单人场景不追求大字典，只拦最典型的几个
PWD_BLACKLIST = {
    "password", "password1", "12345678", "123456789", "1234567890",
    "qwerty123", "abc12345", "admin123", "iloveyou", "11111111", "00000000",
    "a1234567", "passw0rd", "letmein1", "welcome1",
}

LOGIN_MAX_FAIL = 5
LOGIN_LOCK_SEC = 15 * 60
RESET_CODE_TTL_SEC = 10 * 60       # 找回密码验证码有效期
LOGIN_FAIL = {}
LOGIN_LOCK = threading.Lock()      # waitress 多线程下保护 LOGIN_FAIL

PLAN_LIST = [
    {"key": "week", "name": "周卡会员", "price": 5, "days": 7, "points": 60, "badge": "尝鲜", "gift_days": 0},
    {"key": "month", "name": "月卡会员", "price": 19, "days": 30, "points": 300, "badge": "最常用", "gift_days": 0},
    {"key": "senior", "name": "高级月卡", "price": 50, "days": 30, "points": 1000, "badge": "重度用户", "gift_days": 0},
]

FEEDBACK_KIND_TEXT = {"bug": "功能异常", "feature": "功能建议", "account": "账号/会员/积分", "other": "其它"}
FEEDBACK_STATUS_TEXT = {"pending": "待处理", "processing": "处理中", "replied": "已回复", "closed": "已关闭"}


# ────────────────────────────── 数据库 ──────────────────────────────

SCHEMA = """
CREATE TABLE IF NOT EXISTS users (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  username TEXT UNIQUE NOT NULL,
  nickname TEXT DEFAULT '',
  email TEXT DEFAULT '',
  pwd_hash TEXT NOT NULL,
  is_admin INTEGER DEFAULT 0,
  member_expire TEXT DEFAULT '',
  points INTEGER DEFAULT 0,
  created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS user_setting (
  user_id INTEGER PRIMARY KEY,
  show_huangli INTEGER DEFAULT 0,
  show_lunar INTEGER DEFAULT 1,
  huangli_on_click INTEGER DEFAULT 0
);

CREATE TABLE IF NOT EXISTS events (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  user_id INTEGER NOT NULL,
  title TEXT NOT NULL,
  content TEXT DEFAULT '',
  start_time TEXT NOT NULL,
  end_time TEXT NOT NULL,
  remark TEXT DEFAULT '',
  level TEXT DEFAULT 'middle',
  status TEXT DEFAULT 'doing',
  repeat_rule TEXT DEFAULT '',
  repeat_until TEXT DEFAULT '',
  actual_minutes INTEGER DEFAULT 0,
  archived INTEGER DEFAULT 0,
  created_at TEXT NOT NULL,
  updated_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_events_user ON events(user_id);

CREATE TABLE IF NOT EXISTS tags (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  user_id INTEGER NOT NULL,
  name TEXT NOT NULL,
  color TEXT DEFAULT '#7F77DD'
);

CREATE TABLE IF NOT EXISTS event_tag (
  event_id INTEGER NOT NULL,
  tag_id INTEGER NOT NULL,
  PRIMARY KEY (event_id, tag_id)
);

CREATE TABLE IF NOT EXISTS subtasks (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  event_id INTEGER NOT NULL,
  title TEXT NOT NULL,
  done INTEGER DEFAULT 0,
  sort INTEGER DEFAULT 0
);
CREATE INDEX IF NOT EXISTS idx_sub_event ON subtasks(event_id);

CREATE TABLE IF NOT EXISTS teams (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  name TEXT NOT NULL,
  owner_id INTEGER NOT NULL,
  remark TEXT DEFAULT '',
  deleted INTEGER DEFAULT 0,
  created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS memberships (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  team_id INTEGER NOT NULL,
  user_id INTEGER NOT NULL,
  role TEXT DEFAULT 'member',
  status TEXT DEFAULT 'joined',
  remark TEXT DEFAULT '',
  join_time TEXT DEFAULT '',
  UNIQUE (team_id, user_id)
);

CREATE TABLE IF NOT EXISTS invites (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  team_id INTEGER NOT NULL,
  inviter_id INTEGER NOT NULL,
  invitee_id INTEGER NOT NULL,
  status TEXT DEFAULT 'pending',
  message TEXT DEFAULT '',
  invite_time TEXT NOT NULL,
  join_time TEXT DEFAULT ''
);

CREATE TABLE IF NOT EXISTS shares (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  event_id INTEGER NOT NULL,
  from_user INTEGER NOT NULL,
  to_user INTEGER NOT NULL,
  team_id INTEGER NOT NULL,
  title TEXT DEFAULT '',
  start_time TEXT DEFAULT '',
  end_time TEXT DEFAULT '',
  content TEXT DEFAULT '',
  remark TEXT DEFAULT '',
  level TEXT DEFAULT 'middle',
  message TEXT DEFAULT '',
  status TEXT DEFAULT 'pending',
  created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS reports (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  user_id INTEGER NOT NULL,
  kind TEXT NOT NULL,
  period_start TEXT NOT NULL,
  period_end TEXT NOT NULL,
  title TEXT DEFAULT '',
  content TEXT DEFAULT '',
  model TEXT DEFAULT '',
  update_time TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS report_sends (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  report_id INTEGER NOT NULL,
  team_id INTEGER NOT NULL,
  from_user INTEGER NOT NULL,
  to_user INTEGER NOT NULL,
  note TEXT DEFAULT '',
  send_time TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS report_replies (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  report_id INTEGER NOT NULL,
  user_id INTEGER NOT NULL,
  content TEXT DEFAULT '',
  create_time TEXT NOT NULL,
  read_time TEXT DEFAULT ''
);

CREATE TABLE IF NOT EXISTS notices (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  title TEXT NOT NULL,
  content TEXT DEFAULT '',
  kind TEXT DEFAULT 'info',
  top INTEGER DEFAULT 0,
  publish_time TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS notice_state (
  user_id INTEGER NOT NULL,
  notice_id INTEGER NOT NULL,
  read_time TEXT DEFAULT '',
  deleted INTEGER DEFAULT 0,
  PRIMARY KEY (user_id, notice_id)
);

CREATE TABLE IF NOT EXISTS feedback (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  user_id INTEGER NOT NULL,
  kind TEXT DEFAULT 'bug',
  content TEXT DEFAULT '',
  contact TEXT DEFAULT '',
  page TEXT DEFAULT '',
  status TEXT DEFAULT 'pending',
  reply TEXT DEFAULT '',
  reply_admin TEXT DEFAULT '',
  reply_time TEXT DEFAULT '',
  create_time TEXT NOT NULL,
  read_time TEXT DEFAULT ''
);

CREATE TABLE IF NOT EXISTS points_log (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  user_id INTEGER NOT NULL,
  delta INTEGER NOT NULL,
  kind TEXT DEFAULT 'adjust',
  kind_text TEXT DEFAULT '积分变动',
  remark TEXT DEFAULT '',
  balance INTEGER DEFAULT 0,
  time TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS member_applies (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  user_id INTEGER NOT NULL,
  plan TEXT DEFAULT '',
  remark TEXT DEFAULT '',
  proof TEXT DEFAULT '',
  status TEXT DEFAULT 'pending',
  create_time TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS visits (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  page TEXT DEFAULT '',
  title TEXT DEFAULT '',
  time TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS api_tokens (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  user_id INTEGER NOT NULL,
  name TEXT DEFAULT '',
  token_hash TEXT UNIQUE NOT NULL,
  prefix TEXT DEFAULT '',
  scope TEXT DEFAULT 'read,write',
  created_at TEXT NOT NULL,
  last_used TEXT DEFAULT '',
  use_count INTEGER DEFAULT 0,
  revoked INTEGER DEFAULT 0
);
CREATE INDEX IF NOT EXISTS idx_tokens_user ON api_tokens(user_id);

CREATE TABLE IF NOT EXISTS idempotency (
  key TEXT PRIMARY KEY,
  user_id INTEGER NOT NULL,
  response TEXT NOT NULL,
  created_at TEXT NOT NULL
);
"""


def is_image_magic(head):
    """按文件头判断是否常见图片格式（PNG/JPEG/GIF/WEBP），不信任扩展名。"""
    if not head or len(head) < 12:
        return False
    return (
        head.startswith(b"\x89PNG\r\n\x1a\n")                    # PNG
        or head.startswith(b"\xff\xd8\xff")                       # JPEG
        or head.startswith(b"GIF87a") or head.startswith(b"GIF89a")  # GIF
        or (head[:4] == b"RIFF" and head[8:12] == b"WEBP")        # WEBP
    )


def pwd_problem(pwd):
    """返回不合规原因；合规返回空串。规则比原站更严，但原站的历史密码仍可登录。"""
    if not pwd or len(pwd) < PWD_MIN_LEN:
        return f"密码至少 {PWD_MIN_LEN} 位"
    if len(pwd) > PWD_MAX_LEN:
        return f"密码不能超过 {PWD_MAX_LEN} 位"
    if not re.search(r"[A-Za-z]", pwd) or not re.search(r"[0-9]", pwd):
        return "密码需同时包含字母和数字"
    if pwd.lower() in PWD_BLACKLIST:
        return "该密码过于常见，请更换"
    if len(set(pwd)) <= 2:
        return "密码字符过于重复，请更换"
    return ""


def _login_key(username):
    return f"{username}|{request.remote_addr or ''}"


def login_locked(username):
    """失败 5 次锁 15 分钟；按用户名+IP 记录，避免误锁他人账号。"""
    key = _login_key(username)
    with LOGIN_LOCK:
        rec = LOGIN_FAIL.get(key)
    if not rec:
        return 0
    cnt, last = rec
    if cnt < LOGIN_MAX_FAIL:
        return 0
    left = LOGIN_LOCK_SEC - (datetime.now() - last).total_seconds()
    return int(left) if left > 0 else 0


def login_fail(username):
    key = _login_key(username)
    with LOGIN_LOCK:
        cnt, _ = LOGIN_FAIL.get(key, (0, datetime.now()))
        LOGIN_FAIL[key] = (cnt + 1, datetime.now())
        if len(LOGIN_FAIL) > 2000:      # 防内存膨胀
            now = datetime.now()
            for k, (c, t) in list(LOGIN_FAIL.items()):
                if (now - t).total_seconds() > LOGIN_LOCK_SEC * 4:
                    LOGIN_FAIL.pop(k, None)


def login_success(username):
    with LOGIN_LOCK:
        LOGIN_FAIL.pop(_login_key(username), None)


def db():
    if "db" not in g:
        g.db = sqlite3.connect(DB_PATH)
        g.db.row_factory = sqlite3.Row
        g.db.execute("PRAGMA foreign_keys = ON")
        # WAL：读写并发更稳，避免长查询阻塞写入
        g.db.execute("PRAGMA journal_mode = WAL")
        g.db.execute("PRAGMA busy_timeout = 5000")
    return g.db


@app.teardown_appcontext
def close_db(_exc):
    conn = g.pop("db", None)
    if conn is not None:
        conn.close()


def init_db():
    conn = sqlite3.connect(DB_PATH)
    conn.executescript(SCHEMA)
    conn.commit()
    cur = conn.execute("SELECT COUNT(*) c FROM notices")
    if cur.fetchone()[0] == 0:
        now = datetime.now().strftime(DT_FMT)
        conn.executemany(
            "INSERT INTO notices(title, content, kind, top, publish_time) VALUES (?,?,?,?,?)",
            [
                ("欢迎使用日历备忘录（本地版）", "基础功能永久免费：日程、日历、三色优先级、标签、子任务、归档与耗时统计全部可用。", "info", 1, now),
                ("AI 日报 / 周报说明", "本地版不依赖外部大模型服务，日报周报由内置模板按你的日程记录汇总生成，可直接编辑后发送。", "success", 0, now),
            ],
        )
        conn.commit()
    conn.close()


# ────────────────────────────── 通用工具 ──────────────────────────────

def now_str():
    return datetime.now().strftime(DT_FMT)


def today_str():
    return date.today().strftime(DATE_FMT)


def parse_dt(s):
    """兼容 'YYYY-MM-DDTHH:MM' 与 'YYYY-MM-DD HH:MM' 与带秒的形式。"""
    if not s:
        return None
    s = str(s).strip().replace("T", " ")
    for fmt in ("%Y-%m-%d %H:%M:%S", "%Y-%m-%d %H:%M", "%Y-%m-%d"):
        try:
            return datetime.strptime(s, fmt)
        except ValueError:
            continue
    return None


def fmt_dt(v):
    return v.strftime(DT_FMT) if isinstance(v, datetime) else str(v or "")


def to_local_input(s):
    d = parse_dt(s)
    return d.strftime("%Y-%m-%dT%H:%M") if d else ""


def ok(data=None, msg="ok"):
    return jsonify({"code": 0, "data": data, "msg": msg})


def err(msg="操作失败", code=1, http=200):
    return jsonify({"code": code, "msg": msg}), http


def vip_required(msg="该功能是会员专属"):
    return jsonify({"code": 402, "msg": msg}), 200


TOKEN_CACHE_HOOKS = []      # serve.py 注册：Token 变更后清 MCP 层校验缓存


def clear_token_cache():
    for fn in list(TOKEN_CACHE_HOOKS):
        try:
            fn()
        except Exception:
            pass


def hash_token(raw):
    """Token 只存哈希，库泄露也拿不到明文。"""
    import hashlib
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


def token_from_request():
    """从 Authorization: Bearer <token> 或 X-API-Token 取 Token 并解析出用户。

    返回 (user_row, token_row)；无 Token 或无效时返回 (None, None)。
    无效 Token 不静默降级为匿名 —— 由调用方决定报错。
    """
    hdr = request.headers.get("Authorization", "") or ""
    raw = ""
    if hdr.lower().startswith("bearer "):
        raw = hdr[7:].strip()
    if not raw:
        raw = (request.headers.get("X-API-Token") or "").strip()
    if not raw:
        return None, None
    conn = db()
    row = conn.execute(
        "SELECT * FROM api_tokens WHERE token_hash = ? AND revoked = 0", (hash_token(raw),)
    ).fetchone()
    if not row:
        return None, None
    user = conn.execute("SELECT * FROM users WHERE id = ?", (row["user_id"],)).fetchone()
    if not user:
        return None, None
    conn.execute("UPDATE api_tokens SET last_used = ?, use_count = use_count + 1 WHERE id = ?",
                 (now_str(), row["id"]))
    conn.commit()
    return user, row


@app.before_request
def _resolve_token_identity():
    """无 Cookie 会话时，尝试用 Bearer Token 建立身份（仅内存，不写 Cookie）。"""
    g.api_token = None
    g.api_invalid = False
    if request.path.startswith("/api/"):
        hdr = request.headers.get("Authorization", "") or ""
        raw = hdr[7:].strip() if hdr.lower().startswith("bearer ") else (
            (request.headers.get("X-API-Token") or "").strip())
        if raw and not session.get("uid"):
            user, tok = token_from_request()
            if user is not None:
                g.token_user = user
                g.api_token = tok
            else:
                g.api_invalid = True
    return None


def current_user():
    # Cookie 会话优先（前端不受影响），其次 Bearer Token（AI / MCP）
    uid = session.get("uid")
    if uid:
        row = db().execute("SELECT * FROM users WHERE id = ?", (uid,)).fetchone()
        if row:
            return row
    tok_user = g.get("token_user")
    if tok_user is not None:
        # 每次请求重新读，保证会员/积分等字段是最新的
        return db().execute("SELECT * FROM users WHERE id = ?", (tok_user["id"],)).fetchone()
    return None


def require_login():
    u = current_user()
    if u is None:
        if g.get("api_invalid"):
            return None
        return None
    return u


def member_info(u):
    expire = parse_dt(u["member_expire"]) if u["member_expire"] else None
    if not expire:
        return False, "", 0
    left = (expire.date() - date.today()).days
    if left < 0:
        return False, u["member_expire"], 0
    return True, u["member_expire"], left


def add_points(conn, uid, delta, kind, kind_text, remark=""):
    u = conn.execute("SELECT points FROM users WHERE id = ?", (uid,)).fetchone()
    balance = int(u["points"] or 0) + int(delta)
    conn.execute("UPDATE users SET points = ? WHERE id = ?", (max(balance, 0), uid))
    conn.execute(
        "INSERT INTO points_log(user_id, delta, kind, kind_text, remark, balance, time) VALUES (?,?,?,?,?,?,?)",
        (uid, delta, kind, kind_text, remark, max(balance, 0), now_str()),
    )
    return max(balance, 0)


def grant_member(conn, uid, days, points, reason):
    u = conn.execute("SELECT member_expire FROM users WHERE id = ?", (uid,)).fetchone()
    cur_exp = parse_dt(u["member_expire"]) if u["member_expire"] else None
    base = cur_exp if cur_exp and cur_exp > datetime.now() else datetime.now()
    new_exp = base + timedelta(days=days)
    conn.execute("UPDATE users SET member_expire = ? WHERE id = ?", (fmt_dt(new_exp), uid))
    if points:
        add_points(conn, uid, points, "plan", "开通套餐", reason)
    conn.commit()


def event_tags(conn, eid):
    rows = conn.execute(
        "SELECT t.id, t.name, t.color FROM tags t JOIN event_tag et ON et.tag_id = t.id WHERE et.event_id = ?",
        (eid,),
    ).fetchall()
    return [{"id": r["id"], "name": r["name"], "color": r["color"]} for r in rows]


def event_subs(conn, eid):
    rows = conn.execute(
        "SELECT id, title, done FROM subtasks WHERE event_id = ? ORDER BY sort, id", (eid,)
    ).fetchall()
    return [{"id": r["id"], "title": r["title"], "done": bool(r["done"])} for r in rows]


def event_item(conn, row, compact=False):
    eid = row["id"]
    return {
        "id": eid,
        "title": row["title"],
        "content": row["content"] or "",
        "remark": row["remark"] or "",
        "level": row["level"],
        "status": row["status"],
        "startTime": fmt_dt(row["start_time"]),
        "endTime": fmt_dt(row["end_time"]),
        "startLocal": to_local_input(row["start_time"]),
        "endLocal": to_local_input(row["end_time"]),
        "startDate": str(row["start_time"])[:10],
        "endIso": fmt_dt(row["end_time"]),
        "repeatRule": row["repeat_rule"] or "",
        "repeatUntil": row["repeat_until"] or "",
        "actualMinutes": int(row["actual_minutes"] or 0),
        "archived": bool(row["archived"]),
        "tags": event_tags(conn, eid),
        "subtasks": event_subs(conn, eid),
    }


def sort_events(rows):
    """剩余时间正序；已超时的沉到进行中之后；已完成的沉底。"""
    now = datetime.now()

    def key(r):
        end = parse_dt(r["end_time"]) or now
        done = r["status"] == "done"
        overdue = (not done) and end < now
        group = 2 if done else (1 if overdue else 0)
        return (group, end)

    return sorted(rows, key=key)


def paginate(rows, page, page_size):
    total = len(rows)
    pages = max(1, (total + page_size - 1) // page_size)
    page = max(1, min(page, pages))
    start = (page - 1) * page_size
    return rows[start:start + page_size], page, pages, total


def validate_rule(rule):
    """校验重复规则串；合法返回空串，否则返回原因。

    位置语义（complex）：
      complex|monthly|<第几个 1-5 或 last>|<星期 0-6>
      complex|monthly|last|<星期 0-6>
      complex|monthly|nth:N|weekday:W
    """
    parts = str(rule).split("|")
    base = parts[0]
    if base not in ("day", "week", "month", "complex"):
        return "重复规则只能是 day / week / month / complex"
    digits = []
    for p in parts[1:]:
        if p.startswith("interval:"):
            v = p.split(":", 1)[1]
            if not v.isdigit() or not (1 <= int(v) <= 365):
                return "interval 需为 1~365 的整数"
        elif p.startswith("nth:"):
            v = p.split(":", 1)[1]
            if not v.isdigit() or not (1 <= int(v) <= 5):
                return "nth 需为 1~5 的整数"
        elif p.startswith("weekday:"):
            v = p.split(":", 1)[1]
            if not v.isdigit() or not (0 <= int(v) <= 6):
                return "weekday 需为 0~6（0=周一）"
        elif p in ("monthly", "last"):
            continue
        elif p.isdigit():
            digits.append(int(p))
        else:
            return f"无法识别的规则参数：{p}"

    if base == "complex" and digits:
        # 最后一个裸数字是星期（0~6），前面的若存在则是第几个（1~5）
        if not (0 <= digits[-1] <= 6):
            return "星期参数需为 0~6（0=周一）"
        for d in digits[:-1]:
            if not (1 <= d <= 5):
                return "「第几个」需为 1~5"
    elif digits:
        for d in digits:
            if not (1 <= d <= 31):
                return "数字参数需在 1~31 之间"
    return ""


def next_occurrence(start_dt, rule):
    """按规则推进一个周期。

    rule 支持两种写法：
      1) 简单：day / week / month / complex
      2) 带参数（管道分隔）：
         - interval|N            每 N 个周期（如 week|2 = 每两周）
         - complex|monthly|2|2   每月第 2 个星期 2（0=周一）
         - complex|last|weekday  每月最后一个星期 W
         - complex|nth:N|weekday:W
         - until 由调用方用 repeat_until 另行判断
    """
    if not rule:
        return None
    parts = str(rule).split("|")
    base = parts[0]

    interval = 1
    for p in parts[1:]:
        if p.startswith("interval:"):
            try:
                interval = max(1, int(p.split(":", 1)[1]))
            except ValueError:
                interval = 1
        elif p.isdigit():
            interval = max(1, int(p))

    if base == "day":
        return start_dt + timedelta(days=interval)
    if base == "week":
        return start_dt + timedelta(days=7 * interval)
    if base == "month":
        return _add_months_keep_day(start_dt, interval)

    if base == "complex":
        return _next_complex(start_dt, parts)

    return None


def _add_months_keep_day(dt, months=1):
    """加 N 个月并保持原日号；月末不足时取当月最后一天（1/31 → 2/28）。"""
    total = dt.month - 1 + months
    y = dt.year + total // 12
    m = total % 12 + 1
    day = min(dt.day, pycal.monthrange(y, m)[1])
    return dt.replace(year=y, month=m, day=day)


def _next_complex(dt, parts):
    """复杂规则：支持「第 N 个星期 W」与「最后一个星期 W」。

    parts 形式：
      complex|monthly|2|2      → 每月第 2 个星期 2
      complex|monthly|last|2   → 每月最后一个星期 2
      complex|monthly|nth:2|weekday:2
    省略参数时按当前日期自动推断（第几个 / 星期几）。
    """
    nth = None
    weekday = None
    last = False
    nums = []
    for p in parts[1:]:
        if p == "last":
            last = True
        elif p.startswith("nth:"):
            try:
                nth = int(p.split(":", 1)[1])
            except ValueError:
                pass
        elif p.startswith("weekday:"):
            try:
                weekday = int(p.split(":", 1)[1])
            except ValueError:
                pass
        elif p.isdigit():
            nums.append(int(p))
    if weekday is None:
        # 「complex|monthly|2|2」两数字 = 第 N 个 + 星期 W
        # 「complex|monthly|last|4」单数字 = 星期 W（last 已给定第几个）
        # 「complex|monthly|2」单数字且在 last 前 = 第 N 个（星期沿用起始日）
        if len(nums) > 1:
            weekday = nums[1]
        elif last:
            weekday = nums[0] if nums else dt.weekday()
        else:
            weekday = dt.weekday()
    if nth is None and not last:
        if len(nums) > 1:
            nth = nums[0]
        elif nums:
            nth = nums[0]
        else:
            nth = (dt.day - 1) // 7 + 1

    total = dt.month - 1 + 1
    y = dt.year + total // 12
    m = total % 12 + 1
    dim = pycal.monthrange(y, m)[1]
    days = [d for d in range(1, dim + 1) if date(y, m, d).weekday() == weekday]
    if not days:
        return _add_months_keep_day(dt, 1)
    if last:
        pick = days[-1]
    else:
        idx = min(max(1, nth), len(days)) - 1
        pick = days[idx]
    return dt.replace(year=y, month=m, day=pick)


# ────────────────────────────── 静态页面 ──────────────────────────────

@app.route("/")
@app.route("/memo.html")
def page_memo():
    return send_from_directory(WEB_DIR, "memo.html")


@app.route("/memo_login.html")
def page_login():
    return send_from_directory(WEB_DIR, "memo_login.html")


@app.route("/memo_reset.html")
def page_reset():
    return send_from_directory(WEB_DIR, "memo_reset.html")


@app.route("/liushan.jpg")
@app.route("/pay.jpg")
@app.route("/gongan.png")
def page_image():
    return send_from_directory(WEB_DIR, request.path.lstrip("/"))


@app.route("/favicon.ico")
def favicon():
    return ("", 204)


# ────────────────────────────── 认证 ──────────────────────────────

@app.post("/api/memo/login")
def api_login():
    d = request.get_json(silent=True) or {}
    username = (d.get("username") or "").strip()
    password = d.get("password") or ""
    if not username or not password:
        return err("请输入用户名和密码")
    left = login_locked(username)
    if left:
        # 锁定期内仍校验密码：密码正确即视为本人，直接解锁并放行，
        # 避免「攻击者乱试把主人也锁在门外」
        row = db().execute("SELECT * FROM users WHERE username = ?", (username,)).fetchone()
        if row and check_password_hash(row["pwd_hash"], password):
            login_success(username)
            session["uid"] = row["id"]
            return ok({"nickname": row["nickname"] or row["username"],
                       "username": row["username"]}, "登录成功")
        return err(f"密码错误次数过多，请 {left // 60 + 1} 分钟后再试")
    u = db().execute("SELECT * FROM users WHERE username = ?", (username,)).fetchone()
    if not u or not check_password_hash(u["pwd_hash"], password):
        login_fail(username)
        return err("用户名或密码错误")
    login_success(username)
    session["uid"] = u["id"]
    return ok({"nickname": u["nickname"] or u["username"], "username": u["username"]}, "登录成功")


@app.post("/api/memo/register")
def api_register():
    d = request.get_json(silent=True) or {}
    username = (d.get("username") or "").strip()
    nickname = (d.get("nickname") or "").strip()
    email = (d.get("email") or "").strip()
    password = d.get("password") or ""
    if len(username) < 3:
        return err("用户名至少 3 位")
    if not re.fullmatch(r"[A-Za-z0-9_]+", username):
        return err("用户名只能包含字母、数字和下划线")
    if not re.fullmatch(r"[^@\s]+@[^@\s]+\.[^@\s]+", email):
        return err("请填写正确的邮箱")
    bad = pwd_problem(password)
    if bad:
        return err(bad)
    conn = db()
    if conn.execute("SELECT 1 FROM users WHERE username = ?", (username,)).fetchone():
        return err("用户名已被占用")
    if conn.execute("SELECT 1 FROM users WHERE email = ?", (email,)).fetchone():
        return err("该邮箱已注册")
    expire = fmt_dt(datetime.now() + timedelta(days=7))
    cur = conn.execute(
        "INSERT INTO users(username, nickname, email, pwd_hash, member_expire, points, created_at) "
        "VALUES (?,?,?,?,?,?,?)",
        (username, nickname or username, email, generate_password_hash(password), expire, 0, now_str()),
    )
    uid = cur.lastrowid
    conn.execute("INSERT INTO user_setting(user_id) VALUES (?)", (uid,))
    add_points(conn, uid, 50, "trial", "体验赠送", "注册赠送 7 天体验会员 + 50 积分")
    conn.commit()
    session["uid"] = uid
    return ok({"nickname": nickname or username, "username": username}, "注册成功")


@app.get("/api/memo/check")
def api_check():
    u = require_login()
    if not u:
        return jsonify({"code": 0, "data": {"login": False}, "msg": "ok"})
    return ok({"id": u["id"], "login": True,
               "nickname": u["nickname"] or u["username"], "username": u["username"]})


@app.post("/api/memo/logout")
def api_logout():
    session.clear()
    return ok(None, "已退出")


@app.post("/api/memo/change_password")
def api_change_password():
    u = require_login()
    if not u:
        return err("登录已失效", 401)
    d = request.get_json(silent=True) or {}
    old, new = d.get("oldPassword") or "", d.get("newPassword") or ""
    if not check_password_hash(u["pwd_hash"], old):
        return err("原密码不正确")
    bad = pwd_problem(new)
    if bad:
        return err(bad)
    if new == old:
        return err("新密码不能与原密码相同")
    conn = db()
    conn.execute("UPDATE users SET pwd_hash = ? WHERE id = ?", (generate_password_hash(new), u["id"]))
    conn.commit()
    return ok(None, "密码已修改")


@app.post("/api/memo/send_code")
def api_send_code():
    """生成找回密码验证码。

    本地单机版不接邮件服务，因此在 DEMO 模式（MEMO_DEMO_RESET=1，默认开）下
    把验证码直接回给调用方，便于离线演示。验证码本身是随机会话值，且与本次
    会话、目标邮箱、10 分钟有效期三重绑定——不能拿来重置别的账号。
    部署到任何真实场景（多人可达）前必须置 MEMO_DEMO_RESET=0 并接真实邮件通道。
    """
    d = request.get_json(silent=True) or {}
    email = (d.get("email") or "").strip()
    if not email:
        return err("请填写邮箱")
    u = db().execute("SELECT id FROM users WHERE email = ?", (email,)).fetchone()
    if not u:
        return err("该邮箱未注册")
    code = f"{secrets.randbelow(1000000):06d}"
    session["reset_email"] = email
    session["reset_code"] = code
    session["reset_at"] = time.time()
    if os.getenv("MEMO_DEMO_RESET", "1") == "1":
        return ok({"demoCode": code},
                  "验证码已生成（DEMO 模式直接返回，生产环境请关闭 MEMO_DEMO_RESET）")
    return ok(None, "验证码已发送，请查收邮件")


@app.post("/api/memo/reset_password")
def api_reset_password():
    d = request.get_json(silent=True) or {}
    email = (d.get("email") or "").strip()
    code = (d.get("code") or "").strip()
    pwd = d.get("password") or ""
    # 三重绑定：验证码必须存在、属于本会话、属于同一邮箱、且未超时。
    # 缺任何一条都能被用来重置他人账号。
    sent_code = session.get("reset_code")
    sent_email = session.get("reset_email")
    sent_at = session.get("reset_at") or 0
    if (not sent_code or not sent_email or email != sent_email
            or time.time() - sent_at > RESET_CODE_TTL_SEC
            or not secrets.compare_digest(code, sent_code)):
        return err("验证码不正确或已过期")
    bad = pwd_problem(pwd)
    if bad:
        return err(bad)
    conn = db()
    u = conn.execute("SELECT id FROM users WHERE email = ?", (email,)).fetchone()
    if not u:
        return err("该邮箱未注册")
    conn.execute("UPDATE users SET pwd_hash = ? WHERE id = ?", (generate_password_hash(pwd), u["id"]))
    conn.commit()
    return ok(None, "密码已重置，请用新密码登录")


@app.post("/api/visit")
def api_visit():
    """记录一次页面访问。匿名可调用（登录页也要统计），因此必须限长限频。

    不限长的后果实测：单次请求可写入 100KB 字符串，无鉴权即可反复灌库。
    """
    d = request.get_json(silent=True) or {}
    page = str(d.get("page", "") or "")[:200]
    title = str(d.get("title", "") or "")[:200]
    # 单会话 60 秒内最多记 20 条，超出直接返回 ok（不报错，避免前端噪音）
    now = time.time()
    win = session.get("visit_win") or [0, 0]
    if now - win[0] > 60:
        win = [now, 0]
    if win[1] >= 20:
        return ok(None, "ok")
    win[1] += 1
    session["visit_win"] = win
    conn = db()
    conn.execute("INSERT INTO visits(page, title, time) VALUES (?,?,?)",
                 (page, title, now_str()))
    conn.commit()
    return ok(None, "ok")


# ────────────────────────────── 日程 ──────────────────────────────

@app.get("/api/memo/events")
def api_events():
    u = require_login()
    if not u:
        return err("登录已失效", 401)
    conn = db()
    page = max(1, int(request.args.get("page", 1) or 1))
    page_size = max(1, min(200, int(request.args.get("pageSize", 6) or 6)))
    keyword = (request.args.get("keyword") or "").strip().lower()
    level = (request.args.get("level") or "").strip()
    status = (request.args.get("status") or "").strip()
    archived = int(request.args.get("archived", 0) or 0)
    tag_id = (request.args.get("tag") or "").strip()
    date_filter = (request.args.get("date") or "").strip()

    sql = "SELECT * FROM events WHERE user_id = ? AND archived = ?"
    params = [u["id"], archived]
    if status in ("doing", "done"):
        sql += " AND status = ?"
        params.append(status)
    if level in ("high", "middle", "low"):
        sql += " AND level = ?"
        params.append(level)
    if date_filter:
        sql += " AND substr(start_time, 1, 10) = ?"
        params.append(date_filter)
    if tag_id.isdigit():
        sql += " AND id IN (SELECT event_id FROM event_tag WHERE tag_id = ?)"
        params.append(int(tag_id))
    if keyword:
        sql += " AND (lower(title) LIKE ? OR lower(content) LIKE ? OR lower(remark) LIKE ?)"
        like = f"%{keyword}%"
        params += [like, like, like]

    rows = conn.execute(sql, params).fetchall()
    if not date_filter:
        rows = sort_events(rows)
    page_rows, page, pages, total = paginate(rows, page, page_size)
    return ok({
        "list": [event_item(conn, r, True) for r in page_rows],
        "page": page, "pageSize": page_size, "total": total, "totalPages": pages,
    })


@app.get("/api/memo/event/<int:eid>")
def api_event_get(eid):
    u = require_login()
    if not u:
        return err("登录已失效", 401)
    conn = db()
    row = conn.execute("SELECT * FROM events WHERE id = ? AND user_id = ?", (eid, u["id"])).fetchone()
    if not row:
        return err("记录不存在")
    return ok(event_item(conn, row))


@app.post("/api/memo/event")
def api_event_create():
    u = require_login()
    if not u:
        return err("登录已失效", 401)
    d = request.get_json(silent=True) or {}
    title = (d.get("title") or "").strip()
    if not title:
        return err("请填写标题")
    end = parse_dt(d.get("endTime"))
    if not end:
        return err("请选择截止时间")
    start = parse_dt(d.get("startTime")) or end
    if end < start:
        return err("截止时间不能早于开始时间")
    conn = db()
    tag_ids = [int(x) for x in (d.get("tagIds") or []) if str(x).isdigit()]
    is_member, _, _ = member_info(u)
    if not is_member:
        mine = conn.execute("SELECT COUNT(*) c FROM tags WHERE user_id = ?", (u["id"],)).fetchone()["c"]
        new_tags = [tid for tid in tag_ids
                    if not conn.execute("SELECT 1 FROM event_tag WHERE event_id IN "
                                        "(SELECT id FROM events WHERE user_id = ?) AND tag_id = ?",
                                        (u["id"], tid)).fetchone()]
        if mine + len(set(new_tags)) > FREE_TAG_LIMIT:
            return vip_required("标签上限")

    now = now_str()
    cur = conn.execute(
        "INSERT INTO events(user_id, title, content, start_time, end_time, remark, level, status,"
        " created_at, updated_at) VALUES (?,?,?,?,?,?,?,?,?,?)",
        (u["id"], title, (d.get("content") or "").strip(), fmt_dt(start), fmt_dt(end),
         (d.get("remark") or "").strip(), d.get("level") or "middle",
         d.get("status") or "doing", now, now),
    )
    eid = cur.lastrowid
    for tid in tag_ids:
        if conn.execute("SELECT 1 FROM tags WHERE id = ? AND user_id = ?", (tid, u["id"])).fetchone():
            conn.execute("INSERT OR IGNORE INTO event_tag(event_id, tag_id) VALUES (?,?)", (eid, tid))
    for i, s in enumerate(d.get("subtasks") or []):
        t = (s.get("title") or "").strip()
        if t:
            conn.execute("INSERT INTO subtasks(event_id, title, done, sort) VALUES (?,?,?,?)",
                         (eid, t, 1 if s.get("done") else 0, i))
    conn.commit()
    row = conn.execute("SELECT * FROM events WHERE id = ?", (eid,)).fetchone()
    return ok(event_item(conn, row), "保存成功")


@app.put("/api/memo/event/<int:eid>")
def api_event_update(eid):
    u = require_login()
    if not u:
        return err("登录已失效", 401)
    conn = db()
    row = conn.execute("SELECT * FROM events WHERE id = ? AND user_id = ?", (eid, u["id"])).fetchone()
    if not row:
        return err("记录不存在")
    d = request.get_json(silent=True) or {}
    title = (d.get("title") or "").strip()
    if not title:
        return err("请填写标题")
    end = parse_dt(d.get("endTime"))
    if not end:
        return err("请选择截止时间")
    start = parse_dt(d.get("startTime")) or end
    if end < start:
        return err("截止时间不能早于开始时间")

    conn.execute(
        "UPDATE events SET title=?, content=?, start_time=?, end_time=?, remark=?, level=?,"
        " status=?, updated_at=? WHERE id=?",
        (title, (d.get("content") or "").strip(), fmt_dt(start), fmt_dt(end),
         (d.get("remark") or "").strip(), d.get("level") or "middle",
         d.get("status") or "doing", now_str(), eid),
    )
    if "tagIds" in d:
        tag_ids = [int(x) for x in (d.get("tagIds") or []) if str(x).isdigit()]
        conn.execute("DELETE FROM event_tag WHERE event_id = ?", (eid,))
        for tid in tag_ids:
            if conn.execute("SELECT 1 FROM tags WHERE id = ? AND user_id = ?", (tid, u["id"])).fetchone():
                conn.execute("INSERT OR IGNORE INTO event_tag(event_id, tag_id) VALUES (?,?)", (eid, tid))
    if "subtasks" in d:
        keep = []
        for i, s in enumerate(d.get("subtasks") or []):
            t = (s.get("title") or "").strip()
            if not t:
                continue
            sid = int(s.get("id") or 0)
            if sid:
                exists = conn.execute("SELECT id FROM subtasks WHERE id=? AND event_id=?",
                                      (sid, eid)).fetchone()
                if exists:
                    conn.execute("UPDATE subtasks SET title=?, sort=? WHERE id=?", (t, i, sid))
                    keep.append(sid)
                    continue
            cur = conn.execute("INSERT INTO subtasks(event_id, title, done, sort) VALUES (?,?,?,?)",
                               (eid, t, 1 if s.get("done") else 0, i))
            keep.append(cur.lastrowid)
        if keep:
            ph = ",".join("?" * len(keep))
            conn.execute(f"DELETE FROM subtasks WHERE event_id=? AND id NOT IN ({ph})", [eid] + keep)
        else:
            conn.execute("DELETE FROM subtasks WHERE event_id = ?", (eid,))
    conn.commit()
    row = conn.execute("SELECT * FROM events WHERE id = ?", (eid,)).fetchone()
    return ok(event_item(conn, row), "保存成功")


@app.delete("/api/memo/event/<int:eid>")
def api_event_delete(eid):
    u = require_login()
    if not u:
        return err("登录已失效", 401)
    conn = db()
    if not conn.execute("SELECT 1 FROM events WHERE id=? AND user_id=?", (eid, u["id"])).fetchone():
        return err("记录不存在")
    conn.execute("DELETE FROM events WHERE id = ?", (eid,))
    conn.execute("DELETE FROM event_tag WHERE event_id = ?", (eid,))
    conn.execute("DELETE FROM subtasks WHERE event_id = ?", (eid,))
    conn.commit()
    return ok(None, "已删除")


@app.post("/api/memo/event/<int:eid>/status")
def api_event_status(eid):
    u = require_login()
    if not u:
        return err("登录已失效", 401)
    d = request.get_json(silent=True) or {}
    status = d.get("status") if d.get("status") in ("doing", "done") else "done"
    conn = db()
    row = conn.execute("SELECT * FROM events WHERE id=? AND user_id=?", (eid, u["id"])).fetchone()
    if not row:
        return err("记录不存在")
    conn.execute("UPDATE events SET status=?, updated_at=? WHERE id=?", (status, now_str(), eid))

    # 标记完成且设了重复 → 按规则生成下一条（原有那条保持已完成）
    spawned = None
    if status == "done" and (row["repeat_rule"] or ""):
        start = parse_dt(row["start_time"])
        end = parse_dt(row["end_time"])
        nxt_start = next_occurrence(start, row["repeat_rule"])
        if nxt_start:
            nxt_end = nxt_start + (end - start)
            until = parse_dt(row["repeat_until"]) if row["repeat_until"] else None
            if not until or nxt_start.date() <= until.date():
                exists = conn.execute(
                    "SELECT id FROM events WHERE user_id=? AND title=? AND start_time=? AND archived=0",
                    (u["id"], row["title"], fmt_dt(nxt_start)),
                ).fetchone()
                if not exists:
                    now = now_str()
                    cur = conn.execute(
                        "INSERT INTO events(user_id, title, content, start_time, end_time, remark, level,"
                        " status, repeat_rule, repeat_until, created_at, updated_at)"
                        " VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
                        (u["id"], row["title"], row["content"], fmt_dt(nxt_start), fmt_dt(nxt_end),
                         row["remark"], row["level"], "doing", row["repeat_rule"],
                         row["repeat_until"], now, now),
                    )
                    spawned = cur.lastrowid
                    for t in event_tags(conn, eid):
                        conn.execute("INSERT OR IGNORE INTO event_tag(event_id, tag_id) VALUES (?,?)",
                                     (spawned, t["id"]))
    conn.commit()
    return ok({"spawned": spawned}, "状态已更新")


@app.put("/api/memo/event/<int:eid>/repeat")
def api_event_repeat(eid):
    u = require_login()
    if not u:
        return err("登录已失效", 401)
    conn = db()
    if not conn.execute("SELECT 1 FROM events WHERE id=? AND user_id=?", (eid, u["id"])).fetchone():
        return err("记录不存在")
    d = request.get_json(silent=True) or {}
    rule = (d.get("rule") or "none").strip()
    if rule == "none":
        rule = ""
    if rule:
        bad = validate_rule(rule)
        if bad:
            return err(bad)
    is_member, _, _ = member_info(u)
    if rule.split("|")[0] == "complex" and not is_member:
        return vip_required("复杂循环规则")
    until = (d.get("until") or "").strip() if rule else ""
    conn.execute("UPDATE events SET repeat_rule=?, repeat_until=? WHERE id=?", (rule, until, eid))
    conn.commit()
    return ok(None, "已保存")


@app.post("/api/memo/event/<int:eid>/minutes")
def api_event_minutes(eid):
    u = require_login()
    if not u:
        return err("登录已失效", 401)
    d = request.get_json(silent=True) or {}
    try:
        mins = max(0, min(100000, int(d.get("minutes") or 0)))
    except (TypeError, ValueError):
        mins = 0
    conn = db()
    conn.execute("UPDATE events SET actual_minutes=? WHERE id=? AND user_id=?", (mins, eid, u["id"]))
    conn.commit()
    return ok(None, "已保存")


@app.post("/api/memo/event/<int:eid>/archive")
def api_event_archive(eid):
    u = require_login()
    if not u:
        return err("登录已失效", 401)
    d = request.get_json(silent=True) or {}
    flag = 1 if d.get("archived") else 0
    conn = db()
    conn.execute("UPDATE events SET archived=?, updated_at=? WHERE id=? AND user_id=?",
                 (flag, now_str(), eid, u["id"]))
    conn.commit()
    return ok(None, "已归档" if flag else "已恢复")


@app.get("/api/memo/calendar")
def api_calendar():
    u = require_login()
    if not u:
        return err("登录已失效", 401)
    try:
        year = int(request.args.get("year") or date.today().year)
        month = int(request.args.get("month") or date.today().month)
    except ValueError:
        year, month = date.today().year, date.today().month
    prefix = f"{year:04d}-{month:02d}"
    conn = db()
    rows = conn.execute(
        "SELECT * FROM events WHERE user_id=? AND archived=0 AND substr(start_time,1,7)=?",
        (u["id"], prefix),
    ).fetchall()
    marks = {}
    for r in rows:
        ds = str(r["start_time"])[:10]
        marks.setdefault(ds, []).append({
            "id": r["id"], "title": r["title"], "level": r["level"],
            "status": r["status"], "endTime": fmt_dt(r["end_time"]),
        })
    return ok({"marks": marks, "year": year, "month": month})


@app.get("/api/memo/summary")
def api_summary():
    u = require_login()
    if not u:
        return err("登录已失效", 401)
    conn = db()
    rows = conn.execute("SELECT * FROM events WHERE user_id=?", (u["id"],)).fetchall()
    now = datetime.now()
    doing = done = overdue = arch = 0
    today = 0
    tds = today_str()
    for r in rows:
        if r["archived"]:
            arch += 1
            continue
        end = parse_dt(r["end_time"])
        if r["status"] == "done":
            done += 1
        else:
            doing += 1
            if end and end < now:
                overdue += 1
        if str(r["start_time"])[:10] == tds:
            today += 1
    return ok({"total": len(rows) - arch, "doing": doing, "done": done,
               "overdue": overdue, "archived": arch, "today": today})


# ────────────────────────────── 标签 ──────────────────────────────

@app.get("/api/memo/tags")
def api_tags():
    u = require_login()
    if not u:
        return err("登录已失效", 401)
    rows = db().execute("SELECT id, name, color FROM tags WHERE user_id=? ORDER BY id",
                        (u["id"],)).fetchall()
    return ok({"list": [{"id": r["id"], "name": r["name"], "color": r["color"]} for r in rows]})


@app.post("/api/memo/tags")
def api_tag_create():
    u = require_login()
    if not u:
        return err("登录已失效", 401)
    d = request.get_json(silent=True) or {}
    name = (d.get("name") or "").strip()[:30]
    if not name:
        return err("请输入标签名")
    conn = db()
    if conn.execute("SELECT 1 FROM tags WHERE user_id=? AND name=?", (u["id"], name)).fetchone():
        return err("标签已存在")
    is_member, _, _ = member_info(u)
    count = conn.execute("SELECT COUNT(*) c FROM tags WHERE user_id=?", (u["id"],)).fetchone()["c"]
    if not is_member and count >= FREE_TAG_LIMIT:
        return vip_required("标签上限")
    color = TAG_COLORS[count % len(TAG_COLORS)]
    conn.execute("INSERT INTO tags(user_id, name, color) VALUES (?,?,?)", (u["id"], name, color))
    conn.commit()
    return ok(None, "标签已添加")


@app.delete("/api/memo/tag/<int:tid>")
def api_tag_delete(tid):
    u = require_login()
    if not u:
        return err("登录已失效", 401)
    conn = db()
    if not conn.execute("SELECT 1 FROM tags WHERE id=? AND user_id=?", (tid, u["id"])).fetchone():
        return err("标签不存在")
    conn.execute("DELETE FROM event_tag WHERE tag_id=?", (tid,))
    conn.execute("DELETE FROM tags WHERE id=?", (tid,))
    conn.commit()
    return ok(None, "标签已删除")


# ────────────────────────────── 子任务 ──────────────────────────────

def owned_subtask(conn, sid, uid):
    return conn.execute(
        "SELECT s.* FROM subtasks s JOIN events e ON e.id = s.event_id "
        "WHERE s.id=? AND e.user_id=?", (sid, uid)).fetchone()


@app.put("/api/memo/subtask/<int:sid>")
@app.post("/api/memo/subtask/<int:sid>")
def api_subtask_update(sid):
    u = require_login()
    if not u:
        return err("登录已失效", 401)
    conn = db()
    row = owned_subtask(conn, sid, u["id"])
    if not row:
        return err("子任务不存在")
    d = request.get_json(silent=True) or {}
    fields, params = [], []
    if "done" in d:
        fields.append("done=?")
        params.append(1 if d.get("done") else 0)
    if "title" in d:
        fields.append("title=?")
        params.append((d.get("title") or "").strip())
    if fields:
        conn.execute(f"UPDATE subtasks SET {','.join(fields)} WHERE id=?", params + [sid])
        conn.commit()
    return ok(None, "已保存")


@app.delete("/api/memo/subtask/<int:sid>")
def api_subtask_delete(sid):
    u = require_login()
    if not u:
        return err("登录已失效", 401)
    conn = db()
    if not owned_subtask(conn, sid, u["id"]):
        return err("子任务不存在")
    conn.execute("DELETE FROM subtasks WHERE id=?", (sid,))
    conn.commit()
    return ok(None, "已删除")


# ────────────────────────────── 设置 ──────────────────────────────

@app.get("/api/memo/settings")
def api_settings_get():
    u = require_login()
    if not u:
        return err("登录已失效", 401)
    conn = db()
    row = conn.execute("SELECT * FROM user_setting WHERE user_id=?", (u["id"],)).fetchone()
    if not row:
        conn.execute("INSERT INTO user_setting(user_id) VALUES (?)", (u["id"],))
        conn.commit()
        row = conn.execute("SELECT * FROM user_setting WHERE user_id=?", (u["id"],)).fetchone()
    return ok({"show_huangli": row["show_huangli"], "show_lunar": row["show_lunar"],
               "huangli_on_click": row["huangli_on_click"]})


@app.post("/api/memo/settings")
def api_settings_save():
    u = require_login()
    if not u:
        return err("登录已失效", 401)
    d = request.get_json(silent=True) or {}
    conn = db()
    conn.execute("INSERT OR IGNORE INTO user_setting(user_id) VALUES (?)", (u["id"],))
    if "show_huangli" in d:
        conn.execute("UPDATE user_setting SET show_huangli=? WHERE user_id=?",
                     (1 if d.get("show_huangli") else 0, u["id"]))
    if "show_lunar" in d:
        conn.execute("UPDATE user_setting SET show_lunar=? WHERE user_id=?",
                     (1 if d.get("show_lunar") else 0, u["id"]))
    conn.commit()
    return ok(None, "已保存")


# ────────────────────────────── 会员 / 积分 ──────────────────────────────

@app.get("/api/member/status")
def api_member_status():
    u = require_login()
    if not u:
        return err("登录已失效", 401)
    is_member, expire, days = member_info(u)
    return ok({"is_member": is_member, "expire_at": expire, "days_left": days,
               "points": int(u["points"] or 0), "point_cost": POINT_COST})


@app.get("/api/member/plans")
def api_member_plans():
    u = require_login()
    if not u:
        return err("登录已失效", 401)
    return ok({
        "plans": PLAN_LIST,
        "max_proof_kb": MAX_PROOF_KB,
        "gift_days": 0,
        "point_cost": POINT_COST,
        "team_max_members": TEAM_MAX_MEMBERS,
        "trial": {"days": 7, "points": 50},
        "guide": {
            "price_text": "周卡 5 元 / 月卡 19 元 / 高级月卡 50 元",
            "wechat": "本地版无需扫码：提交开通申请后立即到账（演示模式）",
            "note": "本地版为演示模式，提交即生效，不经过人工审核",
        },
    })


@app.get("/api/member/points")
def api_member_points():
    u = require_login()
    if not u:
        return err("登录已失效", 401)
    conn = db()
    # 新版前端用 page+size 分页；旧版用 limit 截断，两种参数都兼容
    if request.args.get("page") is not None or request.args.get("size") is not None:
        try:
            page = max(1, int(request.args.get("page", 1) or 1))
            size = max(1, min(100, int(request.args.get("size", 10) or 10)))
        except ValueError:
            return err("page / size 必须是整数")
        total = conn.execute("SELECT COUNT(*) c FROM points_log WHERE user_id=?",
                             (u["id"],)).fetchone()["c"]
        pages = max(1, (total + size - 1) // size)
        page = min(page, pages)
        rows = conn.execute(
            "SELECT * FROM points_log WHERE user_id=? ORDER BY id DESC LIMIT ? OFFSET ?",
            (u["id"], size, (page - 1) * size)).fetchall()
    else:
        limit = max(1, min(200, int(request.args.get("limit", 50) or 50)))
        total = conn.execute("SELECT COUNT(*) c FROM points_log WHERE user_id=?",
                             (u["id"],)).fetchone()["c"]
        size = limit
        page = 1
        pages = 1
        rows = conn.execute("SELECT * FROM points_log WHERE user_id=? ORDER BY id DESC LIMIT ?",
                            (u["id"], limit)).fetchall()
    return ok({
        "points": int(u["points"] or 0),
        "point_cost": POINT_COST,
        "page": page, "size": size, "total": total, "pages": pages,
        "list": [{"delta": r["delta"], "kind": r["kind"], "kindText": r["kind_text"],
                  "remark": r["remark"], "balance": r["balance"], "time": r["time"]} for r in rows],
    })


@app.post("/api/member/apply")
def api_member_apply():
    u = require_login()
    if not u:
        return err("登录已失效", 401)
    plan = (request.form.get("plan") or "").strip()
    remark = (request.form.get("remark") or "").strip()
    chosen = next((p for p in PLAN_LIST if p["key"] == plan), None)
    if not chosen:
        return err("套餐不存在")
    proof = ""
    f = request.files.get("proof")
    if f and f.filename:
        ext = os.path.splitext(f.filename)[1].lower()
        # 扩展名 + 真实文件头双重校验：只认图片，不靠客户端声明的 Content-Type
        if ext not in PROOF_EXTS:
            return err(f"只接受图片格式：{'/'.join(sorted(PROOF_EXTS))}")
        head = f.stream.read(12)
        f.stream.seek(0)
        if not is_image_magic(head):
            return err("文件内容不是有效图片，请上传真实截图")
        if len(f.read()) > MAX_PROOF_KB * 1024:
            return err(f"付款截图超过 {MAX_PROOF_KB}KB 上限")
        f.seek(0)
        name = f"proof_{u['id']}_{secrets.token_hex(6)}{ext}"
        f.save(os.path.join(UPLOAD_DIR, name))
        proof = name
    conn = db()
    conn.execute("INSERT INTO member_applies(user_id, plan, remark, proof, status, create_time)"
                 " VALUES (?,?,?,?,?,?)", (u["id"], plan, remark, proof, "approved", now_str()))
    # 本地演示：提交即开通（与原站差异点，见说明）
    grant_member(conn, u["id"], chosen["days"], chosen["points"], f"{chosen['name']} 开通")
    return ok(None, "提交成功，会员已开通")


@app.get("/api/member/dashboard")
def api_dashboard():
    u = require_login()
    if not u:
        return err("登录已失效", 401)
    is_member, _, _ = member_info(u)
    if not is_member:
        return vip_required("数据统计仪表盘")
    conn = db()
    rows = conn.execute("SELECT * FROM events WHERE user_id=?", (u["id"],)).fetchall()
    now = datetime.now()
    live = [r for r in rows if not r["archived"]]
    total = len(live)
    done = sum(1 for r in live if r["status"] == "done")
    doing = total - done
    overdue = sum(1 for r in live if r["status"] != "done"
                  and (parse_dt(r["end_time"]) or now) < now)
    minutes = sum(int(r["actual_minutes"] or 0) for r in live)

    trend = []
    for i in range(13, -1, -1):
        d = (date.today() - timedelta(days=i)).strftime(DATE_FMT)
        day_rows = [r for r in live if str(r["start_time"])[:10] == d]
        trend.append({"date": d, "total": len(day_rows),
                      "done": sum(1 for r in day_rows if r["status"] == "done")})

    levels = []
    for lv in ("high", "middle", "low"):
        lr = [r for r in live if r["level"] == lv]
        if lr:
            levels.append({"level": lv, "total": len(lr),
                           "done": sum(1 for r in lr if r["status"] == "done")})

    tag_rows = conn.execute(
        "SELECT t.name nm, COUNT(et.event_id) c FROM tags t "
        "LEFT JOIN event_tag et ON et.tag_id = t.id "
        "LEFT JOIN events e ON e.id = et.event_id AND e.archived = 0 AND e.user_id = ? "
        "WHERE t.user_id = ? GROUP BY t.id HAVING c > 0 ORDER BY c DESC", (u["id"], u["id"])).fetchall()

    return ok({
        "total": total, "done": done, "doing": doing, "overdue": overdue,
        "archived": len(rows) - total, "minutes": minutes,
        "done_rate": round(done * 100.0 / total, 1) if total else 0.0,
        "overdue_rate": round(overdue * 100.0 / total, 1) if total else 0.0,
        "trend": trend, "levels": levels,
        "tags": [{"name": r["nm"], "count": r["c"]} for r in tag_rows],
    })


# ────────────────────────────── 通知 / 反馈 ──────────────────────────────

@app.get("/api/memo/notify")
def api_notify():
    u = require_login()
    if not u:
        return err("登录已失效", 401)
    conn = db()
    invites = conn.execute("SELECT COUNT(*) c FROM invites WHERE invitee_id=? AND status='pending'",
                           (u["id"],)).fetchone()["c"]
    shares = conn.execute("SELECT COUNT(*) c FROM shares WHERE to_user=? AND status='pending'",
                          (u["id"],)).fetchone()["c"]
    reports = conn.execute(
        "SELECT COUNT(*) c FROM report_sends rs WHERE rs.to_user=? AND NOT EXISTS "
        "(SELECT 1 FROM report_replies rr WHERE rr.report_id=rs.report_id AND rr.user_id=? "
        "AND rr.read_time<>'')", (u["id"], u["id"])).fetchone()["c"]
    replies = conn.execute(
        "SELECT COUNT(*) c FROM report_replies rr JOIN reports rp ON rp.id=rr.report_id "
        "WHERE rp.user_id=? AND rr.user_id<>? AND rr.read_time=''", (u["id"], u["id"])).fetchone()["c"]
    notices = conn.execute(
        "SELECT COUNT(*) c FROM notices n WHERE NOT EXISTS "
        "(SELECT 1 FROM notice_state s WHERE s.notice_id=n.id AND s.user_id=? AND "
        "(s.read_time<>'' OR s.deleted=1))", (u["id"],)).fetchone()["c"]
    fb = conn.execute("SELECT COUNT(*) c FROM feedback WHERE user_id=? AND reply<>'' AND read_time=''",
                      (u["id"],)).fetchone()["c"]
    return ok({"invites": invites, "shares": shares, "reports": reports, "replies": replies,
               "notices": notices, "feedbackUnread": fb,
               "teamTotal": invites + shares + reports,
               "total": invites + shares + reports + notices})


@app.get("/api/memo/notices")
def api_notices():
    u = require_login()
    if not u:
        return err("登录已失效", 401)
    page = max(1, int(request.args.get("page", 1) or 1))
    page_size = max(1, min(50, int(request.args.get("pageSize", 10) or 10)))
    conn = db()
    rows = conn.execute(
        "SELECT n.*, s.read_time, s.deleted FROM notices n LEFT JOIN notice_state s "
        "ON s.notice_id=n.id AND s.user_id=? WHERE COALESCE(s.deleted,0)=0 "
        "ORDER BY n.top DESC, n.publish_time DESC", (u["id"],)).fetchall()
    page_rows, page, pages, total = paginate(rows, page, page_size)
    # 打开列表即已读
    for r in page_rows:
        conn.execute("INSERT INTO notice_state(user_id, notice_id, read_time) VALUES (?,?,?) "
                     "ON CONFLICT(user_id, notice_id) DO UPDATE SET read_time=excluded.read_time",
                     (u["id"], r["id"], now_str()))
    conn.commit()
    return ok({
        "list": [{"id": r["id"], "title": r["title"], "content": r["content"], "kind": r["kind"],
                  "top": r["top"], "publishTime": r["publish_time"],
                  "read": bool(r["read_time"])} for r in page_rows],
        "page": page, "pages": pages, "total": total, "pageSize": page_size,
    })


@app.delete("/api/memo/notice/<int:nid>")
def api_notice_delete(nid):
    u = require_login()
    if not u:
        return err("登录已失效", 401)
    conn = db()
    conn.execute("INSERT INTO notice_state(user_id, notice_id, read_time, deleted) VALUES (?,?,?,1) "
                 "ON CONFLICT(user_id, notice_id) DO UPDATE SET deleted=1",
                 (u["id"], nid, now_str()))
    conn.commit()
    return ok(None, "已删除")


@app.get("/api/memo/feedback")
def api_feedback_list():
    u = require_login()
    if not u:
        return err("登录已失效", 401)
    conn = db()
    rows = conn.execute("SELECT * FROM feedback WHERE user_id=? ORDER BY id DESC", (u["id"],)).fetchall()
    unread = sum(1 for r in rows if r["reply"] and not r["read_time"])
    return ok({
        "unread": unread,
        "list": [{"id": r["id"], "kind": r["kind"], "kindText": FEEDBACK_KIND_TEXT.get(r["kind"], r["kind"]),
                  "content": r["content"], "contact": r["contact"], "status": r["status"],
                  "statusText": FEEDBACK_STATUS_TEXT.get(r["status"], r["status"]),
                  "createTime": r["create_time"], "reply": r["reply"],
                  "replyAdmin": r["reply_admin"], "replyTime": r["reply_time"],
                  "unread": bool(r["reply"] and not r["read_time"])} for r in rows],
    })


@app.post("/api/memo/feedback")
def api_feedback_create():
    u = require_login()
    if not u:
        return err("登录已失效", 401)
    d = request.get_json(silent=True) or {}
    content = (d.get("content") or "").strip()
    if len(content) < 5:
        return err("请把问题写清楚一点（至少 5 个字）")
    kind = d.get("kind") if d.get("kind") in FEEDBACK_KIND_TEXT else "other"
    conn = db()
    conn.execute("INSERT INTO feedback(user_id, kind, content, contact, page, status, create_time)"
                 " VALUES (?,?,?,?,?,?,?)",
                 (u["id"], kind, content, (d.get("contact") or "").strip()[:120],
                  d.get("page") or "", "pending", now_str()))
    conn.commit()
    return ok(None, "已提交，感谢反馈")


@app.post("/api/memo/feedback/read")
def api_feedback_read():
    u = require_login()
    if not u:
        return err("登录已失效", 401)
    conn = db()
    conn.execute("UPDATE feedback SET read_time=? WHERE user_id=? AND reply<>'' AND read_time=''",
                 (now_str(), u["id"]))
    conn.commit()
    return ok(None, "已读")


# ────────────────────────────── 团队 ──────────────────────────────

def team_member_rows(conn, tid):
    return conn.execute(
        "SELECT m.*, u.nickname, u.email, u.username FROM memberships m "
        "JOIN users u ON u.id = m.user_id WHERE m.team_id=? ORDER BY "
        "CASE m.role WHEN 'owner' THEN 0 ELSE 1 END, m.id", (tid,)).fetchall()


@app.get("/api/memo/team/list")
def api_team_list():
    u = require_login()
    if not u:
        return err("登录已失效", 401)
    is_member, _, _ = member_info(u)
    if not is_member:
        return vip_required("团队协作")
    page = max(1, int(request.args.get("page", 1) or 1))
    page_size = max(1, min(50, int(request.args.get("pageSize", 10) or 10)))
    conn = db()
    trows = conn.execute(
        "SELECT t.*, m.role FROM teams t JOIN memberships m ON m.team_id=t.id "
        "WHERE m.user_id=? AND m.status='joined' AND t.deleted=0 ORDER BY t.id", (u["id"],)).fetchall()
    teams = []
    for t in trows:
        members = team_member_rows(conn, t["id"])
        joined = [m for m in members if m["status"] == "joined"]
        pending = conn.execute("SELECT COUNT(*) c FROM invites WHERE team_id=? AND status='pending'",
                               (t["id"],)).fetchone()["c"]
        owner = next((m for m in members if m["role"] == "owner"), None)
        teams.append({
            "id": t["id"], "name": t["name"], "role": t["role"], "remark": t["remark"] or "",
            "memberCount": len(joined), "maxMembers": TEAM_MAX_MEMBERS,
            "pendingCount": pending if t["role"] == "owner" else 0,
            "ownerId": owner["user_id"] if owner else t["owner_id"],
        })
    invrows = conn.execute(
        "SELECT i.*, t.name team_name, t.deleted team_deleted, t.owner_id, "
        "iu.nickname inviter_name FROM invites i JOIN teams t ON t.id=i.team_id "
        "LEFT JOIN users iu ON iu.id=i.inviter_id WHERE i.invitee_id=? "
        "ORDER BY i.id DESC", (u["id"],)).fetchall()
    inv_rows_page, _, _, inv_total = paginate(invrows, page, page_size)
    invites = [{
        "inviteId": r["id"], "name": r["team_name"], "status": r["status"],
        "inviterName": r["inviter_name"] or "组长", "inviteTime": r["invite_time"],
        "joinTime": r["join_time"] or "", "message": r["message"] or "",
        "teamDeleted": bool(r["team_deleted"]),
    } for r in inv_rows_page]
    return ok({
        "teams": teams, "invites": invites,
        "inviteTotal": inv_total, "invitePage": page, "invitePageSize": page_size,
        "invitePages": max(1, (inv_total + page_size - 1) // page_size),
        "invitePending": sum(1 for x in invrows if x["status"] == "pending"),
        "pageSize": page_size, "maxMembers": TEAM_MAX_MEMBERS,
    })


@app.post("/api/memo/team")
def api_team_create():
    u = require_login()
    if not u:
        return err("登录已失效", 401)
    is_member, _, _ = member_info(u)
    if not is_member:
        return vip_required("团队协作")
    d = request.get_json(silent=True) or {}
    name = (d.get("name") or "").strip()[:40]
    if not name:
        return err("请填写团队名称")
    conn = db()
    cur = conn.execute("INSERT INTO teams(name, owner_id, remark, created_at) VALUES (?,?,?,?)",
                       (name, u["id"], (d.get("remark") or "").strip(), now_str()))
    tid = cur.lastrowid
    conn.execute("INSERT INTO memberships(team_id, user_id, role, status, join_time) VALUES (?,?,?,?,?)",
                 (tid, u["id"], "owner", "joined", now_str()))
    conn.commit()
    return ok({"id": tid}, "团队已创建")


@app.put("/api/memo/team/<int:tid>")
def api_team_rename(tid):
    u = require_login()
    if not u:
        return err("登录已失效", 401)
    conn = db()
    t = conn.execute("SELECT * FROM teams WHERE id=? AND deleted=0", (tid,)).fetchone()
    if not t:
        return err("团队不存在")
    if t["owner_id"] != u["id"]:
        return err("只有组长可以改名")
    d = request.get_json(silent=True) or {}
    name = (d.get("name") or "").strip()[:40]
    if not name:
        return err("请填写团队名称")
    conn.execute("UPDATE teams SET name=? WHERE id=?", (name, tid))
    conn.commit()
    return ok(None, "已重命名")


@app.delete("/api/memo/team/<int:tid>")
def api_team_disband(tid):
    u = require_login()
    if not u:
        return err("登录已失效", 401)
    conn = db()
    t = conn.execute("SELECT * FROM teams WHERE id=? AND deleted=0", (tid,)).fetchone()
    if not t:
        return err("团队不存在")
    if t["owner_id"] != u["id"]:
        return err("只有组长可以解散团队")
    conn.execute("UPDATE teams SET deleted=1 WHERE id=?", (tid,))
    conn.commit()
    return ok(None, "团队已解散")


@app.delete("/api/memo/team/<int:tid>/member/<int:uid>")
def api_team_remove_member(tid, uid):
    u = require_login()
    if not u:
        return err("登录已失效", 401)
    conn = db()
    t = conn.execute("SELECT * FROM teams WHERE id=? AND deleted=0", (tid,)).fetchone()
    if not t:
        return err("团队不存在")
    if uid != u["id"] and t["owner_id"] != u["id"]:
        return err("无权移除该成员")
    if uid == t["owner_id"] and uid != u["id"]:
        return err("不能移除组长")
    conn.execute("DELETE FROM memberships WHERE team_id=? AND user_id=?", (tid, uid))
    conn.execute("UPDATE invites SET status='quit' WHERE team_id=? AND invitee_id=? AND status='joined'",
                 (tid, uid))
    conn.commit()
    return ok(None, "已退出" if uid == u["id"] else "已移出")


@app.get("/api/memo/team/<int:tid>/members")
def api_team_members(tid):
    u = require_login()
    if not u:
        return err("登录已失效", 401)
    conn = db()
    t = conn.execute("SELECT * FROM teams WHERE id=? AND deleted=0", (tid,)).fetchone()
    if not t:
        return err("团队不存在")
    me = conn.execute("SELECT * FROM memberships WHERE team_id=? AND user_id=?",
                      (tid, u["id"])).fetchone()
    if not me:
        return err("你不在这个团队里")
    rows = team_member_rows(conn, tid)
    joined = [r for r in rows if r["status"] == "joined"]
    return ok({
        "team": {"memberCount": len(joined), "maxMembers": TEAM_MAX_MEMBERS, "name": t["name"]},
        "isOwner": t["owner_id"] == u["id"],
        "members": [{"userId": r["user_id"], "nickname": r["nickname"] or r["username"],
                     "email": r["email"] or "", "username": r["username"], "role": r["role"],
                     "status": r["status"], "remark": r["remark"] or ""} for r in rows],
    })


@app.get("/api/memo/team/<int:tid>/invites")
def api_team_pending(tid):
    u = require_login()
    if not u:
        return err("登录已失效", 401)
    conn = db()
    t = conn.execute("SELECT * FROM teams WHERE id=? AND deleted=0", (tid,)).fetchone()
    if not t:
        return err("团队不存在")
    if t["owner_id"] != u["id"]:
        return err("只有组长可以查看待处理邀请")
    rows = conn.execute(
        "SELECT i.*, us.nickname, us.email FROM invites i JOIN users us ON us.id=i.invitee_id "
        "WHERE i.team_id=? AND i.status='pending' ORDER BY i.id DESC", (tid,)).fetchall()
    return ok({"list": [{"inviteId": r["id"], "nickname": r["nickname"] or "", "email": r["email"] or "",
                         "message": r["message"] or ""} for r in rows]})


@app.get("/api/memo/team/search")
def api_team_search():
    u = require_login()
    if not u:
        return err("登录已失效", 401)
    is_member, _, _ = member_info(u)
    if not is_member:
        return vip_required("团队协作")
    kw = (request.args.get("kw") or "").strip()
    if len(kw) < 2:
        return ok({"list": []})
    conn = db()
    rows = conn.execute(
        "SELECT id, nickname, email, username FROM users WHERE id<>? AND "
        "(email LIKE ? OR username LIKE ? OR nickname LIKE ?) LIMIT 10",
        (u["id"], f"%{kw}%", f"%{kw}%", f"%{kw}%")).fetchall()
    return ok({"list": [{"id": r["id"], "nickname": r["nickname"] or r["username"],
                         "email": r["email"] or "", "username": r["username"]} for r in rows]})


@app.post("/api/memo/team/<int:tid>/invite")
def api_team_invite(tid):
    u = require_login()
    if not u:
        return err("登录已失效", 401)
    is_member, _, _ = member_info(u)
    if not is_member:
        return vip_required("团队协作")
    d = request.get_json(silent=True) or {}
    uid = int(d.get("userId") or 0)
    conn = db()
    t = conn.execute("SELECT * FROM teams WHERE id=? AND deleted=0", (tid,)).fetchone()
    if not t:
        return err("团队不存在")
    if t["owner_id"] != u["id"]:
        return err("只有组长可以邀请成员")
    target = conn.execute("SELECT * FROM users WHERE id=?", (uid,)).fetchone()
    if not target:
        return err("用户不存在")
    rows = team_member_rows(conn, tid)
    joined = [r for r in rows if r["status"] == "joined"]
    if any(r["user_id"] == uid and r["status"] == "joined" for r in rows):
        return err("TA 已经在团队里了")
    if len(joined) >= TEAM_MAX_MEMBERS:
        return err(f"团队最多 {TEAM_MAX_MEMBERS} 人")
    exist = conn.execute("SELECT * FROM invites WHERE team_id=? AND invitee_id=? AND status='pending'",
                         (tid, uid)).fetchone()
    if exist:
        return err("已经邀请过了，等对方确认")
    conn.execute("INSERT INTO invites(team_id, inviter_id, invitee_id, status, message, invite_time)"
                 " VALUES (?,?,?,?,?,?)",
                 (tid, u["id"], uid, "pending", (d.get("message") or "").strip(), now_str()))
    conn.commit()
    return ok(None, "邀请已发出，等对方确认")


@app.get("/api/memo/team/invites/sent")
def api_team_invites_sent():
    u = require_login()
    if not u:
        return err("登录已失效", 401)
    is_member, _, _ = member_info(u)
    if not is_member:
        return vip_required("团队协作")
    page = max(1, int(request.args.get("page", 1) or 1))
    page_size = max(1, min(50, int(request.args.get("pageSize", 10) or 10)))
    conn = db()
    rows = conn.execute(
        "SELECT i.*, t.name team_name, t.deleted team_deleted, us.nickname uname, us.email uemail "
        "FROM invites i JOIN teams t ON t.id=i.team_id JOIN users us ON us.id=i.invitee_id "
        "WHERE i.inviter_id=? ORDER BY i.id DESC", (u["id"],)).fetchall()
    page_rows, page, pages, total = paginate(rows, page, page_size)
    return ok({
        "list": [{"inviteId": r["id"], "teamName": r["team_name"], "userName": r["uname"] or "",
                  "userEmail": r["uemail"] or "", "status": r["status"],
                  "inviteTime": r["invite_time"], "joinTime": r["join_time"] or "",
                  "message": r["message"] or "", "teamDeleted": bool(r["team_deleted"])}
                 for r in page_rows],
        "page": page, "pages": pages, "total": total, "pageSize": page_size,
    })


@app.post("/api/memo/team/invite/<int:mid>")
def api_invite_respond(mid):
    u = require_login()
    if not u:
        return err("登录已失效", 401)
    d = request.get_json(silent=True) or {}
    action = d.get("action")
    conn = db()
    inv = conn.execute("SELECT * FROM invites WHERE id=? AND invitee_id=?", (mid, u["id"])).fetchone()
    if not inv:
        return err("邀请不存在")
    if inv["status"] != "pending":
        return err("这条邀请已经处理过了")
    t = conn.execute("SELECT * FROM teams WHERE id=?", (inv["team_id"],)).fetchone()
    if not t or t["deleted"]:
        conn.execute("UPDATE invites SET status='revoked' WHERE id=?", (mid,))
        conn.commit()
        return err("团队已解散")
    if action == "accept":
        rows = team_member_rows(conn, inv["team_id"])
        if len([r for r in rows if r["status"] == "joined"]) >= TEAM_MAX_MEMBERS:
            return err(f"团队已满 {TEAM_MAX_MEMBERS} 人")
        conn.execute("UPDATE invites SET status='joined', join_time=? WHERE id=?", (now_str(), mid))
        conn.execute("INSERT INTO memberships(team_id, user_id, role, status, join_time) VALUES (?,?,?,?,?) "
                     "ON CONFLICT(team_id, user_id) DO UPDATE SET status='joined', join_time=excluded.join_time",
                     (inv["team_id"], u["id"], "member", "joined", now_str()))
        conn.commit()
        return ok(None, "已加入团队")
    conn.execute("UPDATE invites SET status='rejected' WHERE id=?", (mid,))
    conn.commit()
    return ok(None, "已拒绝")


@app.delete("/api/memo/team/invite/<int:mid>")
def api_invite_revoke(mid):
    u = require_login()
    if not u:
        return err("登录已失效", 401)
    conn = db()
    inv = conn.execute("SELECT * FROM invites WHERE id=?", (mid,)).fetchone()
    if not inv:
        return err("邀请不存在")
    if inv["inviter_id"] != u["id"] and inv["invitee_id"] != u["id"]:
        return err("无权撤回")
    conn.execute("UPDATE invites SET status='revoked' WHERE id=?", (mid,))
    conn.commit()
    return ok(None, "已撤回")


@app.get("/api/memo/team/<int:tid>/member/<int:uid>/profile")
def api_member_profile(tid, uid):
    u = require_login()
    if not u:
        return err("登录已失效", 401)
    conn = db()
    t = conn.execute("SELECT * FROM teams WHERE id=? AND deleted=0", (tid,)).fetchone()
    if not t:
        return err("团队不存在")
    me = conn.execute("SELECT * FROM memberships WHERE team_id=? AND user_id=?",
                      (tid, u["id"])).fetchone()
    if not me:
        return err("你不在这个团队里")
    m = conn.execute("SELECT m.*, us.nickname, us.email, us.username FROM memberships m "
                     "JOIN users us ON us.id=m.user_id WHERE m.team_id=? AND m.user_id=?",
                     (tid, uid)).fetchone()
    if not m:
        return err("成员不存在")
    is_owner = t["owner_id"] == u["id"]
    daily = conn.execute("SELECT COUNT(*) c FROM reports WHERE user_id=? AND kind='daily'",
                         (uid,)).fetchone()["c"]
    weekly = conn.execute("SELECT COUNT(*) c FROM reports WHERE user_id=? AND kind='weekly'",
                          (uid,)).fetchone()["c"]
    replies = conn.execute("SELECT COUNT(*) c FROM report_replies WHERE user_id=?", (uid,)).fetchone()["c"]
    last = conn.execute("SELECT update_time t FROM reports WHERE user_id=? ORDER BY id DESC LIMIT 1",
                        (uid,)).fetchone()
    return ok({
        "userId": uid, "nickname": m["nickname"] or m["username"],
        "email": m["email"] or "", "username": m["username"],
        "role": m["role"], "status": m["status"], "joinTime": m["join_time"] or "",
        "teamName": t["name"], "remark": m["remark"] or "",
        "canRemark": is_owner and uid != u["id"],
        "canViewReports": is_owner,
        "stats": {"daily": daily, "weekly": weekly, "replies": replies,
                  "lastTime": last["t"] if last else ""},
    })


@app.get("/api/memo/team/<int:tid>/member/<int:uid>/reports")
def api_member_reports(tid, uid):
    u = require_login()
    if not u:
        return err("登录已失效", 401)
    conn = db()
    t = conn.execute("SELECT * FROM teams WHERE id=? AND deleted=0", (tid,)).fetchone()
    if not t:
        return err("团队不存在")
    if t["owner_id"] != u["id"]:
        return err("只有组长能查看组员的汇报内容")
    page = max(1, int(request.args.get("page", 1) or 1))
    page_size = max(1, min(50, int(request.args.get("pageSize", 8) or 8)))
    kind = (request.args.get("kind") or "").strip()
    sql = "SELECT * FROM reports WHERE user_id=?"
    params = [uid]
    if kind in ("daily", "weekly"):
        sql += " AND kind=?"
        params.append(kind)
    rows = conn.execute(sql + " ORDER BY period_start DESC, id DESC", params).fetchall()
    page_rows, page, pages, total = paginate(rows, page, page_size)
    out = []
    for r in page_rows:
        sends = conn.execute("SELECT * FROM report_sends WHERE report_id=?", (r["id"],)).fetchall()
        mine_send = next((s for s in sends if s["team_id"] == tid), None)
        read = any(conn.execute("SELECT 1 FROM report_replies WHERE report_id=? AND user_id=? "
                                "AND read_time<>''", (r["id"], uid)).fetchone() for _ in [0]) if sends else False
        reply_count = conn.execute("SELECT COUNT(*) c FROM report_replies WHERE report_id=?",
                                   (r["id"],)).fetchone()["c"]
        out.append({
            "id": r["id"], "kind": r["kind"], "title": r["title"], "content": r["content"],
            "periodStart": r["period_start"], "periodEnd": r["period_end"],
            "sendTime": mine_send["send_time"] if mine_send else r["update_time"],
            "read": read, "replyCount": reply_count,
            "sent": bool(sends), "inTeam": bool(mine_send),
        })
    return ok({"list": out, "page": page, "pages": pages, "total": total, "pageSize": page_size})


@app.post("/api/memo/team/<int:tid>/member/<int:uid>/remark")
def api_member_remark(tid, uid):
    u = require_login()
    if not u:
        return err("登录已失效", 401)
    conn = db()
    t = conn.execute("SELECT * FROM teams WHERE id=? AND deleted=0", (tid,)).fetchone()
    if not t:
        return err("团队不存在")
    if t["owner_id"] != u["id"]:
        return err("只有组长可以设备注")
    d = request.get_json(silent=True) or {}
    remark = (d.get("remark") or "").strip()[:20]
    conn.execute("UPDATE memberships SET remark=? WHERE team_id=? AND user_id=?", (remark, tid, uid))
    conn.commit()
    return ok({"remark": remark}, "备注已保存")


# ────────────────────────────── 日程同步 ──────────────────────────────

@app.post("/api/memo/event/<int:eid>/share")
def api_event_share(eid):
    u = require_login()
    if not u:
        return err("登录已失效", 401)
    is_member, _, _ = member_info(u)
    if not is_member:
        return vip_required("团队协作")
    d = request.get_json(silent=True) or {}
    tid = int(d.get("teamId") or 0)
    targets = [int(x) for x in (d.get("toUsers") or []) if str(x).isdigit()]
    conn = db()
    ev = conn.execute("SELECT * FROM events WHERE id=? AND user_id=?", (eid, u["id"])).fetchone()
    if not ev:
        return err("日程不存在")
    t = conn.execute("SELECT * FROM teams WHERE id=? AND deleted=0", (tid,)).fetchone()
    if not t:
        return err("团队不存在")
    if not targets:
        return err("请至少勾选一位队友")
    sent = 0
    for uid_to in targets:
        m = conn.execute("SELECT * FROM memberships WHERE team_id=? AND user_id=? AND status='joined'",
                         (tid, uid_to)).fetchone()
        if not m or uid_to == u["id"]:
            continue
        conn.execute(
            "INSERT INTO shares(event_id, from_user, to_user, team_id, title, start_time, end_time,"
            " content, remark, level, message, status, created_at) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (eid, u["id"], uid_to, tid, ev["title"], fmt_dt(ev["start_time"]), fmt_dt(ev["end_time"]),
             ev["content"], ev["remark"], ev["level"], (d.get("message") or "").strip(),
             "pending", now_str()),
        )
        sent += 1
    conn.commit()
    return ok(None, f"已发送给 {sent} 位队友" if sent else "没有可发送的队友")


@app.get("/api/memo/share/inbox")
def api_share_inbox():
    u = require_login()
    if not u:
        return err("登录已失效", 401)
    page = max(1, int(request.args.get("page", 1) or 1))
    page_size = max(1, min(50, int(request.args.get("pageSize", 10) or 10)))
    conn = db()
    rows = conn.execute(
        "SELECT s.*, us.nickname from_nick, t.name team_name FROM shares s "
        "JOIN users us ON us.id=s.from_user JOIN teams t ON t.id=s.team_id "
        "WHERE s.to_user=? ORDER BY s.id DESC", (u["id"],)).fetchall()
    page_rows, page, pages, total = paginate(rows, page, page_size)
    return ok({
        "list": [{"id": r["id"], "title": r["title"], "fromNickname": r["from_nick"],
                  "teamName": r["team_name"], "startTime": r["start_time"], "endTime": r["end_time"],
                  "status": r["status"], "message": r["message"] or ""} for r in page_rows],
        "page": page, "pages": pages, "total": total, "pageSize": page_size,
        "pending": sum(1 for r in rows if r["status"] == "pending"),
    })


@app.get("/api/memo/share/sent")
def api_share_sent():
    u = require_login()
    if not u:
        return err("登录已失效", 401)
    page = max(1, int(request.args.get("page", 1) or 1))
    page_size = max(1, min(50, int(request.args.get("pageSize", 10) or 10)))
    conn = db()
    rows = conn.execute(
        "SELECT s.*, us.nickname to_nick, t.name team_name FROM shares s "
        "JOIN users us ON us.id=s.to_user JOIN teams t ON t.id=s.team_id "
        "WHERE s.from_user=? ORDER BY s.id DESC", (u["id"],)).fetchall()
    page_rows, page, pages, total = paginate(rows, page, page_size)
    return ok({
        "list": [{"id": r["id"], "title": r["title"], "toNickname": r["to_nick"],
                  "teamName": r["team_name"], "startTime": r["start_time"], "endTime": r["end_time"],
                  "status": r["status"]} for r in page_rows],
        "page": page, "pages": pages, "total": total, "pageSize": page_size,
    })


@app.post("/api/memo/share/<int:sid>")
def api_share_respond(sid):
    u = require_login()
    if not u:
        return err("登录已失效", 401)
    d = request.get_json(silent=True) or {}
    action = d.get("action")
    conn = db()
    s = conn.execute("SELECT * FROM shares WHERE id=? AND to_user=?", (sid, u["id"])).fetchone()
    if not s:
        return err("同步记录不存在")
    if s["status"] != "pending":
        return err("这条同步已经处理过了")
    if action == "accept":
        now = now_str()
        cur = conn.execute(
            "INSERT INTO events(user_id, title, content, start_time, end_time, remark, level, status,"
            " created_at, updated_at) VALUES (?,?,?,?,?,?,?,?,?,?)",
            (u["id"], s["title"], s["content"], s["start_time"], s["end_time"], s["remark"],
             s["level"], "doing", now, now),
        )
        conn.execute("UPDATE shares SET status='accepted' WHERE id=?", (sid,))
        conn.commit()
        return ok({"eventId": cur.lastrowid}, "已接收，日程已加入你的列表")
    conn.execute("UPDATE shares SET status='rejected' WHERE id=?", (sid,))
    conn.commit()
    return ok(None, "已忽略")


@app.delete("/api/memo/share/<int:sid>")
def api_share_revoke(sid):
    u = require_login()
    if not u:
        return err("登录已失效", 401)
    conn = db()
    if not conn.execute("SELECT 1 FROM shares WHERE id=? AND from_user=?", (sid, u["id"])).fetchone():
        return err("同步记录不存在")
    conn.execute("DELETE FROM shares WHERE id=?", (sid,))
    conn.commit()
    return ok(None, "已撤回")


# ────────────────────────────── AI 日报 / 周报 ──────────────────────────────

def report_period(kind, date_s):
    d = parse_dt(date_s) or datetime.now()
    if kind == "weekly":
        start = d - timedelta(days=d.weekday())
        end = start + timedelta(days=6)
    else:
        start = end = d
    return start.strftime(DATE_FMT), end.strftime(DATE_FMT)


def build_report_content(conn, uid, kind, start_s, end_s):
    """本地模板汇总：不调用外部大模型，按日程记录生成可读草稿。"""
    rows = conn.execute(
        "SELECT * FROM events WHERE user_id=? AND archived=0 AND substr(start_time,1,10) "
        "BETWEEN ? AND ? ORDER BY start_time", (uid, start_s, end_s)).fetchall()
    if not rows:
        return (f"{'周报' if kind == 'weekly' else '日报'}（{start_s}）\n\n"
                f"本{'周' if kind == 'weekly' else '日'}没有记录到日程，建议补充后再汇总。"), 0, 0, 0
    done = [r for r in rows if r["status"] == "done"]
    doing = [r for r in rows if r["status"] != "done"]
    minutes = sum(int(r["actual_minutes"] or 0) for r in rows)
    tag_count = {}
    for r in rows:
        for t in event_tags(conn, r["id"]):
            tag_count[t["name"]] = tag_count.get(t["name"], 0) + 1

    lines = []
    title = f"{'周报' if kind == 'weekly' else '日报'} {start_s}" + (f" ~ {end_s}" if kind == "weekly" else "")
    lines.append(title)
    lines.append("")
    lines.append(f"一、整体情况：共 {len(rows)} 项，已完成 {len(done)} 项，进行中 {len(doing)} 项"
                 + (f"，累计投入 {minutes} 分钟" if minutes else "") + "。")
    if tag_count:
        top = sorted(tag_count.items(), key=lambda x: -x[1])[:5]
        lines.append("二、工作分布：" + "、".join(f"{k}（{v} 项）" for k, v in top) + "。")
    else:
        lines.append("二、工作分布：本周期未使用标签，建议按项目打标签后便于统计。")
    lines.append("三、已完成：")
    if done:
        for r in done[:12]:
            mins = f"（{r['actual_minutes']} 分钟）" if r["actual_minutes"] else ""
            lines.append(f"  · {r['title']}{mins}")
    else:
        lines.append("  · 暂无已完成事项")
    lines.append("四、进行中 / 待跟进：")
    if doing:
        for r in doing[:12]:
            lines.append(f"  · {r['title']}（截止 {fmt_dt(r['end_time'])}）")
    else:
        lines.append("  · 暂无待跟进事项")
    lines.append("五、下一步：按优先级推进进行中事项，超期项优先处理。")
    return "\n".join(lines), len(rows), len(done), minutes


@app.get("/api/memo/report")
def api_report_get():
    u = require_login()
    if not u:
        return err("登录已失效", 401)
    is_member, _, _ = member_info(u)
    if not is_member:
        return vip_required("AI 日报周报")
    kind = request.args.get("kind") if request.args.get("kind") in ("daily", "weekly") else "daily"
    date_s = request.args.get("date") or today_str()
    start_s, end_s = report_period(kind, date_s)
    conn = db()
    r = conn.execute("SELECT * FROM reports WHERE user_id=? AND kind=? AND period_start=?",
                     (u["id"], kind, start_s)).fetchone()
    report = None
    if r:
        report = {"id": r["id"], "kind": r["kind"], "title": r["title"], "content": r["content"],
                  "startDate": r["period_start"], "endDate": r["period_end"],
                  "updateTime": r["update_time"], "model": r["model"]}
    return ok({
        "report": report, "aiEnabled": 1, "points": int(u["points"] or 0),
        "cost": POINT_COST[kind], "startDate": start_s, "endDate": end_s if kind == "weekly" else "",
    })


@app.post("/api/memo/report/generate")
def api_report_generate():
    u = require_login()
    if not u:
        return err("登录已失效", 401)
    is_member, _, _ = member_info(u)
    if not is_member:
        return vip_required("AI 日报周报")
    d = request.get_json(silent=True) or {}
    kind = d.get("kind") if d.get("kind") in ("daily", "weekly") else "daily"
    date_s = d.get("date") or today_str()
    force = bool(d.get("force"))
    manual_only = bool(d.get("manual"))
    start_s, end_s = report_period(kind, date_s)
    conn = db()
    cost = POINT_COST[kind]
    points = int(u["points"] or 0)

    r = conn.execute("SELECT * FROM reports WHERE user_id=? AND kind=? AND period_start=?",
                     (u["id"], kind, start_s)).fetchone()

    def as_dict(row):
        return {"id": row["id"], "kind": row["kind"], "title": row["title"], "content": row["content"],
                "startDate": row["period_start"], "endDate": row["period_end"],
                "updateTime": row["update_time"], "model": row["model"]}

    if r and not force:
        return ok({"report": as_dict(r), "aiEnabled": 1, "points": points, "cost": cost,
                   "cached": True, "shortPoints": False})

    if manual_only or points < cost:
        content = ""
        title = f"{'周报' if kind == 'weekly' else '日报'} {start_s}" + (f" ~ {end_s}" if kind == "weekly" else "")
        if r:
            conn.execute("UPDATE reports SET update_time=? WHERE id=?", (now_str(), r["id"]))
            conn.commit()
            return ok({"report": as_dict(conn.execute("SELECT * FROM reports WHERE id=?",
                                                      (r["id"],)).fetchone()),
                       "aiEnabled": 1, "points": points, "cost": cost,
                       "cached": False, "shortPoints": points < cost})
        cur = conn.execute(
            "INSERT INTO reports(user_id, kind, period_start, period_end, title, content, model, update_time)"
            " VALUES (?,?,?,?,?,?,?,?)",
            (u["id"], kind, start_s, end_s, title, content, "", now_str()))
        conn.commit()
        return ok({"report": as_dict(conn.execute("SELECT * FROM reports WHERE id=?",
                                                  (cur.lastrowid,)).fetchone()),
                   "aiEnabled": 1, "points": points, "cost": cost,
                   "cached": False, "shortPoints": points < cost})

    # 本地模板生成（不调用外部大模型）
    content, _t, _d, _m = build_report_content(conn, u["id"], kind, start_s, end_s)
    title = f"{'周报' if kind == 'weekly' else '日报'}" + (f" {start_s} ~ {end_s}" if kind == "weekly" else f" {start_s}")
    model = "local-summary-v1"
    if r:
        conn.execute("UPDATE reports SET title=?, content=?, model=?, update_time=? WHERE id=?",
                     (title, content, model, now_str(), r["id"]))
        rid = r["id"]
    else:
        cur = conn.execute(
            "INSERT INTO reports(user_id, kind, period_start, period_end, title, content, model, update_time)"
            " VALUES (?,?,?,?,?,?,?,?)",
            (u["id"], kind, start_s, end_s, title, content, model, now_str()))
        rid = cur.lastrowid
    new_points = add_points(conn, u["id"], -cost, "ai_report",
                            "生成周报" if kind == "weekly" else "生成日报",
                            f"{start_s}" + (f" ~ {end_s}" if kind == "weekly" else ""))
    conn.commit()
    return ok({"report": as_dict(conn.execute("SELECT * FROM reports WHERE id=?", (rid,)).fetchone()),
               "aiEnabled": 1, "points": new_points, "cost": cost,
               "cached": False, "shortPoints": False})


@app.put("/api/memo/report/<int:rid>")
def api_report_save(rid):
    u = require_login()
    if not u:
        return err("登录已失效", 401)
    conn = db()
    if not conn.execute("SELECT 1 FROM reports WHERE id=? AND user_id=?", (rid, u["id"])).fetchone():
        return err("汇报不存在")
    d = request.get_json(silent=True) or {}
    conn.execute("UPDATE reports SET title=?, content=?, update_time=? WHERE id=?",
                 ((d.get("title") or "").strip(), d.get("content") or "", now_str(), rid))
    conn.commit()
    return ok(None, "已保存")


@app.post("/api/memo/report/<int:rid>/send")
def api_report_send(rid):
    u = require_login()
    if not u:
        return err("登录已失效", 401)
    is_member, _, _ = member_info(u)
    if not is_member:
        return vip_required("团队协作")
    d = request.get_json(silent=True) or {}
    tid = int(d.get("teamId") or 0)
    to_user = int(d.get("toUser") or 0)
    conn = db()
    r = conn.execute("SELECT * FROM reports WHERE id=? AND user_id=?", (rid, u["id"])).fetchone()
    if not r:
        return err("汇报不存在")
    t = conn.execute("SELECT * FROM teams WHERE id=? AND deleted=0", (tid,)).fetchone()
    if not t:
        return err("团队不存在")
    m = conn.execute("SELECT * FROM memberships WHERE team_id=? AND user_id=? AND status='joined'",
                     (tid, to_user)).fetchone()
    if not m:
        return err("接收人不在该团队")
    conn.execute("INSERT INTO report_sends(report_id, team_id, from_user, to_user, note, send_time)"
                 " VALUES (?,?,?,?,?,?)",
                 (rid, tid, u["id"], to_user, (d.get("note") or "").strip(), now_str()))
    conn.commit()
    return ok(None, "已发送给组长")


@app.get("/api/memo/report/<int:rid>/replies")
def api_report_replies(rid):
    u = require_login()
    if not u:
        return err("登录已失效", 401)
    conn = db()
    r = conn.execute("SELECT * FROM reports WHERE id=?", (rid,)).fetchone()
    if not r:
        return err("汇报不存在")
    sent = conn.execute("SELECT * FROM report_sends WHERE report_id=? AND (from_user=? OR to_user=?)",
                        (rid, u["id"], u["id"])).fetchone()
    if r["user_id"] != u["id"] and not sent:
        return err("无权查看")
    rows = conn.execute("SELECT rr.*, us.nickname FROM report_replies rr "
                        "JOIN users us ON us.id=rr.user_id WHERE rr.report_id=? ORDER BY rr.id",
                        (rid,)).fetchall()
    partner = ""
    if sent:
        other = sent["to_user"] if sent["from_user"] == u["id"] else sent["from_user"]
        o = conn.execute("SELECT nickname, username FROM users WHERE id=?", (other,)).fetchone()
        if o:
            partner = o["nickname"] or o["username"]
    return ok({"list": [{"id": x["id"], "nickname": x["nickname"], "content": x["content"],
                         "createTime": x["create_time"], "mine": x["user_id"] == u["id"]} for x in rows],
               "partnerName": partner})


@app.post("/api/memo/report/<int:rid>/reply")
def api_report_reply(rid):
    u = require_login()
    if not u:
        return err("登录已失效", 401)
    d = request.get_json(silent=True) or {}
    content = (d.get("content") or "").strip()
    if not content:
        return err("先写点内容再回复")
    conn = db()
    r = conn.execute("SELECT * FROM reports WHERE id=?", (rid,)).fetchone()
    if not r:
        return err("汇报不存在")
    sent = conn.execute("SELECT * FROM report_sends WHERE report_id=? AND (from_user=? OR to_user=?)",
                        (rid, u["id"], u["id"])).fetchone()
    if r["user_id"] != u["id"] and not sent:
        return err("无权回复")
    conn.execute("INSERT INTO report_replies(report_id, user_id, content, create_time)"
                 " VALUES (?,?,?,?)", (rid, u["id"], content, now_str()))
    conn.commit()
    return ok(None, "已回复")


@app.post("/api/memo/report/<int:rid>/replies/read")
def api_report_replies_read(rid):
    u = require_login()
    if not u:
        return err("登录已失效", 401)
    conn = db()
    conn.execute("UPDATE report_replies SET read_time=? WHERE report_id=? AND user_id<>? AND read_time=''",
                 (now_str(), rid, u["id"]))
    conn.commit()
    return ok(None, "已读")


@app.get("/api/memo/report/inbox")
def api_report_inbox():
    u = require_login()
    if not u:
        return err("登录已失效", 401)
    page = max(1, int(request.args.get("page", 1) or 1))
    page_size = max(1, min(50, int(request.args.get("pageSize", 10) or 10)))
    conn = db()
    rows = conn.execute(
        "SELECT rs.*, rp.kind, rp.title, rp.content, rp.period_start, rp.period_end, "
        "us.nickname from_nick, t.name team_name FROM report_sends rs "
        "JOIN reports rp ON rp.id=rs.report_id JOIN users us ON us.id=rs.from_user "
        "JOIN teams t ON t.id=rs.team_id WHERE rs.to_user=? ORDER BY rs.id DESC",
        (u["id"],)).fetchall()
    page_rows, page, pages, total = paginate(rows, page, page_size)
    out = []
    for r in page_rows:
        reply_count = conn.execute("SELECT COUNT(*) c FROM report_replies WHERE report_id=?",
                                   (r["report_id"],)).fetchone()["c"]
        unread = conn.execute("SELECT COUNT(*) c FROM report_replies WHERE report_id=? AND user_id<>? "
                              "AND read_time=''", (r["report_id"], u["id"])).fetchone()["c"]
        conn.execute("UPDATE report_replies SET read_time=? WHERE report_id=? AND user_id<>? AND read_time=''",
                     (now_str(), r["report_id"], u["id"]))
        out.append({
            "report": {"id": r["report_id"], "kind": r["kind"], "title": r["title"],
                       "content": r["content"]},
            "fromNickname": r["from_nick"], "teamName": r["team_name"],
            "sendTime": r["send_time"], "note": r["note"] or "",
            "unread": unread, "replyCount": reply_count,
        })
    conn.commit()
    return ok({"list": out, "page": page, "pages": pages, "total": total, "pageSize": page_size})


@app.get("/api/memo/report/sent")
def api_report_sent():
    u = require_login()
    if not u:
        return err("登录已失效", 401)
    page = max(1, int(request.args.get("page", 1) or 1))
    page_size = max(1, min(50, int(request.args.get("pageSize", 10) or 10)))
    conn = db()
    rows = conn.execute("SELECT * FROM reports WHERE user_id=? ORDER BY id DESC", (u["id"],)).fetchall()
    page_rows, page, pages, total = paginate(rows, page, page_size)
    out = []
    for r in page_rows:
        sends = conn.execute("SELECT * FROM report_sends WHERE report_id=? ORDER BY id DESC",
                             (r["id"],)).fetchall()
        last = sends[0] if sends else None
        to_nick = ""
        received = False
        if last:
            o = conn.execute("SELECT nickname, username FROM users WHERE id=?",
                             (last["to_user"],)).fetchone()
            to_nick = (o["nickname"] or o["username"]) if o else ""
            received = bool(conn.execute("SELECT 1 FROM report_replies WHERE report_id=? AND user_id=? "
                                        "AND read_time<>''", (r["id"], last["to_user"])).fetchone())
        reply_count = conn.execute("SELECT COUNT(*) c FROM report_replies WHERE report_id=?",
                                   (r["id"],)).fetchone()["c"]
        unread = conn.execute("SELECT COUNT(*) c FROM report_replies WHERE report_id=? AND user_id<>? "
                              "AND read_time=''", (r["id"], u["id"])).fetchone()["c"]
        conn.execute("UPDATE report_replies SET read_time=? WHERE report_id=? AND user_id<>? AND read_time=''",
                     (now_str(), r["id"], u["id"]))
        out.append({
            "id": r["id"], "kind": r["kind"], "title": r["title"], "content": r["content"],
            "periodStart": r["period_start"], "periodEnd": r["period_end"],
            "toNickname": to_nick, "sendId": last["id"] if last else None,
            "received": received, "unread": unread, "replyCount": reply_count,
        })
    conn.commit()
    return ok({"list": out, "page": page, "pages": pages, "total": total, "pageSize": page_size})


# ══════════════════════════ AI / 开放接口 ══════════════════════════
# 设计要点：
#   · 全部走 /api/ai/*，与前端 /api/memo/* 隔离，前端零改动
#   · 鉴权：Authorization: Bearer <token>，Token 由 /api/ai/token 签发
#   · 响应统一 {ok, data, error}，比前端契约更利于模型阅读
#   · 写操作支持 Idempotency-Key，避免模型重试造成重复创建

AI_DEFAULT_HORIZON = 30
AI_MAX_BATCH = 50


def ai_ok(data, **extra):
    body = {"ok": True, "data": data}
    body.update(extra)
    return jsonify(body)


def ai_err(msg, status=400, code="bad_request"):
    return jsonify({"ok": False, "error": {"code": code, "message": msg}}), status


def ai_user(need_write=False):
    """AI 接口专用鉴权：无身份时直接 401 JSON，不跳登录页。

    need_write=True 时校验 Token scope：只读 Token 拒绝写操作（403）。
    Cookie 会话（用户在浏览器里）视为全权，不受 scope 限制。
    """
    u = current_user()
    if u is None:
        if g.get("api_invalid"):
            return None, ai_err("Token 无效或已被吊销", 401, "invalid_token")
        return None, ai_err("缺少凭证：请携带 Authorization: Bearer <token>", 401, "unauthorized")
    if need_write and session.get("uid") is None:
        tok = g.get("api_token")
        scope = (tok["scope"] if tok is not None else "read,write") or ""
        parts = {x.strip() for x in scope.split(",")}
        if "write" not in parts:
            return None, ai_err("该 Token 为只读权限，不能执行写操作", 403, "read_only_token")
    return u, None


def ai_body():
    return request.get_json(silent=True) or {}


def idem_lookup(conn, uid, key):
    if not key:
        return None
    row = conn.execute("SELECT response FROM idempotency WHERE key = ? AND user_id = ?",
                       (f"{uid}:{key}", uid)).fetchone()
    return json.loads(row["response"]) if row else None


def idem_store(conn, uid, key, payload):
    """记录幂等结果。key 按「用户 + key」隔离，避免不同客户端相同 key 互相覆盖。"""
    if not key:
        return
    conn.execute("INSERT OR REPLACE INTO idempotency(key, user_id, response, created_at) "
                 "VALUES (?,?,?,?)",
                 (f"{uid}:{key}", uid, json.dumps(payload, ensure_ascii=False), now_str()))
    conn.commit()


def idem_cleanup(conn, ttl_hours=24):
    """清理过期幂等记录，防止表无限增长。"""
    cutoff = (datetime.now() - timedelta(hours=ttl_hours)).strftime(DT_FMT)
    cur = conn.execute("DELETE FROM idempotency WHERE created_at < ?", (cutoff,))
    conn.commit()
    return cur.rowcount


# ─────────── 自然语言解析（规则式，无外部模型依赖） ───────────

CN_NUM = {"一": 1, "二": 2, "两": 2, "三": 3, "四": 4, "五": 5, "六": 6, "七": 7,
          "八": 8, "九": 9, "十": 10, "十一": 11, "十二": 12}
WEEKDAY_CN = {"一": 0, "二": 1, "三": 2, "四": 3, "五": 4, "六": 5, "日": 6, "天": 6,
              "1": 0, "2": 1, "3": 2, "4": 3, "5": 4, "6": 5, "7": 6}


def _cn2int(s):
    s = str(s).strip()
    if s.isdigit():
        return int(s)
    if s in CN_NUM:
        return CN_NUM[s]
    if s.startswith("十") and len(s) == 2 and s[1] in CN_NUM:
        return 10 + CN_NUM[s[1]]
    return None


def parse_natural(text, now=None):
    """把一句中文口语解析成日程字段。只做确定性规则，不猜语义。

    返回 dict：title / start / end / level / repeat / tags / matched（命中的时间片段）
    未识别的部分保留为标题，绝不臆造时间。
    """
    now = now or datetime.now()
    raw = (text or "").strip()
    if not raw:
        return {"title": "", "matched": [], "start": None, "end": None,
                "level": "middle", "repeat": "", "tags": []}
    work = raw
    matched = []
    base_day = None
    has_date = False

    def take(pattern, flags=0):
        nonlocal work
        m = re.search(pattern, work, flags)
        if not m:
            return None
        matched.append(m.group(0))
        work = work[:m.start()] + " " + work[m.end():]
        return m

    # ── 日期 ──
    m = take(r"(今天|今日|明天|明日|后天|大后天)")
    if m:
        off = {"今天": 0, "今日": 0, "明天": 1, "明日": 1, "后天": 2, "大后天": 3}[m.group(1)]
        base_day = (now + timedelta(days=off)).date()
        has_date = True

    if base_day is None:
        m = take(r"(下个?月|这个?月|本月)\s*(\d{1,2})\s*[号日]")
        if m:
            delta = 1 if m.group(1).startswith("下") else 0
            y, mo = now.year, now.month + delta
            if mo > 12:
                y, mo = y + 1, mo - 12
            d = min(int(m.group(2)), pycal.monthrange(y, mo)[1])
            base_day = date(y, mo, d)
            has_date = True
        else:
            m = take(r"(\d{1,2})\s*月\s*(\d{1,2})\s*[号日]")
            if m:
                y = now.year + (1 if int(m.group(1)) < now.month else 0)
                mo, d = int(m.group(1)), int(m.group(2))
                d = min(d, pycal.monthrange(y, mo)[1])
                base_day = date(y, mo, d)
                has_date = True

    if base_day is None:
        m = take(r"(\d{4})\s*[-/.]\s*(\d{1,2})\s*[-/.]\s*(\d{1,2})")
        if m:
            try:
                base_day = date(int(m.group(1)), int(m.group(2)), int(m.group(3)))
                has_date = True
            except ValueError:
                pass

    if base_day is None:
        m = take(r"(下下?个?周|下下?个?星期|下下?个?礼拜)\s*([一二三四五六日天1-7])")
        if m:
            prefix = m.group(1)
            nxt_weeks = 2 if prefix.startswith("下下") else 1
            wd = WEEKDAY_CN.get(m.group(2))
            # 以自然周计算：本周周一 + nxt_weeks 周 + 目标星期偏移
            monday = now.date() - timedelta(days=now.weekday())
            base_day = monday + timedelta(days=7 * nxt_weeks + wd)
            has_date = True
        else:
            m = take(r"(这|本)?\s*(周|星期|礼拜)\s*([一二三四五六日天1-7])")
            if m:
                wd = WEEKDAY_CN.get(m.group(3))
                ahead = (wd - now.weekday()) % 7
                if ahead == 0:
                    ahead = 7        # 提到的是今天，取下一次同一星期
                base_day = (now + timedelta(days=ahead)).date()
                has_date = True

    # ── 时间点 ──
    hour, minute = None, None
    m = take(r"(凌晨|早上|上午|中午|下午|傍晚|晚上|夜里)\s*([0-9]{1,2}|[一二两三四五六七八九十]{1,3})\s*[点时](?:\s*([0-9]{1,2})\s*分?|半)?")
    if m:
        h = _cn2int(m.group(2))
        mi = 30 if m.group(0).endswith("半") else (_cn2int(m.group(3)) if m.group(3) else 0)
        if h is not None:
            seg = m.group(1)
            if seg in ("下午", "傍晚", "晚上", "夜里") and h < 12:
                h += 12
            if seg == "中午" and h < 11:
                h += 12
            if seg == "凌晨" and h == 12:
                h = 0
            hour, minute = h, (mi or 0)
    if hour is None:
        m = take(r"([0-9]{1,2})\s*[:：]\s*([0-9]{1,2})")
        if m:
            hour, minute = int(m.group(1)), int(m.group(2))
    if hour is None:
        m = take(r"([0-9]{1,2}|[一二两三四五六七八九十]{1,3})\s*[点时](?:\s*([0-9]{1,2})\s*分?|半)?")
        if m:
            h = _cn2int(m.group(1))
            mi = 30 if m.group(0).endswith("半") else (_cn2int(m.group(2)) if m.group(2) else 0)
            if h is not None and 0 <= h <= 23:
                hour, minute = h, (mi or 0)

    # ── 时长 → 截止 ──
    dur_min = None
    m = take(r"([0-9]{1,3}|[一二两三四五六七八九十]{1,3}|半)\s*个?\s*小时")
    if m:
        v = _cn2int(m.group(1))
        dur_min = 30 if m.group(1) == "半" else (v * 60 if v else None)
    else:
        m = take(r"([0-9]{1,3}|[一二两三四五六七八九十]{1,3})\s*分钟")
        if m:
            dur_min = _cn2int(m.group(1))
    if dur_min is None and re.search(r"(半小时|一个半小时)", work):
        if re.search(r"一个半小时", work):
            dur_min = 90
            matched.append("一个半小时")
            work = re.sub(r"(一个半小时)\s*(后|以后|之后)?", " ", work, count=1)
        else:
            dur_min = 30
            matched.append("半小时")
            work = re.sub(r"(半小时)\s*(后|以后|之后)?", " ", work, count=1)

    # ── 优先级 ──
    level = "middle"
    if re.search(r"(非常紧急|很紧急|紧急|重要|优先|高优|尽快|马上|最要紧)", work):
        level = "high"
        take(r"(非常紧急|很紧急|紧急|重要|优先|高优|尽快|马上|最要紧)")
    elif re.search(r"(不急|次要|随意|有空|低优|慢慢)", work):
        level = "low"
        take(r"(不急|次要|随意|有空|低优|慢慢)")

    # ── 重复 ──
    repeat = ""
    if take(r"(每天|每日|天天)"):
        repeat = "day"
    elif take(r"(每周|每星期|每礼拜)"):
        repeat = "week"
    elif take(r"(每月|每个月)"):
        repeat = "month"
    elif take(r"(工作日|每个工作日)"):
        repeat = "day"

    # ── 标签 ──
    tags = []
    for m in re.finditer(r"#([\w\u4e00-\u9fa5-]{1,30})", raw):
        tags.append(m.group(1))
    for t in tags:
        work = work.replace("#" + t, " ")
        matched.append("#" + t)

    title = re.sub(r"[，,。；;、\s]+", " ", work).strip(" -—:")
    title = re.sub(r"^(在|于|把|给)\s*", "", title).strip()
    # 「N小时后」「N分钟后」的残留尾缀在标题里没有意义，统一清掉
    title = re.sub(r"^(后|以后|之后)\s*", "", title).strip()
    title = re.sub(r"\s*(后|以后|之后)$", "", title).strip()

    # ── 组装时间 ──
    day = base_day or now.date()
    if hour is None and dur_min is None and not has_date:
        start = None
    else:
        # 只给了日期没给时间点时，落在当天 09:00（工作日常规起点），
        # 避免「后天45分钟站会」被塞到当前时分这种奇怪结果
        if hour is not None:
            h, mi = hour, (minute or 0)
        elif has_date:
            h, mi = 9, 0
        else:
            h, mi = now.hour, now.minute
        start = datetime(day.year, day.month, day.day, h, mi)
    end = None
    if start:
        end = start + timedelta(minutes=dur_min) if dur_min else start + timedelta(hours=1)

    return {
        "title": title or raw,
        "start": start, "end": end,
        "level": level, "repeat": repeat, "tags": tags,
        "matched": matched,
    }


def _dt_out(v):
    return v.strftime("%Y-%m-%dT%H:%M") if isinstance(v, datetime) else ""


# ─────────── Token 管理 ───────────

@app.post("/api/ai/token")
def ai_token_create():
    u = current_user()
    if u is None:
        # 允许用账号密码直接换取 Token，方便 CLI / 脚本首次接入
        d0 = ai_body()
        if d0.get("username") and d0.get("password"):
            row = db().execute("SELECT * FROM users WHERE username = ?",
                               (d0["username"].strip(),)).fetchone()
            if row and check_password_hash(row["pwd_hash"], d0["password"]):
                u = row
        if u is None:
            return ai_err("请先登录，或提供 username + password 换取 Token", 401, "unauthorized")

    d = ai_body()
    name = (d.get("name") or "ai-client").strip()[:60]
    scope = (d.get("scope") or "read,write").strip()
    if scope not in ("read", "read,write", "write"):
        return ai_err("scope 只能是 read / write / read,write")
    raw = "memo_" + secrets.token_urlsafe(32)
    conn = db()
    cur = conn.execute(
        "INSERT INTO api_tokens(user_id, name, token_hash, prefix, scope, created_at) "
        "VALUES (?,?,?,?,?,?)",
        (u["id"], name, hash_token(raw), raw[:12], scope, now_str()),
    )
    conn.commit()
    clear_token_cache()
    return ai_ok({
        "id": cur.lastrowid, "name": name, "scope": scope,
        "token": raw,          # 仅此一次返回明文
        "hint": "请立即保存，服务端只存哈希，之后无法再次查看",
    })


@app.get("/api/ai/tokens")
def ai_token_list():
    u, e = ai_user()
    if e:
        return e
    rows = db().execute(
        "SELECT id, name, prefix, scope, created_at, last_used, use_count, revoked "
        "FROM api_tokens WHERE user_id = ? ORDER BY id DESC", (u["id"],)).fetchall()
    return ai_ok([dict(r) for r in rows])


@app.delete("/api/ai/token/<int:tid>")
def ai_token_revoke(tid):
    u, e = ai_user(need_write=True)
    if e:
        return e
    conn = db()
    row = conn.execute("SELECT id FROM api_tokens WHERE id = ? AND user_id = ?",
                       (tid, u["id"])).fetchone()
    if not row:
        return ai_err("Token 不存在", 404, "not_found")
    conn.execute("UPDATE api_tokens SET revoked = 1 WHERE id = ?", (tid,))
    conn.commit()
    clear_token_cache()
    return ai_ok({"id": tid, "revoked": True})


# ─────────── 日程读写 ───────────

def ai_event_view(conn, row):
    it = event_item(conn, row)
    now = datetime.now()
    end = parse_dt(row["end_time"])
    done = row["status"] == "done"
    if done:
        state, left = "done", None
    elif end and end < now:
        state, left = "overdue", int((now - end).total_seconds() // 60)
    else:
        state, left = "doing", int((end - now).total_seconds() // 60) if end else None
    it["state"] = state
    it["minutesLeft"] = left
    return it


def ai_ensure_tag(conn, uid, name):
    name = str(name).strip()[:30]
    if not name:
        return None
    row = conn.execute("SELECT id FROM tags WHERE user_id = ? AND name = ?",
                       (uid, name)).fetchone()
    if row:
        return row["id"]
    count = conn.execute("SELECT COUNT(*) c FROM tags WHERE user_id = ?", (uid,)).fetchone()["c"]
    color = TAG_COLORS[count % len(TAG_COLORS)]
    cur = conn.execute("INSERT INTO tags(user_id, name, color) VALUES (?,?,?)",
                       (uid, name, color))
    return cur.lastrowid


@app.get("/api/ai/events")
def ai_events():
    u, e = ai_user()
    if e:
        return e
    conn = db()
    try:
        page = max(1, int(request.args.get("page", 1)))
        limit = max(1, min(100, int(request.args.get("limit", 20))))
    except ValueError:
        return ai_err("page / limit 必须是整数", 422, "invalid")
    status = (request.args.get("status") or "").strip()
    level = (request.args.get("level") or "").strip()
    kw = (request.args.get("q") or "").strip()
    date_from = (request.args.get("from") or "").strip()
    date_to = (request.args.get("to") or "").strip()
    archived = request.args.get("archived", "0")
    tag = (request.args.get("tag") or "").strip()

    sql = "SELECT * FROM events WHERE user_id = ?"
    params = [u["id"]]
    if archived in ("0", "1"):
        sql += " AND archived = ?"
        params.append(int(archived))
    if status in ("doing", "done"):
        sql += " AND status = ?"
        params.append(status)
    if level in ("high", "middle", "low"):
        sql += " AND level = ?"
        params.append(level)
    if date_from:
        sql += " AND substr(start_time,1,10) >= ?"
        params.append(date_from)
    if date_to:
        sql += " AND substr(start_time,1,10) <= ?"
        params.append(date_to)
    if kw:
        sql += " AND (title LIKE ? OR content LIKE ? OR remark LIKE ?)"
        params += [f"%{kw}%"] * 3
    if tag.isdigit():
        sql += " AND id IN (SELECT event_id FROM event_tag WHERE tag_id = ?)"
        params.append(int(tag))

    rows = sort_events(conn.execute(sql, params).fetchall())
    total = len(rows)
    # 页码越界时归一到最后一页，并显式告知，避免模型把「空列表」误读为「没有数据」
    pages = max(1, (total + limit - 1) // limit)
    requested = page
    if page > pages:
        page = pages
    start = (page - 1) * limit
    chunk = rows[start:start + limit]
    data = {
        "items": [ai_event_view(conn, r) for r in chunk],
        "page": page, "limit": limit, "total": total, "pages": pages,
        "hasMore": start + len(chunk) < total,
    }
    if requested != page:
        data["pageAdjusted"] = {"requested": requested, "applied": page,
                                "hint": f"共 {total} 条、每页 {limit} 条，请求页超出范围已归一到第 {page} 页"}
    return ai_ok(data)


@app.get("/api/ai/event/<int:eid>")
def ai_event_get(eid):
    u, e = ai_user()
    if e:
        return e
    conn = db()
    row = conn.execute("SELECT * FROM events WHERE id = ? AND user_id = ?",
                       (eid, u["id"])).fetchone()
    if not row:
        return ai_err("日程不存在", 404, "not_found")
    return ai_ok(ai_event_view(conn, row))


@app.post("/api/ai/event")
def ai_event_create():
    u, e = ai_user(need_write=True)
    if e:
        return e
    conn = db()
    key = request.headers.get("Idempotency-Key") or ""
    cached = idem_lookup(conn, u["id"], key)
    if cached is not None:
        return ai_ok(cached, idempotent=True)

    d = ai_body()
    text = (d.get("text") or "").strip()

    if text and not d.get("title"):
        parsed = parse_natural(text)
        if not parsed["start"]:
            return jsonify({
                "ok": False,
                "error": {"code": "need_time",
                          "message": "没能从这句话里确定时间，请显式给出 start"},
                "data": {
                    "parsed": {"title": parsed["title"], "level": parsed["level"],
                               "repeat": parsed["repeat"], "tags": parsed["tags"],
                               "matched": parsed["matched"], "start": None, "end": None},
                    "hint": "可改为传 {title, start:'2026-10-29T15:00', durationMinutes:60}",
                },
            }), 422
        payload = {"title": parsed["title"], "start": _dt_out(parsed["start"]),
                   "end": _dt_out(parsed["end"]), "level": parsed["level"],
                   "repeat": parsed["repeat"], "tags": parsed["tags"]}
        source = {"mode": "natural_language", "input": text, "matched": parsed["matched"]}
    else:
        payload = {"title": (d.get("title") or "").strip(),
                   "start": d.get("start") or "", "end": d.get("end") or "",
                   "level": d.get("level") or "middle",
                   "repeat": d.get("repeat") or "", "tags": d.get("tags") or []}
        source = {"mode": "structured"}

    if not payload["title"]:
        return ai_err("title 不能为空", 422, "invalid")
    if len(payload["title"]) > 200:
        return ai_err("title 不能超过 200 字", 422, "invalid")
    if payload["level"] not in ("high", "middle", "low", ""):
        return ai_err("level 只能是 high / middle / low", 422, "invalid_level")
    if payload["repeat"]:
        bad = validate_rule(payload["repeat"])
        if bad:
            return ai_err(bad, 422, "invalid_repeat")
    start = parse_dt(payload["start"]) or parse_dt(payload["end"])
    end = parse_dt(payload["end"])
    if end is None and start is not None:
        dur = d.get("durationMinutes")
        end = start + timedelta(minutes=int(dur)) if dur else start + timedelta(hours=1)
    if start is None or end is None:
        return ai_err("需要 start（或 end）与可解析的时间格式 YYYY-MM-DDTHH:MM",
                      422, "invalid_time")
    if end < start:
        return ai_err("end 不能早于 start", 422, "invalid_time")

    now = now_str()
    cur = conn.execute(
        "INSERT INTO events(user_id, title, content, start_time, end_time, remark, level, status,"
        " repeat_rule, created_at, updated_at) VALUES (?,?,?,?,?,?,?,?,?,?,?)",
        (u["id"], payload["title"], (d.get("content") or "").strip(), fmt_dt(start), fmt_dt(end),
         (d.get("remark") or "").strip(), payload["level"] or "middle", "doing",
         payload["repeat"] or "", now, now),
    )
    eid = cur.lastrowid
    for name in (payload["tags"] or [])[:10]:
        tid = ai_ensure_tag(conn, u["id"], name)
        if tid:
            conn.execute("INSERT OR IGNORE INTO event_tag(event_id, tag_id) VALUES (?,?)", (eid, tid))
    for i, s in enumerate(d.get("subtasks") or []):
        t = (s.get("title") if isinstance(s, dict) else str(s) or "").strip()
        if t:
            conn.execute("INSERT INTO subtasks(event_id, title, done, sort) VALUES (?,?,?,?)",
                         (eid, t, 0, i))
    conn.commit()

    row = conn.execute("SELECT * FROM events WHERE id = ?", (eid,)).fetchone()
    out = ai_event_view(conn, row)
    out["source"] = source
    idem_store(conn, u["id"], key, out)
    return ai_ok(out)


def spawn_next_occurrence(conn, uid, row):
    """按重复规则生成下一条；同日同标题已存在则不重复建。"""
    start = parse_dt(row["start_time"])
    end = parse_dt(row["end_time"])
    nxt = next_occurrence(start, row["repeat_rule"])
    if not nxt:
        return None
    nxt_end = nxt + (end - start)
    until = parse_dt(row["repeat_until"]) if row["repeat_until"] else None
    if until and nxt.date() > until.date():
        return None
    exists = conn.execute(
        "SELECT id FROM events WHERE user_id=? AND title=? AND start_time=? AND archived=0",
        (uid, row["title"], fmt_dt(nxt))).fetchone()
    if exists:
        return None
    now = now_str()
    cur = conn.execute(
        "INSERT INTO events(user_id, title, content, start_time, end_time, remark, level, status,"
        " repeat_rule, repeat_until, created_at, updated_at) VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
        (uid, row["title"], row["content"], fmt_dt(nxt), fmt_dt(nxt_end), row["remark"],
         row["level"], "doing", row["repeat_rule"], row["repeat_until"], now, now),
    )
    for t in event_tags(conn, row["id"]):
        conn.execute("INSERT OR IGNORE INTO event_tag(event_id, tag_id) VALUES (?,?)",
                     (cur.lastrowid, t["id"]))
    conn.commit()
    return cur.lastrowid


@app.patch("/api/ai/event/<int:eid>")
def ai_event_patch(eid):
    u, e = ai_user(need_write=True)
    if e:
        return e
    conn = db()
    row = conn.execute("SELECT * FROM events WHERE id = ? AND user_id = ?",
                       (eid, u["id"])).fetchone()
    if not row:
        return ai_err("日程不存在", 404, "not_found")
    d = ai_body()
    fields, params = [], []
    if "level" in d and d["level"] not in ("high", "middle", "low", ""):
        return ai_err("level 只能是 high / middle / low", 422, "invalid_level")
    if "status" in d and d["status"] not in ("doing", "done", ""):
        return ai_err("status 只能是 doing / done", 422, "invalid_status")
    for k, col in (("title", "title"), ("content", "content"), ("remark", "remark"),
                   ("level", "level"), ("status", "status")):
        if k in d and d[k] != "":
            fields.append(f"{col}=?")
            params.append(d[k])
    for k, col in (("start", "start_time"), ("end", "end_time")):
        if k in d:
            v = parse_dt(d[k])
            if not v:
                return ai_err(f"{k} 格式应为 YYYY-MM-DDTHH:MM", 422, "invalid_time")
            fields.append(f"{col}=?")
            params.append(fmt_dt(v))
    if "archived" in d:
        fields.append("archived=?")
        params.append(1 if d["archived"] else 0)
    if "actualMinutes" in d:
        try:
            fields.append("actual_minutes=?")
            params.append(max(0, min(100000, int(d["actualMinutes"]))))
        except (TypeError, ValueError):
            return ai_err("actualMinutes 必须是整数", 422, "invalid")
    if not fields:
        return ai_err("没有可更新的字段", 422, "no_fields")

    start_new = parse_dt(d.get("start")) if "start" in d else parse_dt(row["start_time"])
    end_new = parse_dt(d.get("end")) if "end" in d else parse_dt(row["end_time"])
    if start_new and end_new and end_new < start_new:
        return ai_err("end 不能早于 start", 422, "invalid_time")

    fields.append("updated_at=?")
    params.append(now_str())
    conn.execute(f"UPDATE events SET {','.join(fields)} WHERE id = ?", params + [eid])

    if "tags" in d:
        conn.execute("DELETE FROM event_tag WHERE event_id = ?", (eid,))
        for name in (d.get("tags") or [])[:10]:
            tid = ai_ensure_tag(conn, u["id"], name)
            if tid:
                conn.execute("INSERT OR IGNORE INTO event_tag(event_id, tag_id) VALUES (?,?)",
                             (eid, tid))
    conn.commit()
    fresh = conn.execute("SELECT * FROM events WHERE id = ?", (eid,)).fetchone()

    spawned = None
    if d.get("status") == "done" and fresh["repeat_rule"]:
        spawned = spawn_next_occurrence(conn, u["id"], fresh)
    return ai_ok({"event": ai_event_view(conn, fresh), "spawnedId": spawned})


@app.post("/api/ai/event/<int:eid>/complete")
def ai_event_complete(eid):
    """语义化完成：等价于 PATCH status=done + 按规则生成下一条。"""
    u, e = ai_user(need_write=True)
    if e:
        return e
    conn = db()
    row = conn.execute("SELECT * FROM events WHERE id = ? AND user_id = ?",
                       (eid, u["id"])).fetchone()
    if not row:
        return ai_err("日程不存在", 404, "not_found")
    d = ai_body()
    target = "done" if d.get("done", True) else "doing"
    conn.execute("UPDATE events SET status=?, updated_at=? WHERE id=?",
                 (target, now_str(), eid))
    conn.commit()
    fresh = conn.execute("SELECT * FROM events WHERE id=?", (eid,)).fetchone()
    spawned = spawn_next_occurrence(conn, u["id"], fresh) if target == "done" else None
    return ai_ok({"event": ai_event_view(conn, fresh), "spawnedId": spawned})


@app.delete("/api/ai/event/<int:eid>")
def ai_event_delete(eid):
    u, e = ai_user(need_write=True)
    if e:
        return e
    conn = db()
    row = conn.execute("SELECT id, title FROM events WHERE id=? AND user_id=?",
                       (eid, u["id"])).fetchone()
    if not row:
        return ai_err("日程不存在", 404, "not_found")
    conn.execute("DELETE FROM events WHERE id=?", (eid,))
    conn.execute("DELETE FROM event_tag WHERE event_id=?", (eid,))
    conn.execute("DELETE FROM subtasks WHERE event_id=?", (eid,))
    conn.commit()
    return ai_ok({"deletedId": eid, "title": row["title"]})


@app.post("/api/ai/events/batch")
def ai_events_batch():
    """批量建日程：单次最多 50 条，逐条返回结果，部分失败不影响整体。"""
    u, e = ai_user(need_write=True)
    if e:
        return e
    d = ai_body()
    items = d.get("items") or []
    if not isinstance(items, list) or not items:
        return ai_err("items 必须是非空数组", 422, "invalid")
    if len(items) > AI_MAX_BATCH:
        return ai_err(f"单次最多 {AI_MAX_BATCH} 条", 422, "too_many")
    conn = db()
    results = []
    for idx, it in enumerate(items):
        if not isinstance(it, dict):
            it = {"text": str(it)}
        text = (it.get("text") or "").strip()
        if text and not it.get("title"):
            p = parse_natural(text)
            if not p["start"]:
                results.append({"index": idx, "ok": False, "error": "无法确定时间"})
                continue
            t, s, en = p["title"], _dt_out(p["start"]), _dt_out(p["end"])
            lv, rp, tg = p["level"], p["repeat"], p["tags"]
        else:
            t = (it.get("title") or "").strip()
            s, en = it.get("start") or "", it.get("end") or ""
            lv, rp, tg = it.get("level") or "middle", it.get("repeat") or "", it.get("tags") or []
        sd = parse_dt(s) or parse_dt(en)
        ed = parse_dt(en)
        if ed is None and sd is not None:
            dur = it.get("durationMinutes")
            ed = sd + timedelta(minutes=int(dur)) if dur else sd + timedelta(hours=1)
        if not t or sd is None or ed is None:
            results.append({"index": idx, "ok": False, "error": "title 与时间不完整"})
            continue
        if ed < sd:
            results.append({"index": idx, "ok": False, "error": "end 早于 start"})
            continue
        now = now_str()
        cur = conn.execute(
            "INSERT INTO events(user_id, title, content, start_time, end_time, remark, level,"
            " status, repeat_rule, created_at, updated_at) VALUES (?,?,?,?,?,?,?,?,?,?,?)",
            (u["id"], t, (it.get("content") or "").strip(), fmt_dt(sd), fmt_dt(ed),
             (it.get("remark") or "").strip(), lv, "doing", rp, now, now),
        )
        eid = cur.lastrowid
        for name in (tg or [])[:10]:
            tid = ai_ensure_tag(conn, u["id"], name)
            if tid:
                conn.execute("INSERT OR IGNORE INTO event_tag(event_id, tag_id) VALUES (?,?)",
                             (eid, tid))
        results.append({"index": idx, "ok": True, "id": eid, "title": t,
                        "start": fmt_dt(sd), "end": fmt_dt(ed)})
    conn.commit()
    return ai_ok({"results": results,
                  "succeeded": sum(1 for r in results if r["ok"]),
                  "failed": sum(1 for r in results if not r["ok"])})


# ─────────── 查询 / 统计 ───────────

@app.get("/api/ai/agenda")
def ai_agenda():
    """按天给议程：默认今天，days 可指定 1~31 天。"""
    u, e = ai_user()
    if e:
        return e
    start_s = (request.args.get("date") or today_str()).strip()
    base = parse_dt(start_s)
    if not base:
        return ai_err("date 格式应为 YYYY-MM-DD", 422, "invalid")
    try:
        days = max(1, min(31, int(request.args.get("days", 1))))
    except ValueError:
        return ai_err("days 必须是整数", 422, "invalid")
    conn = db()
    out = []
    for i in range(days):
        day = (base.date() + timedelta(days=i)).strftime(DATE_FMT)
        rows = sort_events(conn.execute(
            "SELECT * FROM events WHERE user_id=? AND archived=0 AND substr(start_time,1,10)=?",
            (u["id"], day)).fetchall())
        out.append({
            "date": day,
            "weekday": "一二三四五六日"[date.fromisoformat(day).weekday()],
            "count": len(rows),
            "done": sum(1 for r in rows if r["status"] == "done"),
            "items": [ai_event_view(conn, r) for r in rows],
        })
    return ai_ok({"from": out[0]["date"], "to": out[-1]["date"], "days": out})


@app.get("/api/ai/brief")
def ai_brief():
    """一句话可读的现状摘要，供模型直接引用，避免它自己算错。"""
    u, e = ai_user()
    if e:
        return e
    conn = db()
    rows = conn.execute("SELECT * FROM events WHERE user_id = ?", (u["id"],)).fetchall()
    now = datetime.now()
    live = [r for r in rows if not r["archived"]]
    done = [r for r in live if r["status"] == "done"]
    doing = [r for r in live if r["status"] != "done"]
    overdue = [r for r in doing if (parse_dt(r["end_time"]) or now) < now]
    today = [r for r in live if str(r["start_time"])[:10] == today_str()]
    soon = sorted([r for r in doing if (parse_dt(r["end_time"]) or now) >= now],
                  key=lambda r: parse_dt(r["end_time"]))[:5]
    is_member, expire, left = member_info(u)
    invite_n = conn.execute("SELECT COUNT(*) c FROM invites WHERE invitee_id=? AND status='pending'",
                            (u["id"],)).fetchone()["c"]
    parts = [f"共 {len(live)} 条日程：进行中 {len(doing)}、已完成 {len(done)}、已超时 {len(overdue)}",
             f"今天 {len(today)} 条"]
    if soon:
        nxt = soon[0]
        mins = int((parse_dt(nxt["end_time"]) - now).total_seconds() // 60)
        parts.append(f"最近截止「{nxt['title']}」剩 {mins} 分钟")
    if invite_n:
        parts.append(f"{invite_n} 条团队邀请待处理")
    return ai_ok({
        "summary": "；".join(parts) + "。",
        "counts": {"total": len(live), "doing": len(doing), "done": len(done),
                   "overdue": len(overdue), "archived": len(rows) - len(live),
                   "today": len(today)},
        "upcoming": [{"id": r["id"], "title": r["title"], "end": fmt_dt(r["end_time"]),
                      "minutesLeft": int((parse_dt(r["end_time"]) - now).total_seconds() // 60)}
                     for r in soon],
        "overdueTitles": [{"id": r["id"], "title": r["title"], "end": fmt_dt(r["end_time"])}
                          for r in overdue[:8]],
        "member": {"isMember": is_member, "expireAt": expire, "daysLeft": left,
                   "points": int(u["points"] or 0)},
    })


@app.get("/api/ai/tags")
def ai_tags():
    u, e = ai_user()
    if e:
        return e
    conn = db()
    rows = conn.execute(
        "SELECT t.id, t.name, t.color, COUNT(et.event_id) usage "
        "FROM tags t LEFT JOIN event_tag et ON et.tag_id = t.id "
        "LEFT JOIN events e ON e.id = et.event_id AND e.archived = 0 "
        "WHERE t.user_id = ? GROUP BY t.id ORDER BY t.id", (u["id"],)).fetchall()
    return ai_ok([dict(r) for r in rows])


@app.post("/api/ai/tag")
def ai_tag_create():
    u, e = ai_user(need_write=True)
    if e:
        return e
    name = (ai_body().get("name") or "").strip()[:30]
    if not name:
        return ai_err("name 不能为空", 422, "invalid")
    conn = db()
    ctx = conn.execute("SELECT id, name, color FROM tags WHERE user_id=? AND name=?",
                       (u["id"], name)).fetchone()
    if ctx:
        return ai_ok({"id": ctx["id"], "name": ctx["name"], "color": ctx["color"],
                      "existed": True})
    is_member, _, _ = member_info(u)
    count = conn.execute("SELECT COUNT(*) c FROM tags WHERE user_id=?", (u["id"],)).fetchone()["c"]
    if not is_member and count >= FREE_TAG_LIMIT:
        return ai_err(f"免费版最多 {FREE_TAG_LIMIT} 个标签，开通会员可无限", 402, "member_required")
    tid = ai_ensure_tag(conn, u["id"], name)
    conn.commit()
    row = conn.execute("SELECT id, name, color FROM tags WHERE id=?", (tid,)).fetchone()
    return ai_ok({"id": row["id"], "name": row["name"], "color": row["color"], "existed": False})


@app.delete("/api/ai/tag/<int:tid>")
def ai_tag_delete(tid):
    u, e = ai_user(need_write=True)
    if e:
        return e
    conn = db()
    row = conn.execute("SELECT id, name FROM tags WHERE id=? AND user_id=?",
                       (tid, u["id"])).fetchone()
    if not row:
        return ai_err("标签不存在", 404, "not_found")
    conn.execute("DELETE FROM event_tag WHERE tag_id=?", (tid,))
    conn.execute("DELETE FROM tags WHERE id=?", (tid,))
    conn.commit()
    return ai_ok({"deletedId": tid, "name": row["name"]})


@app.post("/api/ai/subtask/<int:sid>/toggle")
def ai_subtask_toggle(sid):
    u, e = ai_user(need_write=True)
    if e:
        return e
    conn = db()
    row = conn.execute(
        "SELECT s.* FROM subtasks s JOIN events e ON e.id = s.event_id "
        "WHERE s.id=? AND e.user_id=?", (sid, u["id"])).fetchone()
    if not row:
        return ai_err("子任务不存在", 404, "not_found")
    d = ai_body()
    done = (not bool(row["done"])) if d.get("done") is None else bool(d.get("done"))
    conn.execute("UPDATE subtasks SET done=? WHERE id=?", (1 if done else 0, sid))
    conn.commit()
    total = conn.execute("SELECT COUNT(*) c FROM subtasks WHERE event_id=?",
                         (row["event_id"],)).fetchone()["c"]
    finished = conn.execute("SELECT COUNT(*) c FROM subtasks WHERE event_id=? AND done=1",
                            (row["event_id"],)).fetchone()["c"]
    return ai_ok({"subtaskId": sid, "done": done, "eventId": row["event_id"],
                  "progress": {"done": finished, "total": total,
                               "percent": round(finished * 100 / total) if total else 0}})


@app.get("/api/ai/calendar")
def ai_calendar():
    u, e = ai_user()
    if e:
        return e
    try:
        year = int(request.args.get("year") or date.today().year)
        month = int(request.args.get("month") or date.today().month)
    except ValueError:
        return ai_err("year / month 必须是整数", 422, "invalid")
    if not 1 <= month <= 12:
        return ai_err("month 需在 1~12", 422, "invalid")
    conn = db()
    rows = conn.execute(
        "SELECT * FROM events WHERE user_id=? AND archived=0 AND substr(start_time,1,7)=?",
        (u["id"], f"{year:04d}-{month:02d}")).fetchall()
    marks = {}
    for r in rows:
        d = str(r["start_time"])[:10]
        marks.setdefault(d, []).append({"id": r["id"], "title": r["title"],
                                        "level": r["level"], "status": r["status"]})
    return ai_ok({"year": year, "month": month,
                  "days": [{"date": k, "count": len(v), "items": v}
                           for k, v in sorted(marks.items())]})


@app.post("/api/ai/parse")
def ai_parse():
    """只解析不落库：模型可先预览自己的理解是否正确。"""
    u, e = ai_user()
    if e:
        return e
    d = ai_body()
    text = (d.get("text") or "").strip()
    if not text:
        return ai_err("text 不能为空", 422, "invalid")
    p = parse_natural(text, parse_dt(d.get("now")) or datetime.now())
    return ai_ok({
        "title": p["title"], "start": _dt_out(p["start"]), "end": _dt_out(p["end"]),
        "level": p["level"], "repeat": p["repeat"], "tags": p["tags"],
        "matched": p["matched"], "understood": bool(p["start"]),
    })


@app.get("/api/ai/schema")
def ai_schema():
    """自描述：让模型不用读文档就知道能调什么。"""
    return ai_ok({
        "name": "calendar-memo-ai-api",
        "version": "1.1",
        "auth": {"type": "bearer", "header": "Authorization: Bearer <token>",
                 "issue": "POST /api/ai/token 传 {username,password}，或已登录 Cookie"},
        "conventions": {
            "response": "{ok:true,data:...} / {ok:false,error:{code,message}}",
            "time": "YYYY-MM-DDTHH:MM",
            "pagination": "page(1起) + limit(1~100)",
            "idempotency": "写接口可带 Idempotency-Key 头避免重试重复创建",
        },
        "endpoints": [
            {"method": "POST", "path": "/api/ai/event",
             "desc": "新建日程。传 text 走自然语言解析，或传 title+start+end",
             "body": {"text": "明天下午3点开会 #项目A", "title": "可选",
                      "start": "YYYY-MM-DDTHH:MM", "end": "可选",
                      "durationMinutes": "未给 end 时的时长",
                      "level": "high|middle|low",
                      "repeat": "day|week|month|complex；可带参数："
                                "week|interval:2（每两周）、"
                                "complex|monthly|2|2（每月第2个星期2）、"
                                "complex|monthly|last|4（每月最后一个星期五）",
                      "tags": ["项目A"], "content": "详情", "remark": "备注",
                      "subtasks": ["步骤1"]}},
            {"method": "GET", "path": "/api/ai/events",
             "desc": "列表，支持 status/level/q/from/to/tag/archived/page/limit"},
            {"method": "GET", "path": "/api/ai/event/<id>", "desc": "单条详情"},
            {"method": "PATCH", "path": "/api/ai/event/<id>",
             "desc": "局部更新任意字段（含 status/archived/tags/actualMinutes）"},
            {"method": "POST", "path": "/api/ai/event/<id>/complete",
             "desc": "标记完成；重复日程自动生成下一条"},
            {"method": "DELETE", "path": "/api/ai/event/<id>", "desc": "删除"},
            {"method": "POST", "path": "/api/ai/events/batch",
             "desc": f"批量建，最多 {AI_MAX_BATCH} 条"},
            {"method": "GET", "path": "/api/ai/agenda", "desc": "按天议程 ?date=&days=1~31"},
            {"method": "GET", "path": "/api/ai/brief", "desc": "现状摘要（含可直引的一句话）"},
            {"method": "GET", "path": "/api/ai/calendar", "desc": "月度打点 ?year=&month="},
            {"method": "GET", "path": "/api/ai/tags", "desc": "标签及使用量"},
            {"method": "POST", "path": "/api/ai/parse", "desc": "只解析不落库"},
            {"method": "POST", "path": "/api/ai/token", "desc": "签发 Token"},
            {"method": "GET", "path": "/api/ai/tokens", "desc": "列出我的 Token"},
            {"method": "DELETE", "path": "/api/ai/token/<id>", "desc": "吊销 Token"},
        ],
        "naturalLanguage": {
            "date": ["今天", "明天", "后天", "下周三", "3月5日", "2026-11-01"],
            "time": ["下午3点", "15:30", "晚上八点半"],
            "duration": ["2小时", "45分钟", "半小时"],
            "level": ["紧急", "重要", "不急", "次要"],
            "repeat": ["每天", "每周", "每月"],
            "tags": ["#标签名"],
        },
        "mcp": {"stdio": "python mcp_server.py",
                "http": "http://127.0.0.1:8000/mcp (Streamable HTTP)", "tools": 14},
    })


# ─────────── 运维辅助 ───────────

def cleanup_uploads(ttl_days=UPLOAD_TTL_DAYS):
    """删除超过 TTL 的凭证图，避免长期累积。返回删除数量。"""
    cutoff = datetime.now().timestamp() - ttl_days * 86400
    removed = 0
    if not os.path.isdir(UPLOAD_DIR):
        return 0
    for fn in os.listdir(UPLOAD_DIR):
        p = os.path.join(UPLOAD_DIR, fn)
        try:
            if os.path.isfile(p) and os.path.getmtime(p) < cutoff:
                os.remove(p)
                removed += 1
        except OSError:
            continue
    return removed


def backup_db(dest=None, tag=""):
    """备份整个数据库到单个 .db 文件，返回备份路径。

    用 sqlite3 的在线备份 API 而不是复制文件——WAL 模式下直接复制会丢
    未 checkpoint 的事务。备份产物就是可独立打开的完整 SQLite 文件。

    tag 用于区分用途（如 "before-restore"），否则同一秒内的两次备份会重名，
    第二次覆盖第一次 —— 恢复场景下这等于丢掉了安全副本。
    """
    os.makedirs(DATA_DIR, exist_ok=True)
    if dest is None:
        stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
        suffix = f"-{tag}" if tag else ""
        dest = os.path.join(DATA_DIR, f"memo-backup-{stamp}{suffix}.db")
        n = 1
        while os.path.exists(dest):
            n += 1
            dest = os.path.join(DATA_DIR, f"memo-backup-{stamp}{suffix}-{n}.db")
    src = sqlite3.connect(DB_PATH)
    dst = sqlite3.connect(dest)
    try:
        src.execute("PRAGMA busy_timeout = 5000")
        with dst:
            src.backup(dst)
    finally:
        src.close()
        dst.close()
    return dest


def restore_db(src_path):
    """从备份文件恢复。先把当前库另存一份再覆盖，失败可回退。"""
    if not os.path.isfile(src_path):
        raise FileNotFoundError(f"备份文件不存在：{src_path}")
    probe = sqlite3.connect(src_path)
    try:
        # 校验确实是本程序的库，避免用户选错文件把数据冲掉
        names = {r[0] for r in probe.execute(
            "SELECT name FROM sqlite_master WHERE type='table'").fetchall()}
    finally:
        probe.close()
    if "events" not in names or "users" not in names:
        raise ValueError("该文件不是日历备忘录的数据库备份")

    safety = backup_db(tag="before-restore")
    src = sqlite3.connect(src_path)
    dst = sqlite3.connect(DB_PATH)
    try:
        with dst:
            src.backup(dst)
    finally:
        src.close()
        dst.close()
    return safety


def run_housekeeping():
    """启动时的例行清理：过期凭证图 + 过期幂等记录 + 失效会话态。

    返回各项清理数量，便于日志核对。
    """
    stats = {"uploads": 0, "idempotency": 0, "tokens": 0}
    try:
        stats["uploads"] = cleanup_uploads()
    except OSError:
        pass
    try:
        conn = sqlite3.connect(DB_PATH)
        conn.execute("PRAGMA busy_timeout = 5000")
        stats["idempotency"] = idem_cleanup(conn)
        # 90 天未使用的 Token 自动吊销，减少僵尸凭据
        cutoff = (datetime.now() - timedelta(days=90)).strftime(DT_FMT)
        cur = conn.execute(
            "UPDATE api_tokens SET revoked = 1 WHERE revoked = 0 AND last_used <> '' "
            "AND last_used < ?", (cutoff,))
        stats["tokens"] = cur.rowcount
        conn.commit()
        conn.close()
    except sqlite3.Error as exc:
        # 不能静默 pass：失败时 stats 会全 0，日志将显示「过期Token 0」，
        # 无法区分「没有过期令牌」与「清理根本没跑」。留痕供日志核对。
        stats["error"] = f"{type(exc).__name__}: {exc}"
    return stats


# ─────────── 数据备份（桌面版用；本地单机场景，需登录） ───────────

@app.post("/api/memo/backup")
def api_backup():
    """把数据库备份成文件，返回下载地址。

    本地桌面版用：用户点一下就能拿到完整数据文件，复制到 U 盘即可迁移。
    """
    u = require_login()
    if not u:
        return err("登录已失效", 401)
    try:
        path = backup_db()
    except sqlite3.Error as exc:
        return err(f"备份失败：{exc}")
    name = os.path.basename(path)
    return ok({"file": name, "size": os.path.getsize(path),
               "download": f"/api/memo/backup/{name}"}, "已生成备份")


@app.get("/api/memo/backup/<name>")
def api_backup_download(name):
    """下载备份文件。只允许 data 目录下以 memo-backup- 开头的 .db，防目录穿越。"""
    u = require_login()
    if not u:
        return err("登录已失效", 401)
    if not re.fullmatch(r"memo-backup-\d{8}-\d{6}(-[a-z-]+|-\d+)?.db", name):
        return err("备份文件名非法", 400)
    return send_from_directory(DATA_DIR, name, as_attachment=True,
                               download_name=name)


@app.post("/api/memo/restore")
def api_restore():
    """上传备份文件恢复数据。

    安全前提：恢复前自动再备份一次当前库，写错了能退回去。
    """
    u = require_login()
    if not u:
        return err("登录已失效", 401)
    f = request.files.get("file")
    if not f or not f.filename:
        return err("请选择一个 .db 备份文件")
    if not f.filename.lower().endswith(".db"):
        return err("只支持 .db 备份文件")
    tmp = os.path.join(DATA_DIR, f"restore-{secrets.token_hex(6)}.db")
    f.save(tmp)
    try:
        safety = restore_db(tmp)
    except (ValueError, FileNotFoundError, sqlite3.Error) as exc:
        return err(f"恢复失败：{exc}")
    finally:
        try:
            os.remove(tmp)
        except OSError:
            pass
    return ok({"safetyCopy": os.path.basename(safety)},
              "已恢复；恢复前的数据已另存为保护副本")


# ─────────── MCP 与服务器启动 ───────────
# 说明：MCP 工具层定义在 mcp_server.py（可 stdio 独立运行）。
# MCP 的 HTTP 传输是 ASGI 应用，无法挂进 Flask（WSGI）——
# 若要单进程跑 Web + MCP，用 serve.py；本文件只负责 Web/REST。


def run_production(host="127.0.0.1", port=8000, threads=8):
    """生产服务器：waitress 多线程，替代 Flask 开发服务器。"""
    from waitress import serve
    print(f"日历备忘录（waitress）→ http://{host}:{port}/memo_login.html")
    print(f"AI API 自描述        → http://{host}:{port}/api/ai/schema")
    print("MCP (stdio)          → python mcp_server.py")
    print(f"MCP (HTTP，可选)     → python mcp_server.py --http --port {port + 1}")
    hs = run_housekeeping()
    print(f"例行清理             → 凭证图 {hs['uploads']} 个 / 幂等记录 {hs['idempotency']} 条 / "
          f"过期 Token {hs['tokens']} 个")
    serve(app, host=host, port=port, threads=threads, channel_timeout=60)


# ─────────── 全局异常兜底 ───────────
# 原站前端要求 /api/memo/* 返回 JSON（否则提示「接口返回异常」）；
# AI 客户端更要求结构稳定。这里统一把未捕获异常转成 JSON，避免 500/404 返回 HTML。

@app.errorhandler(404)
def _handle_404(exc):
    if request.path.startswith("/api/ai"):
        return ai_err("接口不存在", 404, "not_found")
    if request.path.startswith("/api/"):
        return jsonify({"code": 404, "msg": "接口不存在"}), 404
    return exc


@app.errorhandler(405)
def _handle_405(exc):
    if request.path.startswith("/api/ai"):
        return ai_err("请求方法不被允许", 405, "method_not_allowed")
    if request.path.startswith("/api/"):
        return jsonify({"code": 405, "msg": "请求方法不被允许"}), 405
    return exc


@app.errorhandler(413)
def _handle_413(exc):
    msg = "上传内容过大（上限 8MB）"
    if request.path.startswith("/api/ai"):
        return ai_err(msg, 413, "payload_too_large")
    return jsonify({"code": 413, "msg": msg}), 413


@app.errorhandler(sqlite3.Error)
def _handle_db_error(exc):
    app.logger.exception("数据库错误: %s", exc)
    if request.path.startswith("/api/ai"):
        return ai_err("数据库暂时不可用，请稍后重试", 503, "db_unavailable")
    return jsonify({"code": 503, "msg": "数据库暂时不可用，请稍后重试"}), 503


@app.errorhandler(Exception)
def _handle_unexpected(exc):
    from werkzeug.exceptions import HTTPException
    if isinstance(exc, HTTPException):
        return exc
    app.logger.exception("未捕获异常: %s", exc)
    if request.path.startswith("/api/ai"):
        return ai_err("服务内部错误，请稍后重试", 500, "internal_error")
    if request.path.startswith("/api/"):
        return jsonify({"code": 500, "msg": "服务内部错误，请稍后重试"}), 500
    return "服务内部错误", 500


init_db()

if __name__ == "__main__":
    port = int(os.environ.get("PORT", 8000))
    host = os.environ.get("HOST", "127.0.0.1")
    if os.environ.get("MEMO_DEV") == "1":
        print(f"开发模式 → http://{host}:{port}/memo_login.html")
        app.run(host=host, port=port, debug=False)
    else:
        try:
            run_production(host, port)
        except ImportError:
            print("未安装 waitress，退回 Flask 开发服务器（pip install waitress）")
            app.run(host=host, port=port, debug=False)
