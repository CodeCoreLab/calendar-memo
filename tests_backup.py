"""备份与恢复回归测试。

用法：.venv/Scripts/python.exe tests_backup.py

设计要点
--------
- 全程临时数据目录，不触碰 data/memo.db。
- 恢复是**破坏性操作**（会覆盖整个库），所以每个用例都从一份干净副本起步。
- 断言重点在「失败时不能破坏数据」，不只是「成功时能还原」。

覆盖
- 备份生成 / 文件名格式 / 下载内容有效性
- 恢复：正确还原、保护副本、非 SQLite 拒绝、错误扩展名拒绝、表结构不符拒绝
- 恢复失败后原库完好（最关键的一条）
- 未登录不可备份/恢复
"""

import io
import json
import os
import shutil
import sqlite3
import sys
import tempfile
import uuid

HERE = os.path.dirname(os.path.abspath(__file__))
TMP = tempfile.mkdtemp(prefix="memo_bak_")
os.environ["MEMO_DATA_DIR"] = TMP
sys.path.insert(0, HERE)

import app as memo   # noqa: E402

RESULTS = []


def check(name, cond, detail=""):
    RESULTS.append((name, bool(cond), detail))
    print(f"{'PASS' if cond else 'FAIL'}  {name}" + (f"  [{detail}]" if detail else ""))
    return bool(cond)


def new_client():
    return memo.app.test_client()


def post(c, path, payload=None):
    r = c.post(path, json=payload if payload is not None else {})
    try:
        return r.status_code, (r.get_json() or {})
    except Exception:
        return r.status_code, {}


def multipart(c, path, filename, content, ctype="application/octet-stream"):
    """构造 multipart/form-data 上传（flask test_client 的 data 方式）。"""
    return c.post(path, data={"file": (io.BytesIO(content), filename, ctype)},
                  content_type="multipart/form-data")


def db_count_events():
    conn = sqlite3.connect(memo.DB_PATH)
    n = conn.execute("SELECT COUNT(*) FROM events").fetchone()[0]
    conn.close()
    return n


def reset_db(seed=True):
    """重建一份干净的库（恢复类用例每次都从头开始）。"""
    for f in ("memo.db", "memo.db-wal", "memo.db-shm"):
        p = os.path.join(TMP, f)
        if os.path.exists(p):
            os.remove(p)
    conn = sqlite3.connect(memo.DB_PATH)
    conn.executescript(memo.SCHEMA)
    from werkzeug.security import generate_password_hash
    conn.execute(
        "INSERT INTO users(username,nickname,email,pwd_hash,member_expire,points,created_at)"
        " VALUES (?,?,?,?,?,?,?)",
        ("bak_user", "备", "bak@x.local", generate_password_hash("BakPass12345"),
         "", 0, memo.now_str()))
    uid = conn.execute("SELECT id FROM users WHERE username='bak_user'").fetchone()[0]
    conn.execute("INSERT INTO user_setting(user_id) VALUES (?)", (uid,))
    if seed:
        for i in range(3):
            conn.execute(
                "INSERT INTO events(user_id,title,content,start_time,end_time,remark,level,"
                "status,repeat_rule,repeat_until,actual_minutes,archived,created_at,updated_at)"
                " VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (uid, f"初始日程{i+1}", "", f"2026-12-0{i+1} 09:00",
                 f"2026-12-0{i+1} 10:00", "", "middle", "doing", "", "", 0, 0,
                 memo.now_str(), memo.now_str()))
    conn.commit()
    conn.close()
    return uid


def main():
    print("=" * 70)
    print("备份与恢复回归测试")
    print("=" * 70)
    print(f"临时数据目录: {TMP}\n")

    # ─────────── 一、备份生成与下载 ───────────
    print("── 一、备份生成与下载 ──")
    uid = reset_db()
    c, d = new_client(), None
    _, d = post(c, "/api/memo/login", {"username": "bak_user", "password": "BakPass12345"})
    check("1.1 登录成功", d.get("code") == 0, d.get("msg", ""))

    _, d = post(c, "/api/memo/backup", {})
    name = (d.get("data") or {}).get("file")
    size = (d.get("data") or {}).get("size")
    check("1.2 备份生成成功", d.get("code") == 0 and bool(name), str(name))
    check("1.3 文件名符合白名单格式",
          bool(name) and __import__("re").fullmatch(
              r"memo-backup-\d{8}-\d{6}(-[a-z-]+|-\d+)?.db", name), str(name))

    if name:
        r = c.get(f"/api/memo/backup/{name}")
        blob = r.data
        check("1.4 备份可下载且大小一致", r.status_code == 200 and len(blob) == size,
              f"{r.status_code} / {len(blob)} vs {size}")

        # 下载内容是有效 SQLite，且表与数据齐全
        tmpf = os.path.join(TMP, "_verify.db")
        open(tmpf, "wb").write(blob)
        conn = sqlite3.connect(tmpf)
        tables = {r[0] for r in conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table'")}
        ev = conn.execute("SELECT COUNT(*) FROM events").fetchone()[0]
        conn.close()
        os.remove(tmpf)
        check("1.5 下载文件是有效 SQLite（22 表）", len(tables) >= 20, f"{len(tables)} 表")
        check("1.6 备份内含 3 条日程", ev == 3, str(ev))

    # ─────────── 二、同一秒两次备份不重名 ───────────
    print("\n── 二、备份命名去重 ──")
    _, d1 = post(c, "/api/memo/backup", {})
    _, d2 = post(c, "/api/memo/backup", {})
    f1, f2 = (d1.get("data") or {}).get("file"), (d2.get("data") or {}).get("file")
    check("2.1 同一秒两次备份不重名", bool(f1) and bool(f2) and f1 != f2, f"{f1} / {f2}")

    # ─────────── 三、恢复：正常路径 ───────────
    print("\n── 三、恢复正确还原 ──")
    reset_db()
    c2 = new_client()
    post(c2, "/api/memo/login", {"username": "bak_user", "password": "BakPass12345"})
    before = db_count_events()

    # 取一份备份
    _, d = post(c2, "/api/memo/backup", {})
    bname = d["data"]["file"]
    blob = c2.get(f"/api/memo/backup/{bname}").data

    # 故意多建一条，再用备份还原 → 应回到 before
    new_client()
    post(c2, "/api/memo/event", {"title": "应被还原掉的日程",
                                 "startTime": "2026-12-20T10:00",
                                 "endTime": "2026-12-20T11:00",
                                 "level": "middle", "status": "doing"})
    after_add = db_count_events()
    check("3.1 新增后条数 +1", after_add == before + 1, f"{before} → {after_add}")

    r = multipart(c2, "/api/memo/restore", bname, blob)
    d = r.get_json() or {}
    check("3.2 恢复请求成功", d.get("code") == 0, d.get("msg", ""))
    safety = (d.get("data") or {}).get("safetyCopy")
    check("3.3 生成保护副本", bool(safety) and "before-restore" in safety, str(safety))

    restored = db_count_events()
    check("3.4 数据还原到备份时刻", restored == before, f"{after_add} → {restored}，期望 {before}")

    # 保护副本可下载
    if safety:
        r = c2.get(f"/api/memo/backup/{safety}")
        check("3.5 保护副本可下载", r.status_code == 200 and len(r.data) > 1024,
              f"{r.status_code} {len(r.data)}B")

    # ─────────── 四、恢复：拒绝非法输入（且不能破坏原库） ───────────
    print("\n── 四、非法输入被拒且原库完好 ──")
    reset_db()
    c3 = new_client()
    post(c3, "/api/memo/login", {"username": "bak_user", "password": "BakPass12345"})
    baseline = db_count_events()

    cases = [
        ("4.1 非 SQLite 内容被拒", "fake.db", b"this is not a database", "application/octet-stream"),
        ("4.2 错误扩展名被拒", "evil.txt", b"abc", "text/plain"),
        ("4.3 空文件被拒", "empty.db", b"", "application/octet-stream"),
    ]
    for label, fname, content, ctype in cases:
        r = multipart(c3, "/api/memo/restore", fname, content, ctype)
        d = r.get_json() or {}
        check(label, d.get("code") != 0, d.get("msg", ""))

    # 关键：三次失败尝试后，原库必须还是好的
    still = db_count_events()
    check("4.4 三次失败后原库完好", still == baseline, f"{baseline} → {still}")
    r = c3.get("/api/memo/summary")
    d = r.get_json() or {}
    check("4.5 失败后接口仍正常", d.get("code") == 0, str(d.get("data")))

    # 4.6 结构不符（有效 SQLite 但不是本程序的库）
    tmpf = os.path.join(TMP, "other.db")
    conn = sqlite3.connect(tmpf)
    conn.execute("CREATE TABLE whatever(id INTEGER)")
    conn.commit()
    conn.close()
    other_blob = open(tmpf, "rb").read()
    os.remove(tmpf)
    r = multipart(c3, "/api/memo/restore", "other.db", other_blob)
    d = r.get_json() or {}
    check("4.6 表结构不符被拒", d.get("code") != 0, d.get("msg", ""))
    check("4.7 之后原库仍完好", db_count_events() == baseline)

    # ─────────── 五、路径穿越 ───────────
    print("\n── 五、备份下载白名单 ──")
    for bad in ["../../../etc/passwd", "..%2F..%2Fetc%2Fpasswd", "memo.db", "run.log",
                "memo-backup-x.db"]:
        r = c3.get(f"/api/memo/backup/{bad}")
        d = r.get_json() or {}
        blocked = (r.status_code >= 400) or (d.get("code") not in (0, None))
        check(f"5.x 拒绝非法文件名 {bad[:24]}", blocked,
              f"HTTP {r.status_code} code={d.get('code')}")

    # ─────────── 六、未登录不可备份/恢复 ───────────
    print("\n── 六、未登录被拒 ──")
    anon = new_client()
    _, d = post(anon, "/api/memo/backup", {})
    check("6.1 未登录不可备份", d.get("code") == 401, f"code={d.get('code')}")

    r = multipart(anon, "/api/memo/restore", "x.db", other_blob)
    d = r.get_json() or {}
    check("6.2 未登录不可恢复", d.get("code") == 401, f"code={d.get('code')}")

    r = anon.get("/api/memo/backup/memo-backup-20260101-000000.db")
    d = r.get_json() or {}
    check("6.3 未登录不可下载备份", d.get("code") == 401 or r.status_code in (401, 403),
          f"HTTP {r.status_code} code={d.get('code')}")

    # ─────────── 汇总 ───────────
    total = len(RESULTS)
    passed = sum(1 for _, ok, _ in RESULTS if ok)
    print("\n" + "=" * 70)
    print(f"共 {total} 项，通过 {passed} 项，失败 {total - passed} 项")
    if total - passed:
        print("\n失败项：")
        for name, ok, detail in RESULTS:
            if not ok:
                print(f"  - {name}  [{detail}]")
        print("\n结果: FAIL")
        return 1
    print("结果: PASS —— 备份/恢复与失败保护符合预期")
    return 0


if __name__ == "__main__":
    try:
        code = main()
    finally:
        shutil.rmtree(TMP, ignore_errors=True)
    sys.exit(code)
