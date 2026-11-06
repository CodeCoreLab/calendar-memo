"""B16 回归测试：WSGI 状态行必须是 `<code> <reason-phrase>`。

缺陷：serve.py 原来用 `start_response(f"{status} OK", ...)`，
非 2xx（如 404）会产生 "404 OK" 这种码/短语不匹配的非法状态行，
严格 HTTP 解析器与部分代理会报错。

跑法：python -m pytest tests_serve_status.py -v
"""

import io
import os
import sys
import tempfile
import urllib.error
from unittest import mock

HERE = os.path.dirname(os.path.abspath(__file__))
TMP = tempfile.mkdtemp(prefix="memo_serve_status_")
os.environ["MEMO_DATA_DIR"] = TMP
sys.path.insert(0, HERE)

import serve   # noqa: E402  （必须先设 MEMO_DATA_DIR 再 import）


def test_status_line_uses_matching_reason_phrase():
    assert serve.status_line(200) == "200 OK"
    assert serve.status_line(404) == "404 Not Found"
    assert serve.status_line(401) == "401 Unauthorized"
    assert serve.status_line(502) == "502 Bad Gateway"
    assert serve.status_line(503) == "503 Service Unavailable"


def test_status_line_falls_back_for_non_standard_code():
    # 上游可能返回非标准码；状态行仍必须是「码 + 空格 + 短语」
    line = serve.status_line(599)
    assert line == "599 Unknown"
    assert line.startswith("599 ")
    assert "599 599" not in line


def _call_forwarder(upstream_exc=None, upstream_resp=None):
    """跑一次 MCPForwarder.__call__，返回 start_response 收到的状态行。"""
    captured = {}

    def start_response(status, headers):
        captured["status"] = status
        captured["headers"] = headers

    environ = {
        "REQUEST_METHOD": "GET",
        "PATH_INFO": "/mcp",
        "QUERY_STRING": "",
        "HTTP_AUTHORIZATION": "Bearer dummy-token",
        "CONTENT_LENGTH": "0",
        "wsgi.input": io.BytesIO(b""),
        "wsgi.url_scheme": "http",
        "SERVER_NAME": "127.0.0.1",
        "SERVER_PORT": "8000",
    }

    fwd = serve.MCPForwarder(12345)
    side_effect = upstream_exc if upstream_exc is not None else None
    with mock.patch.object(serve.MCPForwarder, "_token_valid", return_value=True), \
            mock.patch.object(fwd.opener, "open",
                              side_effect=side_effect, return_value=upstream_resp):
        body = fwd(environ, start_response)
    captured["body"] = body
    return captured


def test_non_2xx_upstream_response_has_valid_status_line():
    """上游返回 404 时，状态行不能是 "404 OK"。"""
    exc = urllib.error.HTTPError(
        "http://127.0.0.1:12345/mcp", 404, "Not Found", {"Content-Type": "text/plain"}, None)
    exc.read = lambda: b"nope"

    got = _call_forwarder(exc)
    status = got["status"]

    assert status == "404 Not Found", f"状态行非法：{status!r}"
    code, _, phrase = status.partition(" ")
    assert code == "404"
    assert phrase and phrase != "OK"


def test_non_2xx_upstream_5xx_has_valid_status_line():
    exc = urllib.error.HTTPError(
        "http://127.0.0.1:12345/mcp", 503, "Service Unavailable", {}, None)
    exc.read = lambda: b"down"

    got = _call_forwarder(exc)
    assert got["status"] == "503 Service Unavailable"


def test_2xx_upstream_response_keeps_ok():
    ok_resp = mock.Mock()
    ok_resp.status = 200
    ok_resp.read = lambda: b'{"ok":true}'
    ok_resp.headers = mock.Mock()
    ok_resp.headers.items.return_value = [("Content-Type", "application/json")]

    got = _call_forwarder(upstream_resp=ok_resp)
    assert got["status"] == "200 OK"
