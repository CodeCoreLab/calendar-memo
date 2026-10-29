"""单端口统一服务：Web + REST API + MCP（HTTP）。

背景：Flask 是 WSGI，MCP 的 Streamable HTTP 是 ASGI，两者不能直接互相挂载。
本文件用**前置路由**把它们拼在一个端口上：
    /mcp*  → MCP ASGI 应用（内部用 uvicorn 起在回环端口，由本层转发）
    其它   → waitress 跑 Flask

这样客户端只需记住一个地址：http://127.0.0.1:8000/mcp

用法：
    python serve.py                    # 默认 8000
    python serve.py --port 9000
    python serve.py --no-mcp           # 只跑 Web/REST
"""

import argparse
import json
import os
import socket
import sys
import threading
import time
import urllib.error
import urllib.request

import app as memo

WEB_PORT = 8000
MCP_INTERNAL_PORT = 0          # 0 = 自动选空闲端口
MCP_PATH_PREFIX = "/mcp"


def pick_port():
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    p = s.getsockname()[1]
    s.close()
    return p


class MCPForwarder:
    """把 /mcp 前缀的请求转发给内部 ASGI 服务（uvicorn）。

    使用 urllib 直连回环端口，避免额外依赖 aiohttp。
    转发前先校验 Bearer Token：无有效凭据直接 401，
    避免客户端握手成功却在每次工具调用时才失败（排查成本高）。
    """

    # Token 校验结果缓存：避免每个 MCP 请求都开一次数据库连接。
    # 命中缓存 60 秒，吊销 Token 最多延迟 1 分钟生效（可接受，且省掉大量连接开销）。
    _CACHE = {}
    _CACHE_TTL = 60.0
    _CACHE_LOCK = threading.Lock()

    def __init__(self, upstream_port):
        self.up = upstream_port
        self.opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))

    @classmethod
    def _token_valid(cls, raw):
        if not raw:
            return False
        h = memo.hash_token(raw)
        now = time.time()
        with cls._CACHE_LOCK:
            hit = cls._CACHE.get(h)
            if hit and now - hit[1] < cls._CACHE_TTL:
                return hit[0]
        ok = False
        try:
            conn = memo.sqlite3.connect(memo.DB_PATH)
            try:
                conn.execute("PRAGMA busy_timeout = 3000")
                ok = conn.execute(
                    "SELECT 1 FROM api_tokens WHERE token_hash=? AND revoked=0",
                    (h,)).fetchone() is not None
            finally:
                conn.close()
        except memo.sqlite3.Error:
            return False
        with cls._CACHE_LOCK:
            if len(cls._CACHE) > 512:
                cls._CACHE.clear()
            cls._CACHE[h] = (ok, now)
        return ok

    @classmethod
    def invalidate(cls, raw=""):
        """吊销/签发 Token 后清缓存，让变更立即生效。"""
        with cls._CACHE_LOCK:
            if raw:
                cls._CACHE.pop(memo.hash_token(raw), None)
            else:
                cls._CACHE.clear()

    def __call__(self, environ, start_response):
        method = environ.get("REQUEST_METHOD", "GET")
        path = environ.get("PATH_INFO", MCP_PATH_PREFIX)
        qs = environ.get("QUERY_STRING", "")

        # MCP 必须带有效 Token：无凭据/无效凭据一律 401，不进入 MCP 协议层
        raw = ""
        auth = environ.get("HTTP_AUTHORIZATION", "") or ""
        if auth.lower().startswith("bearer "):
            raw = auth[7:].strip()
        if not raw:
            raw = (environ.get("HTTP_X_API_TOKEN") or "").strip()
        if not self._token_valid(raw):
            msg = json.dumps({"ok": False, "error": {
                "code": "unauthorized" if not raw else "invalid_token",
                "message": "MCP 需要有效的 Bearer Token：先运行 "
                           "python mcp_server.py --issue-token <用户> <密码>"}},
                ensure_ascii=False).encode()
            start_response("401 Unauthorized",
                           [("Content-Type", "application/json; charset=utf-8"),
                            ("Content-Length", str(len(msg))),
                            ("WWW-Authenticate", 'Bearer realm="calendar-memo"')])
            return [msg]

        url = f"http://127.0.0.1:{self.up}{path}" + (f"?{qs}" if qs else "")

        length = environ.get("CONTENT_LENGTH") or "0"
        try:
            body = environ["wsgi.input"].read(int(length)) if int(length) > 0 else None
        except (ValueError, KeyError):
            body = None

        headers = {}
        for k, v in environ.items():
            if k.startswith("HTTP_"):
                hk = k[5:].replace("_", "-").title()
                if hk.lower() in ("host", "content-length", "connection"):
                    continue
                headers[hk] = v
            elif k in ("CONTENT_TYPE", "CONTENT_LENGTH"):
                headers[k.replace("_", "-").title()] = v
        headers["Host"] = f"127.0.0.1:{self.up}"

        req = urllib.request.Request(url, data=body, method=method, headers=headers)
        try:
            resp = self.opener.open(req, timeout=300)
            status = resp.status
            raw = resp.read()
            out_headers = [(k, v) for k, v in resp.headers.items()
                           if k.lower() not in ("transfer-encoding", "connection",
                                                "content-encoding", "content-length")]
        except urllib.error.HTTPError as exc:
            status = exc.code
            raw = exc.read()
            out_headers = [(k, v) for k, v in exc.headers.items()
                           if k.lower() not in ("transfer-encoding", "connection",
                                                "content-length")]
        except Exception as exc:  # 上游没起来
            msg = f"MCP upstream unavailable: {exc}".encode()
            start_response("502 Bad Gateway",
                           [("Content-Type", "text/plain; charset=utf-8"),
                            ("Content-Length", str(len(msg)))])
            return [msg]

        out_headers.append(("Content-Length", str(len(raw))))
        start_response(f"{status} OK", out_headers)
        return [raw]


def start_mcp_http(port):
    """在后台线程里用 uvicorn 起 MCP ASGI 应用。"""
    import uvicorn
    import mcp_server as ms

    # session_idle_timeout：默认 1800s（30 分钟）。长时间挂着的 AI 客户端
    # 若中途闲置超过该值，session 会被回收，需要重新 initialize。
    # 这里放宽到 8 小时，减少无谓重连；max_sessions 相应收紧防内存膨胀。
    star = ms.mcp.streamable_http_app(
        streamable_http_path=MCP_PATH_PREFIX,
        host="127.0.0.1",
        session_idle_timeout=float(os.environ.get("MCP_SESSION_IDLE_TIMEOUT", 8 * 3600)),
        max_sessions=int(os.environ.get("MCP_MAX_SESSIONS", 512)),
    )

    config = uvicorn.Config(star, host="127.0.0.1", port=port, log_level="warning",
                            access_log=False, lifespan="on")
    server = uvicorn.Server(config)
    t = threading.Thread(target=server.run, daemon=True, name="mcp-http")
    t.start()

    # 等内部服务就绪（最多 10 秒）
    for _ in range(50):
        try:
            sock = socket.create_connection(("127.0.0.1", port), timeout=0.2)
            sock.close()
            return True
        except OSError:
            time.sleep(0.2)
    return False


def main():
    ap = argparse.ArgumentParser(description="日历备忘录统一服务（Web + API + MCP）")
    ap.add_argument("--port", type=int, default=WEB_PORT)
    ap.add_argument("--host", default="127.0.0.1")
    ap.add_argument("--no-mcp", action="store_true", help="不启用 MCP HTTP")
    ap.add_argument("--threads", type=int, default=8)
    args = ap.parse_args()

    if not args.no_mcp:
        mcp_port = pick_port()
        try:
            ok = start_mcp_http(mcp_port)
        except Exception as exc:
            print(f"[warn] MCP HTTP 启动失败：{exc}（Web 与 REST 不受影响）", file=sys.stderr)
            ok = False
        if ok:
            # 把 /mcp 前缀请求转给内部 MCP 服务
            original_wsgi = memo.app.wsgi_app

            def router(environ, start_response):
                if environ.get("PATH_INFO", "").startswith(MCP_PATH_PREFIX):
                    return MCPForwarder(mcp_port)(environ, start_response)
                return original_wsgi(environ, start_response)

            memo.app.wsgi_app = router
            # Token 签发/吊销后立即让 MCP 层缓存失效，避免 60 秒延迟窗口
            memo.TOKEN_CACHE_HOOKS.append(MCPForwarder.invalidate)
            print(f"[mcp] HTTP 已启用 → http://{args.host}:{args.port}{MCP_PATH_PREFIX}")
        else:
            print("[warn] MCP HTTP 未就绪，仅提供 Web/REST", file=sys.stderr)

    hs = memo.run_housekeeping()
    print(f"[clean] 凭证图 {hs['uploads']} / 幂等 {hs['idempotency']} / 过期Token {hs['tokens']}")
    print(f"[web] 页面    → http://{args.host}:{args.port}/memo_login.html")
    print(f"[web] AI 自述 → http://{args.host}:{args.port}/api/ai/schema")
    print(f"[mcp] stdio   → python mcp_server.py")
    from waitress import serve
    serve(memo.app, host=args.host, port=args.port, threads=args.threads, channel_timeout=300)


if __name__ == "__main__":
    main()
