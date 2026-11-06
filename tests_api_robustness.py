"""回归测试：A12 / A25 / A26 / A44（stdlib unittest，无第三方依赖）

覆盖的本轮修复
- A12 Token 签发时 scope 由服务端按账号能力封顶，调用方不能自选全权 scope
- A25 多个 /api/ 端点的 int(query) 未容错，非法参数不再 500
- A26 Token 使用计数不再每次请求都 UPDATE + commit（改为批量落库）
- A44 require_login() 的 api_invalid 分支不再是死代码（普通端点也能区分
      「Token 无效」与「未登录」）

跑法：cd <repo> && python -m unittest tests_api_robustness -v
"""

import atexit
import os
import shutil
import sqlite3
import sys
import tempfile
import unittest
from datetime import datetime, timedelta

HERE = os.path.dirname(os.path.abspath(__file__))
TMP = tempfile.mkdtemp(prefix="memo_robust_")
os.environ["MEMO_DATA_DIR"] = TMP
sys.path.insert(0, HERE)

import app as memo   # noqa: E402  （必须先设 MEMO_DATA_DIR 再 import）

PLAIN_USER, PLAIN_PW = "rb_plain", "PlainPass12345"
MEMBER_USER, MEMBER_PW = "rb_member", "MemberPass12345"


def seed_db():
    """建库并造两个账号：普通账号（非会员）与会员账号。

    幂等：多个 TestCase 共用同一个临时库，重复调用不应报 UNIQUE 冲突。
    """
    from werkzeug.security import generate_password_hash
    conn = sqlite3.connect(memo.DB_PATH, timeout=10)
    try:
        conn.execute("PRAGMA busy_timeout = 10000")
        conn.executescript(memo.SCHEMA)
        conn.commit()
        expire = (datetime.now() + timedelta(days=365)).strftime(memo.DT_FMT)
        for username, nickname, pwd, member_expire in (
                (PLAIN_USER, "普通", PLAIN_PW, ""),
                (MEMBER_USER, "会员", MEMBER_PW, expire)):
            conn.execute(
                "INSERT OR IGNORE INTO users"
                "(username, nickname, email, pwd_hash, is_admin, member_expire, points, created_at)"
                " VALUES (?,?,?,?,?,?,?,?)",
                (username, nickname, "", generate_password_hash(pwd), 0, member_expire, 0,
                 memo.now_str()))
        conn.commit()
    finally:
        conn.close()


def token_rows():
    conn = sqlite3.connect(memo.DB_PATH)
    conn.row_factory = sqlite3.Row
    rows = [dict(r) for r in conn.execute("SELECT * FROM api_tokens")]
    conn.close()
    return rows


def update_token_expiry(token_hash, value):
    conn = sqlite3.connect(memo.DB_PATH)
    conn.execute("UPDATE api_tokens SET expires_at = ? WHERE token_hash = ?", (value, token_hash))
    conn.commit()
    conn.close()


class Base(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        seed_db()

    def setUp(self):
        self.client = memo.app.test_client()

    def tearDown(self):
        # 每个用例独立清空 Token 表，避免互相影响计数断言
        conn = sqlite3.connect(memo.DB_PATH)
        conn.execute("DELETE FROM api_tokens")
        conn.commit()
        conn.close()
        memo._TOKEN_USE_PENDING.clear()

    # ── 工具 ──
    def post(self, path, payload=None, headers=None):
        r = self.client.post(path, json=payload if payload is not None else {},
                             headers=headers or {})
        try:
            return r.status_code, (r.get_json() or {})
        except Exception:
            return r.status_code, {}

    def get(self, path, headers=None):
        r = self.client.get(path, headers=headers or {})
        try:
            return r.status_code, (r.get_json() or {})
        except Exception:
            return r.status_code, {}

    def login(self, username, password):
        r = self.client.post("/api/memo/login", json={"username": username, "password": password})
        return (r.get_json() or {})

    def issue_token(self, username, password, scope=None):
        body = {"username": username, "password": password, "name": "rb"}
        if scope is not None:
            body["scope"] = scope
        st, d = self.post("/api/ai/token", body)
        return st, d


# ══════════════════════════════════════════════════════════════════
# A12 —— Token scope 必须由服务端封顶
# ══════════════════════════════════════════════════════════════════
class TestTokenScopeCap(Base):
    def test_plain_user_cannot_self_issue_write_scope(self):
        """非会员账号自签 read,write 必须被拒（A12 的核心绕过路径）。"""
        st, d = self.issue_token(PLAIN_USER, PLAIN_PW, "read,write")
        self.assertEqual(st, 403, d)
        self.assertFalse(d.get("ok", True), d)
        self.assertEqual(len(token_rows()), 0, "被拒的签发不应留下 Token 记录")

    def test_plain_user_cannot_self_issue_write_only_scope(self):
        st, d = self.issue_token(PLAIN_USER, PLAIN_PW, "write")
        self.assertEqual(st, 403, d)
        self.assertEqual(len(token_rows()), 0)

    def test_default_scope_is_capped_by_server(self):
        """不传 scope 时也由服务端推导（普通账号 → 只读），不能默认全权。"""
        st, d = self.issue_token(PLAIN_USER, PLAIN_PW)
        self.assertEqual(st, 200, d)
        self.assertEqual((d.get("data") or {}).get("scope"), "read", d)

    def test_read_scope_allowed_for_plain_user(self):
        st, d = self.issue_token(PLAIN_USER, PLAIN_PW, "read")
        self.assertEqual(st, 200, d)
        self.assertEqual((d.get("data") or {}).get("scope"), "read", d)

    def test_member_user_can_issue_write_scope(self):
        st, d = self.issue_token(MEMBER_USER, MEMBER_PW, "read,write")
        self.assertEqual(st, 200, d)
        self.assertEqual((d.get("data") or {}).get("scope"), "read,write", d)

    def test_read_only_token_cannot_write(self):
        """封顶后的只读 Token 写操作应被 403 read_only_token 拒绝。"""
        st, d = self.issue_token(PLAIN_USER, PLAIN_PW)
        tok = (d.get("data") or {}).get("token")
        self.assertTrue(tok, d)
        st, d = self.post("/api/ai/event", {"title": "x", "start": "2026-01-01 09:00"},
                          headers={"Authorization": f"Bearer {tok}"})
        self.assertEqual(st, 403, d)
        self.assertEqual((d.get("error") or {}).get("code"), "read_only_token", d)

    def test_unknown_scope_rejected(self):
        st, d = self.issue_token(MEMBER_USER, MEMBER_PW, "admin")
        self.assertEqual(st, 400, d)
        self.assertEqual(len(token_rows()), 0)

    def test_issued_token_carries_expiry(self):
        """签发的 Token 必须带过期时间（A12：泄漏后不能长期有效）。"""
        st, d = self.issue_token(MEMBER_USER, MEMBER_PW)
        self.assertEqual(st, 200, d)
        rows = token_rows()
        self.assertEqual(len(rows), 1)
        self.assertTrue(rows[0].get("expires_at"), rows[0])

    def test_expired_token_is_rejected(self):
        st, d = self.issue_token(MEMBER_USER, MEMBER_PW)
        tok = (d.get("data") or {}).get("token")
        self.assertTrue(tok, d)
        row = token_rows()[0]
        update_token_expiry(row["token_hash"],
                            (datetime.now() - timedelta(days=1)).strftime(memo.DT_FMT))
        st, d = self.get("/api/ai/brief", headers={"Authorization": f"Bearer {tok}"})
        self.assertNotEqual(st, 200, d)
        self.assertFalse(d.get("ok", True), d)


# ══════════════════════════════════════════════════════════════════
# A25 —— 非法 int 参数不再 500
# ══════════════════════════════════════════════════════════════════
class TestQueryIntTolerance(Base):
    """所有断言都要求「不是 500，且返回业务成功码」。"""

    def setUp(self):
        super().setUp()
        self.login(PLAIN_USER, PLAIN_PW)

    def _assert_ok(self, path):
        st, d = self.get(path)
        self.assertNotEqual(st, 500, f"{path} → HTTP {st} {d}")
        self.assertEqual(d.get("code"), 0, f"{path} → {d}")

    def test_events_paging_params(self):
        self._assert_ok("/api/memo/events?page=abc&pageSize=xyz&archived=notanint")

    def test_events_page_negative(self):
        self._assert_ok("/api/memo/events?page=-5&pageSize=-3")

    def test_notices_paging_params(self):
        self._assert_ok("/api/memo/notices?page=abc&pageSize=xyz")

    def test_points_limit_param(self):
        # else 分支的 limit 参数（原 1649 行 int() 未容错）
        self._assert_ok("/api/member/points?limit=abc")

    def test_share_inbox_paging_params(self):
        self._assert_ok("/api/memo/share/inbox?page=abc&pageSize=xyz")

    def test_report_inbox_paging_params(self):
        self._assert_ok("/api/memo/report/inbox?page=abc&pageSize=xyz")

    def test_share_sent_paging_params(self):
        self._assert_ok("/api/memo/share/sent?page=abc&pageSize=xyz")

    def test_report_sent_paging_params(self):
        self._assert_ok("/api/memo/report/sent?page=abc&pageSize=xyz")

    def test_archived_non_numeric_is_zero(self):
        """archived 非法值应回退 0（不过滤归档），而不是 500。"""
        st, d = self.get("/api/memo/events?archived=abc")
        self.assertEqual(st, 200, d)
        self.assertEqual(d.get("code"), 0, d)
        self.assertEqual((d.get("data") or {}).get("pageSize"), 6, d)


# ══════════════════════════════════════════════════════════════════
# A26 —— Token 使用计数批量落库
# ══════════════════════════════════════════════════════════════════
class TestTokenUsageBatching(Base):
    def test_usage_not_written_on_every_request(self):
        """连续请求不应每次都写库：计数先在内存累计，达到阈值才落库。"""
        st, d = self.issue_token(MEMBER_USER, MEMBER_PW)
        tok = (d.get("data") or {}).get("token")
        self.assertTrue(tok, d)
        for _ in range(3):
            st, d = self.get("/api/ai/brief", headers={"Authorization": f"Bearer {tok}"})
            self.assertEqual(st, 200, d)
        rows = token_rows()
        self.assertEqual(rows[0]["use_count"], 0,
                         "热路径不应每次请求都 UPDATE api_tokens（use_count 已落库）")
        self.assertEqual(rows[0]["last_used"], "", "last_used 也不应每次请求都写")

    def test_flush_persists_counters(self):
        """批量 flush 后计数与 last_used 正确落库。"""
        st, d = self.issue_token(MEMBER_USER, MEMBER_PW)
        tok = (d.get("data") or {}).get("token")
        for _ in range(3):
            self.get("/api/ai/brief", headers={"Authorization": f"Bearer {tok}"})
        memo._flush_token_use()
        rows = token_rows()
        self.assertEqual(rows[0]["use_count"], 3, rows[0])
        self.assertTrue(rows[0]["last_used"], rows[0])


# ══════════════════════════════════════════════════════════════════
# A44 —— require_login 的 api_invalid 分支不再是死代码
# ══════════════════════════════════════════════════════════════════
class TestRequireLoginBranch(Base):
    def test_invalid_token_distinguished_from_anonymous(self):
        """带无效 Token 与完全匿名，两者错误信息必须不同（A44 的真实差异化）。

        本项目约定：业务错误放 body（code=401），HTTP 状态仍是 200
        （见 tests_security.py 的既有断言），所以这里比对 body 的 msg。
        """
        st_anon, d_anon = self.get("/api/memo/events")
        st_bad, d_bad = self.get("/api/memo/events",
                                 headers={"Authorization": "Bearer memo_" + "0" * 40})
        self.assertEqual(d_anon.get("code"), 401, d_anon)
        self.assertEqual(d_bad.get("code"), 401, d_bad)
        self.assertNotEqual(d_anon.get("msg"), d_bad.get("msg"),
                            "无效 Token 与未登录必须给出不同提示")
        self.assertIn("Token", str(d_bad.get("msg")), d_bad)

    def test_anonymous_message_unchanged(self):
        st, d = self.get("/api/memo/events")
        self.assertEqual(d.get("msg"), "登录已失效", d)

    def test_valid_session_still_works(self):
        self.login(PLAIN_USER, PLAIN_PW)
        st, d = self.get("/api/memo/events")
        self.assertEqual(d.get("code"), 0, d)


if __name__ == "__main__":
    try:
        unittest.main(verbosity=2)
    finally:
        atexit.unregister(shutil.rmtree)
        shutil.rmtree(TMP, ignore_errors=True)
