"""日历备忘录 · MCP Server

两种运行方式：
  1) stdio（推荐给 Claude Desktop / Cursor 等本地客户端）
         python mcp_server.py
  2) Streamable HTTP（远程或同机 HTTP 客户端）
         python mcp_server.py --http --port 8765

鉴权：MCP 层用 Token 定位账号（环境变量 MEMO_TOKEN，或 --token 参数）。
     Token 由主服务的 POST /api/ai/token 签发，或运行：
         python mcp_server.py --issue-token ${SEED_ADMIN_USER:-demo} ${SEED_ADMIN_PASS:-demo12345}

本进程直接读写同一个 SQLite 库，不经过 HTTP，因此与 Web 端数据实时一致。
"""

import argparse
import contextvars
import json
import os
import sys
from datetime import datetime, timedelta, date

# 复用主服务的全部业务实现，避免两套逻辑漂移
import app as memo
from mcp.server.mcpserver import MCPServer, Context

SERVER_NAME = "calendar-memo"
SERVER_VERSION = "1.1.0"

mcp = MCPServer(
    name=SERVER_NAME,
    title="日历备忘录",
    version=SERVER_VERSION,
    instructions=(
        "这是日历备忘录的 MCP 服务，直接操作用户本地的日程库。\n"
        "时间格式统一 YYYY-MM-DDTHH:MM；不确定用户意图时先调 parse_text 预览解析结果，"
        "再用 create_event 落库。\n"
        "查询现状优先用 brief_status（已算好统计，别自己重算）。"
    ),
)

# ─────────── 身份绑定 ───────────

_STATIC = {"user_id": None, "name": ""}

# 每请求上下文用 contextvars 隔离：HTTP 模式下多个客户端并发调用时，
# 全局变量会互相覆盖导致鉴权串号（实测 6/16 次串号），contextvars 天然按协程隔离。
_CTX_VAR: contextvars.ContextVar = contextvars.ContextVar("mcp_ctx", default=None)


def _set_ctx(ctx):
    """工具入口保存本次请求的 Context，供 _header_token() 取请求头。"""
    _CTX_VAR.set(ctx)


def bind_identity(token=None, username=None, password=None, conn=None):
    """把进程绑定到一个账号（stdio 模式）。返回 (ok, 说明)。"""
    own = conn is None
    conn = conn or memo.sqlite3.connect(memo.DB_PATH)
    conn.row_factory = memo.sqlite3.Row
    try:
        if token:
            row = conn.execute(
                "SELECT * FROM api_tokens WHERE token_hash=? AND revoked=0",
                (memo.hash_token(token),)).fetchone()
            if not row:
                return False, "Token 无效或已被吊销"
            user = conn.execute("SELECT * FROM users WHERE id=?", (row["user_id"],)).fetchone()
            if not user:
                return False, "Token 关联的用户不存在"
            _STATIC["user_id"] = user["id"]
            _STATIC["name"] = user["nickname"] or user["username"]
            _STATIC["writable"] = "write" in {x.strip() for x in (row["scope"] or "").split(",")}
            return True, f"已绑定账号 {_STATIC['name']}（token: {row['name']}）"
        if username and password:
            user = conn.execute("SELECT * FROM users WHERE username=?", (username,)).fetchone()
            if not user or not memo.check_password_hash(user["pwd_hash"], password):
                return False, "用户名或密码错误"
            _STATIC["user_id"] = user["id"]
            _STATIC["name"] = user["nickname"] or user["username"]
            _STATIC["writable"] = True          # 账号密码登录视为全权
            return True, f"已绑定账号 {_STATIC['name']}（账号密码）"
        return False, "未提供凭证"
    finally:
        if own:
            conn.close()


def _conn():
    conn = memo.sqlite3.connect(memo.DB_PATH)
    conn.row_factory = memo.sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    conn.execute("PRAGMA busy_timeout = 5000")
    return conn


def _header_token():
    """从当前 MCP 请求的 HTTP 头里取 Token（Streamable HTTP 模式）。

    stdio 模式拿不到 HTTP 请求，返回空串，回落到静态绑定。
    用 contextvars 而非全局变量，保证并发调用互不污染。
    """
    ctx = _CTX_VAR.get()
    if ctx is None:
        return ""
    try:
        req = getattr(ctx.request_context, "request", None)
        if req is None:
            return ""
        headers = getattr(req, "headers", None)
        if headers is None:
            scope = getattr(req, "scope", None)
            headers = scope.get("headers") if scope else None
            if headers:
                for k, v in headers:
                    kk = k.decode() if isinstance(k, bytes) else str(k)
                    if kk.lower() in ("authorization", "x-api-token"):
                        vv = v.decode() if isinstance(v, bytes) else str(v)
                        return vv[7:].strip() if vv.lower().startswith("bearer ") else vv.strip()
                return ""
        auth = headers.get("authorization") or headers.get("Authorization") or ""
        if auth.lower().startswith("bearer "):
            return auth[7:].strip()
        return (headers.get("x-api-token") or "").strip()
    except Exception:
        return ""


def _uid(need_write=False):
    """当前调用者 id：优先请求头 Token（HTTP），否则静态绑定（stdio）。

    need_write=True 时校验 Token scope，只读 Token 拒绝写操作。
    """
    tok = _header_token()
    if tok:
        conn = _conn()
        try:
            row = conn.execute(
                "SELECT user_id, scope FROM api_tokens WHERE token_hash=? AND revoked=0",
                (memo.hash_token(tok),)).fetchone()
            if not row:
                raise RuntimeError("请求头中的 Token 无效或已吊销")
            if need_write:
                parts = {x.strip() for x in (row["scope"] or "").split(",")}
                if "write" not in parts:
                    raise RuntimeError("该 Token 为只读权限，不能执行写操作")
            return row["user_id"]
        finally:
            conn.close()
    if not _STATIC["user_id"]:
        raise RuntimeError(
            "MCP 未认证：stdio 模式请设置 MEMO_TOKEN 或传 --token；"
            "HTTP 模式请在请求头携带 Authorization: Bearer <token>")
    if need_write and not _STATIC.get("writable", True):
        raise RuntimeError("该 Token 为只读权限，不能执行写操作")
    return _STATIC["user_id"]


def _user(conn):
    return conn.execute("SELECT * FROM users WHERE id=?", (_uid(),)).fetchone()


def _fail(msg):
    return {"ok": False, "error": msg}


def _event_out(conn, row):
    it = memo.event_item(conn, row)
    now = datetime.now()
    end = memo.parse_dt(row["end_time"])
    done = row["status"] == "done"
    if done:
        it["state"], it["minutesLeft"] = "done", None
    elif end and end < now:
        it["state"] = "overdue"
        it["minutesLeft"] = -int((now - end).total_seconds() // 60)
    else:
        it["state"] = "doing"
        it["minutesLeft"] = int((end - now).total_seconds() // 60) if end else None
    return it


def _resolve_or_create_tag(conn, name):
    name = str(name).strip()[:30]
    if not name:
        return None
    row = conn.execute("SELECT id FROM tags WHERE user_id=? AND name=?", (_uid(), name)).fetchone()
    if row:
        return row["id"]
    return memo.ai_ensure_tag(conn, _uid(), name)


def _insert_event(conn, *, title, start, end, level="middle", repeat="",
                  tags=None, content="", remark="", subtasks=None):
    now = memo.now_str()
    cur = conn.execute(
        "INSERT INTO events(user_id, title, content, start_time, end_time, remark, level, status,"
        " repeat_rule, created_at, updated_at) VALUES (?,?,?,?,?,?,?,?,?,?,?)",
        (_uid(), title, content, memo.fmt_dt(start), memo.fmt_dt(end), remark, level,
         "doing", repeat, now, now),
    )
    eid = cur.lastrowid
    for t in (tags or [])[:10]:
        tid = _resolve_or_create_tag(conn, t)
        if tid:
            conn.execute("INSERT OR IGNORE INTO event_tag(event_id, tag_id) VALUES (?,?)", (eid, tid))
    for i, s in enumerate(subtasks or []):
        st = (s.get("title") if isinstance(s, dict) else str(s) or "").strip()
        if st:
            conn.execute("INSERT INTO subtasks(event_id, title, done, sort) VALUES (?,?,?,?)",
                         (eid, st, 0, i))
    conn.commit()
    return eid


# ─────────── 工具：写 ───────────

@mcp.tool(name="create_event",
          description="新建一条日程。传 text 可用中文口语（如「明天下午3点开会 2小时 #项目A 紧急」），"
                      "也可直接传 title + start + end。返回创建的日程详情。")
def create_event(text: str = "", title: str = "", start: str = "", end: str = "",
                 duration_minutes: int = 0, level: str = "middle", repeat: str = "",
                 tags: str = "", content: str = "", remark: str = "",
                 subtasks: str = "", ctx: Context = None) -> dict:
    _set_ctx(ctx)                  # 先绑定请求上下文，才能取到请求头 Token
    try:
        _uid(need_write=True)      # 只读 Token 在此被拒
    except RuntimeError as exc:
        return {"ok": False, "error": str(exc)}
    conn = _conn()
    try:
        tag_list = [t.strip() for t in tags.replace("#", ",").split(",") if t.strip()]
        sub_list = [s.strip() for s in subtasks.split(",") if s.strip()]
        if text and not title:
            p = memo.parse_natural(text)
            if not p["start"]:
                return {"ok": False,
                        "error": "句子中没有可确定的时间，请改用 start 参数（YYYY-MM-DDTHH:MM）",
                        "parsed": {"title": p["title"], "level": p["level"],
                                   "repeat": p["repeat"], "tags": p["tags"],
                                   "matched": p["matched"]}}
            title = p["title"]
            start, end = memo._dt_out(p["start"]), memo._dt_out(p["end"])
            level = p["level"]
            repeat = p["repeat"]
            tag_list = list(dict.fromkeys(tag_list + p["tags"]))
        if not title:
            return _fail("title 不能为空（或提供 text 让服务解析）")
        if level and level not in ("high", "middle", "low"):
            return _fail("level 只能是 high / middle / low")
        if repeat:
            _bad = memo.validate_rule(repeat)
            if _bad:
                return _fail(_bad)
        sd = memo.parse_dt(start) or memo.parse_dt(end)
        ed = memo.parse_dt(end)
        if ed is None and sd is not None:
            ed = sd + timedelta(minutes=duration_minutes) if duration_minutes else sd + timedelta(hours=1)
        if sd is None or ed is None:
            return _fail("需要 start（或 end），格式 YYYY-MM-DDTHH:MM")
        if ed < sd:
            return _fail("end 不能早于 start")
        eid = _insert_event(conn, title=title, start=sd, end=ed, level=level, repeat=repeat,
                            tags=tag_list, content=content, remark=remark, subtasks=sub_list)
        row = conn.execute("SELECT * FROM events WHERE id=?", (eid,)).fetchone()
        return {"ok": True, "event": _event_out(conn, row)}
    finally:
        conn.close()


@mcp.tool(name="create_events_batch",
          description="批量新建日程。items 为 JSON 数组，每项可含 text 或 title/start/end。单次最多 50 条。")
def create_events_batch(items: str, ctx: Context = None) -> dict:
    _set_ctx(ctx)                  # 先绑定请求上下文，才能取到请求头 Token
    try:
        _uid(need_write=True)      # 只读 Token 在此被拒
    except RuntimeError as exc:
        return {"ok": False, "error": str(exc)}
    try:
        arr = json.loads(items) if isinstance(items, str) else items
    except json.JSONDecodeError as exc:
        return _fail(f"items 必须是合法 JSON 数组：{exc}")
    if not isinstance(arr, list) or not arr:
        return _fail("items 必须是非空数组")
    if len(arr) > 50:
        return _fail("单次最多 50 条")
    conn = _conn()
    try:
        results = []
        for idx, it in enumerate(arr):
            if not isinstance(it, dict):
                it = {"text": str(it)}
            text = (it.get("text") or "").strip()
            if text and not it.get("title"):
                p = memo.parse_natural(text)
                if not p["start"]:
                    results.append({"index": idx, "ok": False, "error": "无法确定时间"})
                    continue
                t, s, en = p["title"], memo._dt_out(p["start"]), memo._dt_out(p["end"])
                lv, rp, tg = p["level"], p["repeat"], p["tags"]
            else:
                t = (it.get("title") or "").strip()
                s, en = it.get("start") or "", it.get("end") or ""
                lv, rp = it.get("level") or "middle", it.get("repeat") or ""
                tg = it.get("tags") or []
            sd = memo.parse_dt(s) or memo.parse_dt(en)
            ed = memo.parse_dt(en)
            if ed is None and sd is not None:
                d = it.get("durationMinutes") or it.get("duration_minutes")
                ed = sd + timedelta(minutes=int(d)) if d else sd + timedelta(hours=1)
            if not t or sd is None or ed is None:
                results.append({"index": idx, "ok": False, "error": "title 与时间不完整"})
                continue
            if ed < sd:
                results.append({"index": idx, "ok": False, "error": "end 早于 start"})
                continue
            eid = _insert_event(conn, title=t, start=sd, end=ed, level=lv, repeat=rp,
                                tags=tg, content=it.get("content") or "")
            results.append({"index": idx, "ok": True, "id": eid, "title": t,
                            "start": memo.fmt_dt(sd), "end": memo.fmt_dt(ed)})
        return {"ok": True, "succeeded": sum(1 for r in results if r["ok"]),
                "failed": sum(1 for r in results if not r["ok"]), "results": results}
    finally:
        conn.close()


@mcp.tool(name="update_event",
          description="局部更新日程。只传要改的字段即可，如只改 status='done' 或 level='high'。")
def update_event(event_id: int, title: str = "", start: str = "", end: str = "",
                 level: str = "", status: str = "", content: str = "", remark: str = "",
                 tags: str = "", archived: bool = None, actual_minutes: int = -1, ctx: Context = None) -> dict:
    _set_ctx(ctx)                  # 先绑定请求上下文，才能取到请求头 Token
    try:
        _uid(need_write=True)      # 只读 Token 在此被拒
    except RuntimeError as exc:
        return {"ok": False, "error": str(exc)}
    conn = _conn()
    try:
        row = conn.execute("SELECT * FROM events WHERE id=? AND user_id=?",
                           (event_id, _uid())).fetchone()
        if not row:
            return _fail(f"日程 {event_id} 不存在")
        if level and level not in ("high", "middle", "low"):
            return _fail("level 只能是 high / middle / low")
        if status and status not in ("doing", "done"):
            return _fail("status 只能是 doing / done")
        fields, params = [], []
        for v, col in ((title, "title"), (content, "content"), (remark, "remark"),
                       (level, "level"), (status, "status")):
            if v:
                fields.append(f"{col}=?")
                params.append(v)
        for v, col in ((start, "start_time"), (end, "end_time")):
            if v:
                dt = memo.parse_dt(v)
                if not dt:
                    return _fail(f"{col} 格式应为 YYYY-MM-DDTHH:MM")
                fields.append(f"{col}=?")
                params.append(memo.fmt_dt(dt))
        if archived is not None:
            fields.append("archived=?")
            params.append(1 if archived else 0)
        if actual_minutes >= 0:
            fields.append("actual_minutes=?")
            params.append(min(100000, actual_minutes))
        if not fields:
            return _fail("没有传任何需要更新的字段")
        sd = memo.parse_dt(start) or memo.parse_dt(row["start_time"])
        ed = memo.parse_dt(end) or memo.parse_dt(row["end_time"])
        if sd and ed and ed < sd:
            return _fail("end 不能早于 start")
        fields.append("updated_at=?")
        params.append(memo.now_str())
        conn.execute(f"UPDATE events SET {','.join(fields)} WHERE id=?", params + [event_id])
        if tags:
            conn.execute("DELETE FROM event_tag WHERE event_id=?", (event_id,))
            for t in [x.strip() for x in tags.replace("#", ",").split(",") if x.strip()][:10]:
                tid = _resolve_or_create_tag(conn, t)
                if tid:
                    conn.execute("INSERT OR IGNORE INTO event_tag(event_id, tag_id) VALUES (?,?)",
                                 (event_id, tid))
        conn.commit()
        fresh = conn.execute("SELECT * FROM events WHERE id=?", (event_id,)).fetchone()
        spawned = None
        if status == "done" and fresh["repeat_rule"]:
            spawned = memo.spawn_next_occurrence(conn, _uid(), fresh)
        return {"ok": True, "event": _event_out(conn, fresh), "spawnedId": spawned}
    finally:
        conn.close()


@mcp.tool(name="complete_event",
          description="把日程标记为已完成（或传 done=false 恢复为进行中）。重复日程会自动生成下一条。")
def complete_event(event_id: int, done: bool = True, ctx: Context = None) -> dict:
    _set_ctx(ctx)                  # 先绑定请求上下文，才能取到请求头 Token
    try:
        _uid(need_write=True)      # 只读 Token 在此被拒
    except RuntimeError as exc:
        return {"ok": False, "error": str(exc)}
    conn = _conn()
    try:
        row = conn.execute("SELECT * FROM events WHERE id=? AND user_id=?",
                           (event_id, _uid())).fetchone()
        if not row:
            return _fail(f"日程 {event_id} 不存在")
        target = "done" if done else "doing"
        conn.execute("UPDATE events SET status=?, updated_at=? WHERE id=?",
                     (target, memo.now_str(), event_id))
        conn.commit()
        fresh = conn.execute("SELECT * FROM events WHERE id=?", (event_id,)).fetchone()
        spawned = memo.spawn_next_occurrence(conn, _uid(), fresh) if done else None
        return {"ok": True, "event": _event_out(conn, fresh), "spawnedId": spawned}
    finally:
        conn.close()


@mcp.tool(name="delete_event", description="删除日程（不可恢复）。删除前建议先 get_event 确认。")
def delete_event(event_id: int, ctx: Context = None) -> dict:
    _set_ctx(ctx)                  # 先绑定请求上下文，才能取到请求头 Token
    try:
        _uid(need_write=True)      # 只读 Token 在此被拒
    except RuntimeError as exc:
        return {"ok": False, "error": str(exc)}
    conn = _conn()
    try:
        row = conn.execute("SELECT id, title, start_time FROM events WHERE id=? AND user_id=?",
                           (event_id, _uid())).fetchone()
        if not row:
            return _fail(f"日程 {event_id} 不存在")
        conn.execute("DELETE FROM events WHERE id=?", (event_id,))
        conn.execute("DELETE FROM event_tag WHERE event_id=?", (event_id,))
        conn.execute("DELETE FROM subtasks WHERE event_id=?", (event_id,))
        conn.commit()
        return {"ok": True, "deleted": {"id": row["id"], "title": row["title"],
                                        "start": memo.fmt_dt(row["start_time"])}}
    finally:
        conn.close()


@mcp.tool(name="toggle_subtask",
          description="勾选/取消勾选某条子任务，返回该日程的子任务总进度。")
def toggle_subtask(subtask_id: int, done: bool = None, ctx: Context = None) -> dict:
    _set_ctx(ctx)                  # 先绑定请求上下文，才能取到请求头 Token
    try:
        _uid(need_write=True)      # 只读 Token 在此被拒
    except RuntimeError as exc:
        return {"ok": False, "error": str(exc)}
    conn = _conn()
    try:
        row = conn.execute(
            "SELECT s.* FROM subtasks s JOIN events e ON e.id=s.event_id "
            "WHERE s.id=? AND e.user_id=?", (subtask_id, _uid())).fetchone()
        if not row:
            return _fail(f"子任务 {subtask_id} 不存在")
        target = (not bool(row["done"])) if done is None else bool(done)
        conn.execute("UPDATE subtasks SET done=? WHERE id=?", (1 if target else 0, subtask_id))
        conn.commit()
        total = conn.execute("SELECT COUNT(*) c FROM subtasks WHERE event_id=?",
                             (row["event_id"],)).fetchone()["c"]
        fin = conn.execute("SELECT COUNT(*) c FROM subtasks WHERE event_id=? AND done=1",
                           (row["event_id"],)).fetchone()["c"]
        return {"ok": True, "subtaskId": subtask_id, "done": target, "eventId": row["event_id"],
                "progress": {"done": fin, "total": total,
                             "percent": round(fin * 100 / total) if total else 0}}
    finally:
        conn.close()


# ─────────── 工具：读 ───────────

@mcp.tool(name="brief_status",
          description="查看当前整体状况：总数/进行中/已完成/超时/今天数量、最近截止、超时清单、会员与积分。"
                      "用户问「我现在有什么安排/忙不忙」时优先调这个。")
def brief_status(ctx: Context = None) -> dict:
    _set_ctx(ctx)
    conn = _conn()
    try:
        u = _user(conn)
        rows = conn.execute("SELECT * FROM events WHERE user_id=?", (_uid(),)).fetchall()
        now = datetime.now()
        live = [r for r in rows if not r["archived"]]
        done = [r for r in live if r["status"] == "done"]
        doing = [r for r in live if r["status"] != "done"]
        overdue = [r for r in doing if (memo.parse_dt(r["end_time"]) or now) < now]
        today_l = [r for r in live if str(r["start_time"])[:10] == memo.today_str()]
        soon = sorted([r for r in doing if (memo.parse_dt(r["end_time"]) or now) >= now],
                      key=lambda r: memo.parse_dt(r["end_time"]))[:5]
        is_member, expire, left = memo.member_info(u)
        parts = [f"共 {len(live)} 条日程：进行中 {len(doing)}、已完成 {len(done)}、"
                 f"已超时 {len(overdue)}", f"今天 {len(today_l)} 条"]
        if soon:
            nxt = soon[0]
            mins = int((memo.parse_dt(nxt["end_time"]) - now).total_seconds() // 60)
            parts.append(f"最近截止「{nxt['title']}」剩 {mins} 分钟")
        return {"ok": True, "summary": "；".join(parts) + "。",
                "counts": {"total": len(live), "doing": len(doing), "done": len(done),
                           "overdue": len(overdue), "archived": len(rows) - len(live),
                           "today": len(today_l)},
                "upcoming": [{"id": r["id"], "title": r["title"],
                              "end": memo.fmt_dt(r["end_time"]),
                              "minutesLeft": int((memo.parse_dt(r["end_time"]) - now).total_seconds() // 60)}
                             for r in soon],
                "overdue": [{"id": r["id"], "title": r["title"],
                             "end": memo.fmt_dt(r["end_time"])} for r in overdue[:10]],
                "member": {"isMember": is_member, "expireAt": expire,
                           "daysLeft": left, "points": int(u["points"] or 0)}}
    finally:
        conn.close()


@mcp.tool(name="list_events",
          description="查询日程列表。可按状态/级别/关键词/日期范围/标签过滤，按剩余时间正序返回。")
def list_events(status: str = "", level: str = "", q: str = "", date_from: str = "",
                date_to: str = "", tag: str = "", archived: bool = False,
                page: int = 1, limit: int = 20, ctx: Context = None) -> dict:
    _set_ctx(ctx)
    conn = _conn()
    try:
        sql = "SELECT * FROM events WHERE user_id=?"
        params = [_uid()]
        sql += " AND archived=?"
        params.append(1 if archived else 0)
        if status in ("doing", "done"):
            sql += " AND status=?"
            params.append(status)
        if level in ("high", "middle", "low"):
            sql += " AND level=?"
            params.append(level)
        if date_from:
            sql += " AND substr(start_time,1,10)>=?"
            params.append(date_from)
        if date_to:
            sql += " AND substr(start_time,1,10)<=?"
            params.append(date_to)
        if q:
            sql += " AND (title LIKE ? OR content LIKE ? OR remark LIKE ?)"
            params += [f"%{q}%"] * 3
        if tag:
            sql += (" AND id IN (SELECT et.event_id FROM event_tag et JOIN tags t ON t.id=et.tag_id"
                    " WHERE t.user_id=? AND t.name=?)")
            params += [_uid(), tag]
        rows = memo.sort_events(conn.execute(sql, params).fetchall())
        total = len(rows)
        page = max(1, page)
        limit = max(1, min(100, limit))
        chunk = rows[(page - 1) * limit:(page - 1) * limit + limit]
        return {"ok": True, "total": total, "page": page, "limit": limit,
                "items": [_event_out(conn, r) for r in chunk]}
    finally:
        conn.close()


@mcp.tool(name="get_event", description="按 id 查看单条日程的完整详情（含子任务、标签、进度）。")
def get_event(event_id: int, ctx: Context = None) -> dict:
    _set_ctx(ctx)
    conn = _conn()
    try:
        row = conn.execute("SELECT * FROM events WHERE id=? AND user_id=?",
                           (event_id, _uid())).fetchone()
        if not row:
            return _fail(f"日程 {event_id} 不存在")
        return {"ok": True, "event": _event_out(conn, row)}
    finally:
        conn.close()


@mcp.tool(name="get_agenda",
          description="按天获取议程。date 默认今天，days 可看未来多天（1~31）。")
def get_agenda(date_str: str = "", days: int = 1, ctx: Context = None) -> dict:
    _set_ctx(ctx)
    conn = _conn()
    try:
        base = memo.parse_dt(date_str) if date_str else datetime.now()
        if not base:
            return _fail("date 格式应为 YYYY-MM-DD")
        days = max(1, min(31, days))
        out = []
        for i in range(days):
            d = (base.date() + timedelta(days=i)).strftime(memo.DATE_FMT)
            rows = memo.sort_events(conn.execute(
                "SELECT * FROM events WHERE user_id=? AND archived=0 AND substr(start_time,1,10)=?",
                (_uid(), d)).fetchall())
            out.append({"date": d,
                        "weekday": "一二三四五六日"[date.fromisoformat(d).weekday()],
                        "count": len(rows),
                        "done": sum(1 for r in rows if r["status"] == "done"),
                        "items": [_event_out(conn, r) for r in rows]})
        return {"ok": True, "days": out}
    finally:
        conn.close()


@mcp.tool(name="parse_text",
          description="只解析一句中文口语，不写库。用于预览「明天下午3点开会2小时」会被理解成什么。")
def parse_text(text: str, now: str = "", ctx: Context = None) -> dict:
    _set_ctx(ctx)
    base = memo.parse_dt(now) if now else datetime.now()
    p = memo.parse_natural(text, base)
    return {"ok": True, "understood": bool(p["start"]),
            "title": p["title"], "start": memo._dt_out(p["start"]),
            "end": memo._dt_out(p["end"]), "level": p["level"],
            "repeat": p["repeat"], "tags": p["tags"], "matched": p["matched"]}


@mcp.tool(name="list_tags", description="列出所有标签及其被引用的日程数量。")
def list_tags(ctx: Context = None) -> dict:
    _set_ctx(ctx)
    conn = _conn()
    try:
        rows = conn.execute(
            "SELECT t.id, t.name, t.color, COUNT(et.event_id) usage "
            "FROM tags t LEFT JOIN event_tag et ON et.tag_id=t.id "
            "LEFT JOIN events e ON e.id=et.event_id AND e.archived=0 "
            "WHERE t.user_id=? GROUP BY t.id ORDER BY t.id", (_uid(),)).fetchall()
        return {"ok": True, "tags": [dict(r) for r in rows]}
    finally:
        conn.close()


@mcp.tool(name="create_tag", description="新建标签（免费版最多 5 个）。已存在则直接返回原标签。")
def create_tag(name: str, ctx: Context = None) -> dict:
    _set_ctx(ctx)                  # 先绑定请求上下文，才能取到请求头 Token
    try:
        _uid(need_write=True)      # 只读 Token 在此被拒
    except RuntimeError as exc:
        return {"ok": False, "error": str(exc)}
    conn = _conn()
    try:
        u = _user(conn)
        exist = conn.execute("SELECT id, name, color FROM tags WHERE user_id=? AND name=?",
                             (_uid(), name.strip()[:30])).fetchone()
        if exist:
            return {"ok": True, "tag": dict(exist), "existed": True}
        is_member, _, _ = memo.member_info(u)
        cnt = conn.execute("SELECT COUNT(*) c FROM tags WHERE user_id=?", (_uid(),)).fetchone()["c"]
        if not is_member and cnt >= memo.FREE_TAG_LIMIT:
            return _fail(f"免费版最多 {memo.FREE_TAG_LIMIT} 个标签，开通会员可无限")
        tid = memo.ai_ensure_tag(conn, _uid(), name)
        conn.commit()
        row = conn.execute("SELECT id, name, color FROM tags WHERE id=?", (tid,)).fetchone()
        return {"ok": True, "tag": dict(row), "existed": False}
    finally:
        conn.close()


@mcp.tool(name="get_calendar_month",
          description="按月返回打点数据：哪些日期有日程、各几条。")
def get_calendar_month(year: int = 0, month: int = 0, ctx: Context = None) -> dict:
    _set_ctx(ctx)
    today = date.today()
    year = year or today.year
    month = month or today.month
    if not 1 <= month <= 12:
        return _fail("month 需在 1~12")
    conn = _conn()
    try:
        rows = conn.execute(
            "SELECT * FROM events WHERE user_id=? AND archived=0 AND substr(start_time,1,7)=?",
            (_uid(), f"{year:04d}-{month:02d}")).fetchall()
        marks = {}
        for r in rows:
            d = str(r["start_time"])[:10]
            marks.setdefault(d, []).append({"id": r["id"], "title": r["title"],
                                            "level": r["level"], "status": r["status"]})
        return {"ok": True, "year": year, "month": month, "daysWithEvents": len(marks),
                "days": [{"date": k, "count": len(v), "items": v} for k, v in sorted(marks.items())]}
    finally:
        conn.close()


@mcp.tool(name="whoami", description="查看 MCP 当前绑定的账号与权限。")
def whoami(ctx: Context = None) -> dict:
    _set_ctx(ctx)
    try:
        uid = _uid()
    except RuntimeError as exc:
        return {"ok": False, "error": str(exc)}
    conn = _conn()
    try:
        u = _user(conn)
        is_member, expire, left = memo.member_info(u)
        return {"ok": True, "userId": u["id"],
                "username": u["username"], "nickname": u["nickname"],
                "isMember": is_member, "memberExpireAt": expire, "daysLeft": left,
                "points": int(u["points"] or 0),
                "server": {"name": SERVER_NAME, "version": SERVER_VERSION}}
    finally:
        conn.close()


# ─────────── 启动 ───────────

def main():
    ap = argparse.ArgumentParser(description="日历备忘录 MCP Server")
    ap.add_argument("--http", action="store_true", help="用 Streamable HTTP 而不是 stdio")
    ap.add_argument("--port", type=int, default=8765)
    ap.add_argument("--host", default="127.0.0.1")
    ap.add_argument("--token", default=os.environ.get("MEMO_TOKEN", ""))
    ap.add_argument("--user", default=os.environ.get("MEMO_USER", ""))
    ap.add_argument("--password", default=os.environ.get("MEMO_PASSWORD", ""))
    ap.add_argument("--issue-token", nargs=2, metavar=("USERNAME", "PASSWORD"),
                    help="直接用账号密码签发一个 Token 后退出")
    args = ap.parse_args()

    if args.issue_token:
        ok, msg = bind_identity(username=args.issue_token[0], password=args.issue_token[1])
        if not ok:
            print(f"失败：{msg}", file=sys.stderr)
            sys.exit(1)
        conn = _conn()
        raw = "memo_" + memo.secrets.token_urlsafe(32)
        cur = conn.execute(
            "INSERT INTO api_tokens(user_id, name, token_hash, prefix, scope, created_at) "
            "VALUES (?,?,?,?,?,?)",
            (_uid(), "mcp-cli", memo.hash_token(raw), raw[:12], "read,write", memo.now_str()))
        conn.commit()
        conn.close()
        print(f"已为「{_STATIC['name']}」签发 Token（id={cur.lastrowid}）：\n{raw}")
        return

    ok, msg = bind_identity(token=args.token, username=args.user, password=args.password)
    if ok:
        print(f"[mcp] {msg}", file=sys.stderr)
    else:
        print(f"[mcp] 警告：{msg}；工具调用会返回未绑定错误。\n"
              f"[mcp] 提示：先运行 python mcp_server.py --issue-token <用户名> <密码>",
              file=sys.stderr)

    if args.http:
        print(f"[mcp] Streamable HTTP 监听 http://{args.host}:{args.port}/mcp", file=sys.stderr)
        mcp.run(transport="streamable-http", host=args.host, port=args.port)
    else:
        mcp.run(transport="stdio")


if __name__ == "__main__":
    main()
