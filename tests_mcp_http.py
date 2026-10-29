"""MCP Streamable HTTP 传输测试：验证单端口 /mcp 可用（含请求头 Token 鉴权）。

用法：.venv/Scripts/python.exe tests_mcp_http.py [url] [token]
"""

import asyncio
import json
import os
import sys

import httpx2
from mcp import ClientSession
from mcp.client.streamable_http import streamable_http_client

URL = sys.argv[1] if len(sys.argv) > 1 else "http://127.0.0.1:8000/mcp"
TOKEN = sys.argv[2] if len(sys.argv) > 2 else os.environ.get("MEMO_TOKEN", "")
RESULTS = []


def check(name, cond, detail=""):
    RESULTS.append((name, bool(cond)))
    print(f"{'PASS' if cond else 'FAIL'}  {name}" + (f"  [{detail}]" if detail else ""))


def payload(res):
    for c in getattr(res, "content", []) or []:
        t = getattr(c, "text", None)
        if t:
            try:
                return json.loads(t)
            except json.JSONDecodeError:
                return {"raw": t}
    return getattr(res, "structuredContent", None) or {}


async def main():
    headers = {"Authorization": f"Bearer {TOKEN}"} if TOKEN else {}
    async with httpx2.AsyncClient(headers=headers, timeout=60) as client:
        async with streamable_http_client(URL, http_client=client) as (r, w):
            async with ClientSession(r, w) as s:
                await s.initialize()
                tools = await s.list_tools()
                check("HTTP 握手 + 列工具", len(tools.tools) >= 14, f"{len(tools.tools)} 个")

                d = payload(await s.call_tool("whoami", {}))
                check("whoami（请求头 Token 鉴权）", d.get("ok"),
                      str(d.get("nickname") or d.get("error")))

                d = payload(await s.call_tool("brief_status", {}))
                check("brief_status（HTTP）", d.get("ok") and d.get("summary"),
                      (d.get("summary") or "")[:36])

                d = payload(await s.call_tool("parse_text", {"text": "明天上午9点 晨会 30分钟"}))
                check("parse_text（HTTP）", d.get("understood"),
                      f"{d.get('start')} ~ {d.get('end')}")

                d = payload(await s.call_tool("create_event", {
                    "title": "HTTP传输验证", "start": "2026-11-25T10:00",
                    "end": "2026-11-25T11:00"}))
                eid = (d.get("event") or {}).get("id")
                check("create_event（HTTP）", d.get("ok") and eid, f"id={eid}")

                d = payload(await s.call_tool("list_events", {"limit": 3}))
                check("list_events（HTTP）", d.get("ok"), f"{d.get('total')} 条")

                if eid:
                    d = payload(await s.call_tool("delete_event", {"event_id": eid}))
                    check("delete_event（HTTP 清理）", d.get("ok"))

    ok = sum(1 for _, c in RESULTS if c)
    print(f"\n{ok}/{len(RESULTS)} 通过")
    return 0 if ok == len(RESULTS) else 1


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
