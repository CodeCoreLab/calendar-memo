"""安全回归测试：鉴权边界 + 越权防护 + 本轮 P0 修复固化。

用法：.venv/Scripts/python.exe tests_security.py

设计要点
--------
- 全程用临时数据目录（tempfile.mkdtemp），绝不触碰 data/memo.db。
- 每个用例独立 test_client（cookie 不串），跨会话攻击才能测出来。
- 断言的是「攻击失败」而不只是「正常路径通过」——回归价值在后者。

覆盖的本轮修复
- P0-1 找回密码验证码：随机化 + 会话/邮箱/时效三重绑定
- P0-2 /api/visit：限长 + 限频
"""

import json
import os
import shutil
import sqlite3
import sys
import tempfile

HERE = os.path.dirname(os.path.abspath(__file__))
TMP = tempfile.mkdtemp(prefix="memo_sec_")
os.environ["MEMO_DATA_DIR"] = TMP
sys.path.insert(0, HERE)

import app as memo   # noqa: E402  （必须先设 MEMO_DATA_DIR 再 import）

RESULTS = []


def check(name, cond, detail=""):
    RESULTS.append((name, bool(cond), detail))
    print(f"{'PASS' if cond else 'FAIL'}  {name}" + (f"  [{detail}]" if detail else ""))
    return bool(cond)


def new_client():
    """每个用例用新 client，保证 session 独立——跨会话攻击才测得出。"""
    return memo.app.test_client()


def post(c, path, payload=None):
    r = c.post(path, json=payload if payload is not None else {})
    try:
        return r.status_code, (r.get_json() or {})
    except Exception:
        return r.status_code, {}


def get(c, path):
    r = c.get(path)
    try:
        return r.status_code, (r.get_json() or {})
    except Exception:
        return r.status_code, {}


def login(username, password):
    c = new_client()
    _, d = post(c, "/api/memo/login", {"username": username, "password": password})
    return c, d


def seed_db():
    """建库并灌入两个账号，返回 (主账号, 副账号) 的凭证。"""
    conn = sqlite3.connect(memo.DB_PATH)
    conn.executescript(memo.SCHEMA)
    conn.commit()
    from werkzeug.security import generate_password_hash
    pw_a, pw_b = "SecPass12345", "SecPass67890"
    conn.execute(
        "INSERT INTO users(username, nickname, email, pwd_hash, member_expire, points, created_at)"
        " VALUES (?,?,?,?,?,?,?)",
        ("sec_user_a", "甲", "a@sec.local", generate_password_hash(pw_a), "", 0, memo.now_str()))
    uid_a = conn.execute("SELECT id FROM users WHERE username='sec_user_a'").fetchone()[0]
    conn.execute(
        "INSERT INTO users(username, nickname, email, pwd_hash, member_expire, points, created_at)"
        " VALUES (?,?,?,?,?,?,?)",
        ("sec_user_b", "乙", "b@sec.local", generate_password_hash(pw_b), "", 0, memo.now_str()))
    uid_b = conn.execute("SELECT id FROM users WHERE username='sec_user_b'").fetchone()[0]
    conn.execute("INSERT INTO user_setting(user_id) VALUES (?)", (uid_a,))
    conn.execute("INSERT INTO user_setting(user_id) VALUES (?)", (uid_b,))
    conn.commit()
    conn.close()
    return ("sec_user_a", pw_a, uid_a), ("sec_user_b", pw_b, uid_b)


def main():
    print("=" * 70)
    print("安全回归测试 · 鉴权边界与越权防护")
    print("=" * 70)
    print(f"临时数据目录: {TMP}\n")

    (ua, pwa, uida), (ub, pwb, uidb) = seed_db()

    # ─────────── 一、P0-1 找回密码验证码 ───────────
    print("── 一、找回密码（P0-1 修复固化）──")
    EMAIL_B = "b@sec.local"

    # 1.1 攻击：全新会话直接猜历史固定码
    c_att = new_client()
    _, d = post(c_att, "/api/memo/reset_password",
                {"email": EMAIL_B, "code": "246810", "password": "Hacked12345"})
    check("1.1 全新会话猜固定码 246810 被拒", d.get("code") != 0, d.get("msg", ""))

    # 1.2 攻击：空验证码
    c_att2 = new_client()
    _, d = post(c_att2, "/api/memo/reset_password",
                {"email": EMAIL_B, "code": "", "password": "Hacked12345"})
    check("1.2 空验证码被拒", d.get("code") != 0, d.get("msg", ""))

    # 1.3 正常流程：拿码 → 同会话改自己密码
    c_ok = new_client()
    _, d = post(c_ok, "/api/memo/send_code", {"email": EMAIL_B})
    code_b = (d.get("data") or {}).get("demoCode")
    check("1.3 send_code 返回 6 位随机码",
          bool(code_b) and len(code_b) == 6 and code_b != "246810", str(code_b))

    _, d = post(c_ok, "/api/memo/reset_password",
                {"email": EMAIL_B, "code": code_b, "password": "NewPassB123"})
    check("1.4 同会话用本人码可重置", d.get("code") == 0, d.get("msg", ""))

    # 1.5 攻击：A 会话拿到的码，B 会话拿来用
    c_a = new_client()
    _, d = post(c_a, "/api/memo/send_code", {"email": "a@sec.local"})
    code_a = (d.get("data") or {}).get("demoCode")
    c_b = new_client()
    _, d = post(c_b, "/api/memo/reset_password",
                {"email": "a@sec.local", "code": code_a, "password": "Hacked12345"})
    check("1.5 跨会话重放被拒", d.get("code") != 0, d.get("msg", ""))

    # 1.6 攻击：用 A 邮箱的码改 B 账号
    _, d = post(c_a, "/api/memo/reset_password",
                {"email": EMAIL_B, "code": code_a, "password": "Hacked12345"})
    check("1.6 拿 A 的码改 B 账号被拒", d.get("code") != 0, d.get("msg", ""))

    # 1.7 验证 1.1 的密码确实没被改掉（用新密码登录应失败）
    c_v = new_client()
    _, d = post(c_v, "/api/memo/login", {"username": ub, "password": "Hacked12345"})
    check("1.7 攻击密码无法登录", d.get("code") != 0, d.get("msg", ""))

    # ─────────── 二、P0-2 /api/visit 限长限频 ───────────
    print("\n── 二、访问上报（P0-2 修复固化）──")
    c_v2 = new_client()
    _, d = post(c_v2, "/api/visit", {"page": "x" * 100000, "title": "y" * 100000})
    check("2.1 超长字段不报错（静默截断）", d.get("code") == 0, d.get("msg", ""))

    conn = sqlite3.connect(memo.DB_PATH)
    maxlen = conn.execute("SELECT MAX(LENGTH(page)) FROM visits").fetchone()[0] or 0
    check("2.2 落库长度被截断到 200", maxlen <= 200, f"实际 {maxlen}")
    base = conn.execute("SELECT COUNT(*) FROM visits").fetchone()[0]
    conn.close()

    for i in range(25):
        post(c_v2, "/api/visit", {"page": f"p{i}"})
    conn = sqlite3.connect(memo.DB_PATH)
    after = conn.execute("SELECT COUNT(*) FROM visits").fetchone()[0]
    conn.close()
    check("2.3 单会话限频生效（25 次只落 ≤20）", after - base <= 20, f"新增 {after - base}")

    # ─────────── 三、鉴权边界：未登录访问 ───────────
    print("\n── 三、未登录访问必须被拒 ───")
    need_auth = [
        ("/api/memo/events", "GET"),
        ("/api/memo/summary", "GET"),
        ("/api/memo/tags", "GET"),
        ("/api/memo/settings", "GET"),
        ("/api/memo/team/list", "GET"),
        ("/api/member/dashboard", "GET"),
        ("/api/memo/report/inbox", "GET"),
        ("/api/memo/share/inbox", "GET"),
        ("/api/memo/backup", "POST"),
        ("/api/memo/notices", "GET"),
    ]
    anon = new_client()
    for path, method in need_auth:
        if method == "GET":
            st, d = get(anon, path)
        else:
            st, d = post(anon, path, {})
        # 本项目约定：业务错误放 body（code=401），HTTP 状态可能仍是 200
        blocked = (d.get("code") == 401) or (st in (401, 403))
        check(f"3.x 未登录 {method} {path} 被拒", blocked, f"HTTP {st} code={d.get('code')}")

    # ─────────── 四、越权：A 不能碰 B 的数据 ───────────
    print("\n── 四、横向越权防护 ───")
    ca, da = login(ua, pwa)
    cb_, db_ = login(ub, pwb)   # b 的密码在 1.4 被改成 NewPassB123
    cb_, db_ = login(ub, "NewPassB123")
    check("4.0 A/B 各自登录成功", da.get("code") == 0 and db_.get("code") == 0)

    # A 建一条日程，B 尝试读写删
    _, d = post(ca, "/api/memo/event", {
        "title": "A 的私密日程", "startTime": "2026-12-01T09:00",
        "endTime": "2026-12-01T10:00", "level": "middle", "status": "doing"})
    eid = (d.get("code") == 0) and (d.get("data") or {}).get("id")
    check("4.1 A 建日程成功", bool(eid), str(eid))

    if eid:
        _, d = get(cb_, f"/api/memo/event/{eid}")
        leaked = (d.get("code") == 0) and (d.get("data") or {}).get("title") == "A 的私密日程"
        check("4.2 B 读 A 的日程被拒", not leaked, f"code={d.get('code')}")

        _, d = post(cb_, f"/api/memo/event/{eid}", {"title": "B 篡改"})
        check("4.3 B 改 A 的日程被拒", d.get("code") != 0, d.get("msg", ""))

        r = cb_.delete(f"/api/memo/event/{eid}")
        d = r.get_json() or {}
        check("4.4 B 删 A 的日程被拒", d.get("code") != 0, d.get("msg", ""))

        # 确认 A 的日程还在
        _, d = get(ca, f"/api/memo/event/{eid}")
        still = (d.get("code") == 0) and (d.get("data") or {}).get("title") == "A 的私密日程"
        check("4.5 攻击后 A 的日程完好", still)

    # ─────────── 五、API Token 鉴权 ───────────
    print("\n── 五、AI Token 鉴权 ───")
    _, d = post(ca, "/api/ai/token", {"username": ua, "password": pwa, "name": "sec-test"})
    tok = (d.get("data") or {}).get("token")
    check("5.1 签发 Token 成功", bool(tok), str((d.get("data") or {}).get("prefix")))

    if tok:
        bad = "memo_" + "0" * 40
        r = new_client().get("/api/ai/brief", headers={"Authorization": f"Bearer {bad}"})
        d = r.get_json() or {}
        check("5.2 伪造 Token 被拒", d.get("ok") is not True, f"code={d.get('code')}")

        r = new_client().get("/api/ai/brief", headers={"Authorization": f"Bearer {tok}"})
        d = r.get_json() or {}
        check("5.3 有效 Token 可用", d.get("ok") is True or d.get("code") == 0)

        # Token 只存哈希：库里不应有明文
        conn = sqlite3.connect(memo.DB_PATH)
        rows = [r[0] for r in conn.execute("SELECT token_hash FROM api_tokens")]
        conn.close()
        check("5.4 库里不存 Token 明文", tok not in rows and all(len(h) == 64 for h in rows),
              f"{len(rows)} 条哈希")

    # ─────────── 六、登录防爆破 ───
    # 设计意图（刻意如此，别当缺陷改）：锁定期内用正确密码仍放行——防止攻击者
    # 乱试把主人锁在门外。代价是要限制锁定期内的尝试次数，否则锁定期会变成
    # 攻击者的无限尝试窗口。LOGIN_LOCK_EXTRA 就是这个配额。
    print("\n── 六、登录防爆破 ───")
    # 各用例之间必须重置失败计数：LOGIN_FAIL 是按 username+IP 的进程级状态，
    # 不清就会从上一个用例带过来，导致「配额被前面的用例吃掉」这种假失败。
    memo.LOGIN_FAIL.clear()
    c_bf = new_client()
    seen_lock_msg = False
    for _ in range(memo.LOGIN_MAX_FAIL):
        _, d = post(c_bf, "/api/memo/login", {"username": ua, "password": "WrongPass!"})
    _, d = post(c_bf, "/api/memo/login", {"username": ua, "password": "WrongPass!"})
    seen_lock_msg = "次数过多" in str(d.get("msg", ""))
    check(f"6.1 连错 {memo.LOGIN_MAX_FAIL}+ 次后进入锁定提示", seen_lock_msg, d.get("msg", ""))

    # 6.2 锁定期内正确密码放行（防误锁设计，必须保住）
    memo.LOGIN_FAIL.clear()
    c_own = new_client()
    for _ in range(memo.LOGIN_MAX_FAIL):
        post(c_own, "/api/memo/login", {"username": ua, "password": "WrongPass!"})
    _, d = post(c_own, "/api/memo/login", {"username": ua, "password": pwa})
    check("6.2 锁定期内本人正确密码仍可登录（防误锁）", d.get("code") == 0, d.get("msg", ""))

    # 6.3 锁定期内配额用尽后，连正确密码也不放行（关闭无限窗口）
    memo.LOGIN_FAIL.clear()
    c_extra = new_client()
    for _ in range(memo.LOGIN_MAX_FAIL):
        post(c_extra, "/api/memo/login", {"username": ub, "password": "WrongPass!"})
    for _ in range(memo.LOGIN_LOCK_EXTRA + 2):
        post(c_extra, "/api/memo/login", {"username": ub, "password": "WrongMore!"})
    _, d = post(c_extra, "/api/memo/login", {"username": ub, "password": "NewPassB123"})
    blocked = d.get("code") != 0
    check("6.3 锁定期配额用尽后正确密码也被拦", blocked, d.get("msg", ""))

    memo.LOGIN_FAIL.clear()
    print("\n── 七、锁定隔离 ───")
    c_other = new_client()
    _, d = post(c_other, "/api/memo/login", {"username": "sec_never_tried", "password": "x"})
    check("7.1 锁 A 不影响未试过的账号", d.get("code") != 0 and "次数过多" not in str(d.get("msg", "")),
          d.get("msg", ""))

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
    print("结果: PASS —— 鉴权边界与越权防护全部符合预期")
    return 0


if __name__ == "__main__":
    try:
        code = main()
    finally:
        shutil.rmtree(TMP, ignore_errors=True)
    sys.exit(code)
