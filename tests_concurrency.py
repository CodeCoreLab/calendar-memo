"""MCP 并发身份隔离测试。

背景：早期实现用模块级全局变量传递 MCP Context，HTTP 模式下多个客户端
并发调用会互相覆盖，实测 16 次里 6 次「张冠李戴」（A 的操作落到 B 账号）。
改为 contextvars 后必须验证：并发下身份不再串号。

用法：.venv/Scripts/python.exe tests_concurrency.py [url]
"""

import asyncio
import json
import os
import sys
import urllib.request

import httpx2
from mcp import ClientSession
from mcp.client.streamable_http import streamable_http_client

BASE = "http://127.0.0.1:8000"
URL = sys.argv[1] if len(sys.argv) > 1 else f"{BASE}/mcp"
ROUNDS = 10

print("=" * 62)
print("MCP 并发身份隔离测试")
print("=" * 62)


def _post(path, payload):
    op = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    req = urllib.request.Request(BASE + path, data=json.dumps(payload).encode(),
                                 headers={"Content-Type": "application/json"})
    return json.loads(op.open(req).read())


def payload_of(res):
    for c in getattr(res, "content", []) or []:
        t = getattr(c, "text", None)
        if t:
            try:
                return json.loads(t)
            except json.JSONDecodeError:
                return {"raw": t}
    return getattr(res, "structuredContent", None) or {}


# 两个不同账号各签发独立 Token
_U1 = os.environ.get("SEED_ADMIN_USER", "demo")
_P1 = os.environ.get("SEED_ADMIN_PASS", "demo12345")
_U2 = os.environ.get("SEED_TEAMMATE_USER", "teammate01")
_P2 = os.environ.get("SEED_TEAMMATE_PASS", "teammate12345")

tok_a = _post("/api/ai/token", {"username": _U1, "password": _P1,
                                "name": "concurrency-a"})["data"]["token"]
tok_b = _post("/api/ai/token", {"username": _U2, "password": _P2,
                                "name": "concurrency-b"})["data"]["token"]
print("已签发两个账号的独立 Token\n")


async def worker(token, label, expect, results):
    headers = {"Authorization": f"Bearer {token}"}
    async with httpx2.AsyncClient(headers=headers, timeout=60) as cl:
        async with streamable_http_client(URL, http_client=cl) as (r, w):
            async with ClientSession(r, w) as s:
                await s.initialize()
                for _ in range(ROUNDS):
                    d = payload_of(await s.call_tool("whoami", {}))
                    results.append((label, expect, d.get("nickname") or d.get("error")))


async def main():
    results = []
    await asyncio.gather(
        worker(tok_a, "A", "x小马", results),
        worker(tok_b, "B", "小李", results),
    )
    total = len(results)
    bad = [(lb, exp, got) for lb, exp, got in results if got != exp]
    ok = total - len(bad)

    from collections import Counter
    print("各账号返回的身份分布：")
    for (lb, exp), n in Counter((lb, got) for lb, exp, got in results).items():
        print(f"  {lb}（应={next(e for l, e, g in results if l == lb)}）→ 实际={n and [g for l, e, g in results if l == lb][0]}：{n} 次")

    print(f"\n总调用 {total} 次，正确 {ok} 次，串号 {len(bad)} 次")
    if bad:
        print("串号样例:", bad[:5])
        print("\n结果: FAIL —— 并发身份未隔离")
        return 1
    print("\n结果: PASS —— 并发下身份完全隔离")
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
