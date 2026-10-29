# AI / MCP 接入速查

服务地址：`http://127.0.0.1:8000`
当前签发的 Token（账号 `${SEED_ADMIN_USER:-demo}` / x小马，scope `read,write`）：

```
<your-token>
```

> 该 Token 只存哈希于 `data/memo.db`，**明文仅此一份**，遗失请重新签发（见文末）。
> 生产使用请换自己的 Token，勿提交到公开仓库。

---

## 1. 30 秒验证

```bash
T=<your-token>

# 我是谁
curl -s --noproxy '*' http://127.0.0.1:8000/api/ai/brief \
  -H "Authorization: Bearer $T"

# 自然语言建日程
curl -s --noproxy '*' -X POST http://127.0.0.1:8000/api/ai/event \
  -H "Authorization: Bearer $T" -H "Content-Type: application/json" \
  -d '{"text":"明天下午3点 项目评审 2小时 #项目A 紧急"}'
```

## 2. Python 调用

```python
import json, os, urllib.request

TOKEN = os.environ["MEMO_AI_TOKEN"]   # 示例值略，用 `python mcp_server.py --issue-token <用户> <密码>` 自行签发
BASE  = "http://127.0.0.1:8000"

def ai(path, payload=None, method=None):
    data = json.dumps(payload).encode() if payload else None
    req = urllib.request.Request(
        BASE + path, data=data, method=method or ("POST" if data else "GET"),
        headers={"Content-Type": "application/json",
                 "Authorization": f"Bearer {TOKEN}"})
    with urllib.request.urlopen(req) as r:
        return json.loads(r.read())

print(ai("/api/ai/brief")["data"]["summary"])
print(ai("/api/ai/event", {"text": "后天 上午9点 交报告 1小时"})["data"]["title"])
for it in ai("/api/ai/agenda?days=7")["data"]["days"]:
    print(it["date"], it["count"], "条")
```

## 3. MCP 接入

### Claude Desktop / Cursor（stdio）

```json
{
  "mcpServers": {
    "calendar-memo": {
      "command": "<你的项目路径>/.venv/Scripts/python.exe",
      "args": ["<你的项目路径>/mcp_server.py"],
      "env": { "MEMO_TOKEN": "<用 mcp_server.py --issue-token 签发>" }
    }
  }
}
```

### HTTP（同一服务端口，多客户端共用一个地址）

```
URL:    http://127.0.0.1:8000/mcp
Header: Authorization: Bearer <your-token>
```

Token 从请求头读取，因此**一个 MCP 服务可以同时服务多个不同账号的客户端**。

### 验证脚本

```bash
.venv/Scripts/python.exe tests_mcp.py <your-token>
.venv/Scripts/python.exe tests_mcp_http.py http://127.0.0.1:8000/mcp <your-token>
```

---

## 4. 模型使用建议（已写入 MCP instructions）

| 场景 | 推荐调用 |
|------|---------|
| 用户问「我现在忙不忙 / 有什么安排」 | `brief_status`（已算好统计，别自己重算） |
| 用户说「帮我记一下…」 | 先 `parse_text` 预览 → 再 `create_event` |
| 一次记多件事 | `create_events_batch` |
| 用户说「这个做完了」 | `complete_event`（重复日程会自动生成下一条） |
| 找某条日程 | `list_events` 过滤，或 `get_event` 按 id |
| 「这周都有什么」 | `get_agenda` days=7 |

## 5. 重新签发 Token

```bash
.venv/Scripts/python.exe mcp_server.py --issue-token ${SEED_ADMIN_USER:-demo} ${SEED_ADMIN_PASS:-demo12345}
```

或走 REST：

```bash
curl -s --noproxy '*' -X POST http://127.0.0.1:8000/api/ai/token \
  -H "Content-Type: application/json" \
  -d '{"username":"${SEED_ADMIN_USER:-demo}","password":"${SEED_ADMIN_PASS:-demo12345}","name":"my-agent"}'
```

吊销：`DELETE /api/ai/token/<id>`（带 Token 调用）。
查看自己的 Token 列表：`GET /api/ai/tokens`（不返回明文）。
