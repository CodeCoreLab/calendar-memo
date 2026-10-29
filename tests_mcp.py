"""MCP Server 端到端测试（stdio 真实协议，不是模拟调用）。

用法：.venv/Scripts/python.exe tests_mcp.py <token>
"""

import asyncio
import json
import os
import sys

from mcp import ClientSession, StdioServerParameters, stdio_client

TOKEN = sys.argv[1] if len(sys.argv) > 1 else os.environ.get("MEMO_TOKEN", "")
PY = os.path.join(os.path.dirname(os.path.abspath(__file__)), ".venv", "Scripts", "python.exe")
SERVER = os.path.join(os.path.dirname(os.path.abspath(__file__)), "mcp_server.py")

RESULTS = []


def check(name, cond, detail=""):
    RESULTS.append((name, bool(cond), detail))
    print(f"{'PASS' if cond else 'FAIL'}  {name}" + (f"  [{detail}]" if detail else ""))


def payload(res):
    """MCP 工具返回结构化内容时，content[0].text 里通常就是 JSON 字符串。"""
    for c in getattr(res, "content", []) or []:
        t = getattr(c, "text", None)
        if t:
            try:
                return json.loads(t)
            except json.JSONDecodeError:
                return {"raw": t}
    if getattr(res, "structuredContent", None):
        return res.structuredContent
    return {}


async def main():
    params = StdioServerParameters(
        command=PY, args=[SERVER], env={**os.environ, "MEMO_TOKEN": TOKEN},
        encoding_error_handler="replace",
    )
    async with stdio_client(params) as (r, w):
        async with ClientSession(r, w) as s:
            await s.initialize()

            tools = await s.list_tools()
            names = [t.name for t in tools.tools]
            check("工具可列出", len(names) >= 14, f"{len(names)} 个")
            print("     工具:", ", ".join(names))

            # whoami
            d = payload(await s.call_tool("whoami", {}))
            check("whoami 绑定账号", d.get("ok") and d.get("nickname"), str(d.get("nickname")))

            # parse
            # 「明天下午3点开会 2小时」→ 明天 15:00~17:00（相对当前日期断言，避免跨天失败）
            from datetime import datetime, timedelta
            d = payload(await s.call_tool("parse_text", {"text": "明天下午3点开会 2小时 紧急"}))
            expect = (datetime.now() + timedelta(days=1)).strftime("%Y-%m-%d") + "T15:00"
            got = d.get("start", "")
            check("parse_text 解析", got == expect,
                  f"{got} ~ {d.get('end')}（期望 {expect}）")

            # create
            d = payload(await s.call_tool("create_event",
                                          {"text": "下周三上午10点 项目评审 1小时 #评审 紧急"}))
            eid = (d.get("event") or {}).get("id")
            check("create_event 自然语言", d.get("ok") and eid,
                  f"id={eid} {(d.get('event') or {}).get('title')}")

            # get
            d2 = payload(await s.call_tool("get_event", {"event_id": eid}))
            check("get_event 详情", d2.get("ok") and d2["event"]["level"] == "high",
                  f"level={d2['event']['level']} tags={[t['name'] for t in d2['event']['tags']]}")

            # update
            d3 = payload(await s.call_tool("update_event",
                                           {"event_id": eid, "level": "low", "remark": "MCP 改的"}))
            check("update_event 局部更新", d3.get("ok") and d3["event"]["level"] == "low")

            # complete
            d4 = payload(await s.call_tool("complete_event", {"event_id": eid, "done": True}))
            check("complete_event", d4.get("ok") and d4["event"]["state"] == "done")

            # batch
            d5 = payload(await s.call_tool("create_events_batch", {"items": json.dumps([
                {"text": "后天 上午9点 交报告 1小时"},
                {"title": "MCP结构化", "start": "2026-11-12T14:00", "end": "2026-11-12T15:00"},
            ])}))
            check("create_events_batch", d5.get("succeeded") == 2, f"{d5.get('succeeded')} 成功")

            # list
            d6 = payload(await s.call_tool("list_events", {"limit": 5}))
            check("list_events", d6.get("ok") and isinstance(d6.get("items"), list),
                  f"{d6.get('total')} 条")

            # agenda
            # agenda：用「明天」而非写死日期，避免跨天后变成过去日期
            from datetime import datetime as _dt, timedelta as _td
            tom = (_dt.now() + _td(days=1)).strftime("%Y-%m-%d")
            d7 = payload(await s.call_tool("get_agenda", {"date_str": tom, "days": 3}))
            check("get_agenda", d7.get("ok") and len(d7.get("days") or []) == 3)

            # brief
            d8 = payload(await s.call_tool("brief_status", {}))
            check("brief_status", d8.get("ok") and d8.get("summary"), (d8.get("summary") or "")[:40])

            # tags
            d9 = payload(await s.call_tool("list_tags", {}))
            check("list_tags", d9.get("ok") and isinstance(d9.get("tags"), list),
                  f"{len(d9.get('tags') or [])} 个")

            # calendar
            d10 = payload(await s.call_tool("get_calendar_month", {"year": 2026, "month": 11}))
            check("get_calendar_month", d10.get("ok") and "days" in d10)

            # create_tag
            d11 = payload(await s.call_tool("create_tag", {"name": "MCP标签"}))
            check("create_tag", d11.get("ok") and d11.get("tag", {}).get("name") == "MCP标签")

            # subtask toggle：先给某条日程加子任务再勾选
            d12 = payload(await s.call_tool("create_event", {
                "title": "子任务测试", "start": "2026-11-20T10:00",
                "end": "2026-11-20T11:00", "subtasks": "步骤甲,步骤乙"}))
            nev = (d12.get("event") or {})
            sid = (nev.get("subtasks") or [{}])[0].get("id")
            d13 = payload(await s.call_tool("toggle_subtask", {"subtask_id": sid}))
            check("toggle_subtask", d13.get("ok") and d13.get("progress", {}).get("percent") == 50,
                  str(d13.get("progress")))

            # delete
            d14 = payload(await s.call_tool("delete_event", {"event_id": nev.get("id")}))
            check("delete_event", d14.get("ok") and d14["deleted"]["id"] == nev.get("id"))

    ok = sum(1 for _, c, _ in RESULTS if c)
    print(f"\n{ok}/{len(RESULTS)} 通过")
    return 0 if ok == len(RESULTS) else 1


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
