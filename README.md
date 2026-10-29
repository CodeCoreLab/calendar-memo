# 日历备忘录 · 本地 1:1 复刻版（含 AI / MCP 接口）

前端为像素级还原的单页日历；后端为本地 **Flask + SQLite** 实现，**可完全离线运行**；
并额外提供 **AI 专用 REST API** 与 **MCP Server**，供大模型直接读写日程。

> **演示项目定位**：这是本地单机可用的完整实现，不含任何真实业务数据。
> 默认口令仅用于本地演示，请勿部署到任何多人可达的环境。

## 快速启动

```bat
start.bat
```

或手动：

```bash
python -m venv .venv
.venv/Scripts/python.exe -m pip install -r requirements.txt
.venv/Scripts/python.exe seed.py            # 建表 + 演示数据
.venv/Scripts/python.exe serve.py           # Web + REST + MCP 同端口
```

打开 <http://127.0.0.1:8000/memo_login.html>。

种子账号（仅本地演示，可用环境变量覆盖）：

| 账号 | 密码 | 覆盖变量 | 用途 |
|------|------|---------|------|
| `demo` | `demo12345` | `SEED_ADMIN_USER` / `SEED_ADMIN_PASS` | 主账号，预置日程/标签/团队/积分 |
| `teammate01` | `teammate12345` | `SEED_TEAMMATE_PASS` | 协作演示，已在「运维小组」，用于验证邀请与日报往来 |

首次部署务必改口令：`SEED_ADMIN_PASS=... python seed.py`

三种启动方式：

| 命令 | 提供 | 适用 |
|------|------|------|
| `serve.py` | Web + REST + MCP(HTTP) 单端口 8000 | **推荐**，一个地址全搞定 |
| `app.py` | Web + REST（waitress） | 不需要 MCP 时 |
| `mcp_server.py` | MCP（stdio） | Claude Desktop / Cursor 等本地客户端 |

---

## 一、AI REST API

### 1. 获取 Token

```bash
curl -X POST http://127.0.0.1:8000/api/ai/token \
  -H "Content-Type: application/json" \
  -d '{"username":"${SEED_ADMIN_USER:-demo}","password":"${SEED_ADMIN_PASS:-demo12345}","name":"my-agent"}'
```

返回的 `data.token` 只出现一次（服务端只存 SHA-256 哈希）。之后所有请求携带：

```
Authorization: Bearer memo_xxxxxxxx
```

### 2. 端点清单

自描述端点：`GET /api/ai/schema` —— 模型可先读它，无需人肉喂文档。

| 方法 | 路径 | 说明 |
|------|------|------|
| POST | `/api/ai/event` | 新建。传 `text` 走自然语言，或传 `title`+`start`+`end` |
| GET | `/api/ai/events` | 列表，支持 `status/level/q/from/to/tag/archived/page/limit` |
| GET | `/api/ai/event/<id>` | 单条详情（含子任务、标签、剩余分钟） |
| PATCH | `/api/ai/event/<id>` | 局部更新任意字段 |
| POST | `/api/ai/event/<id>/complete` | 标记完成；重复日程自动生成下一条 |
| DELETE | `/api/ai/event/<id>` | 删除 |
| POST | `/api/ai/events/batch` | 批量建（≤50 条），逐条返回成败 |
| GET | `/api/ai/agenda` | 按天议程 `?date=&days=1~31` |
| GET | `/api/ai/brief` | 现状摘要（已算好统计，模型别自己重算） |
| GET | `/api/ai/calendar` | 月度打点 `?year=&month=` |
| GET | `/api/ai/tags` | 标签及引用量 |
| POST | `/api/ai/tag` | 新建标签 |
| DELETE | `/api/ai/tag/<id>` | 删除标签（连带摘除关联） |
| POST | `/api/ai/subtask/<id>/toggle` | 勾选子任务，返回进度 |
| POST | `/api/ai/parse` | **只解析不落库**，先预览理解是否正确 |
| GET | `/api/ai/tokens` | 列出我的 Token |
| DELETE | `/api/ai/token/<id>` | 吊销 Token |
| GET | `/api/ai/schema` | 自描述 |

### 3. 自然语言建日程

```bash
curl -X POST http://127.0.0.1:8000/api/ai/event \
  -H "Authorization: Bearer $T" -H "Content-Type: application/json" \
  -d '{"text":"明天下午3点 评审会 2小时 #项目A 紧急"}'
```

返回含 `source.matched`，明确告诉模型它识别到了哪些片段：

```json
{"ok":true,"data":{
  "id":21,"title":"评审会","startLocal":"2026-10-29T15:00","endLocal":"2026-10-29T17:00",
  "level":"high","state":"doing","minutesLeft":1020,
  "tags":[{"name":"项目A"}],
  "source":{"mode":"natural_language","input":"...","matched":["明天","下午3点","2小时","紧急","#项目A"]}
}}
```

支持的语法（规则式，确定性，不猜语义）：

| 类别 | 示例 |
|------|------|
| 日期 | 今天/明天/后天/大后天、本周五、下周三、下下周一、11月5日、本月30号、2026-12-01 |
| 时间 | 下午3点、15:30、晚上八点半、上午10点20分 |
| 时长 | 2小时、45分钟、半小时、一个半小时 |
| 优先级 | 紧急/重要/尽快 → high；不急/次要 → low |
| 重复 | 每天/每周/每月/工作日 |
| 标签 | `#项目A` |

**复杂重复规则**（`repeat` 字段，也可以直接传字符串）：

| 写法 | 含义 |
|------|------|
| `day` / `week` / `month` | 每天 / 每周 / 每月（月末收敛：1/31 → 2/28） |
| `week|interval:2` | 每两周 |
| `complex|monthly|2|2` | 每月第 2 个星期 2 |
| `complex|monthly|last|4` | 每月最后一个星期五 |

Web 表单里选中「复杂规则」会出现「第几个」「星期几」两个下拉，无需记语法。

**无法确定时间时返回 422 + 提示，绝不臆造**：

```json
{"ok":false,"error":{"code":"need_time","message":"..."},
 "data":{"parsed":{...},"hint":"可改为传 {title, start:'2026-10-29T15:00', durationMinutes:60}"}}
```

### 4. 幂等

写接口带 `Idempotency-Key` 头，同 key 重复请求返回首次结果（含 `idempotent: true`），
避免模型重试造成重复创建。

```bash
curl -X POST .../api/ai/event -H "Idempotency-Key: task-42" -d '{"title":"...","start":"..."}'
```

### 5. 响应约定

```json
{"ok": true,  "data": {...}}
{"ok": false, "error": {"code":"invalid_time","message":"end 不能早于 start"}}
```

时间统一 `YYYY-MM-DDTHH:MM`；分页 `page`(1 起) + `limit`(1~100)。

---

## 二、MCP Server

15 个工具，覆盖日程全生命周期，AI 客户端可直接调用。

### 1. 绑定身份

```bash
# 签发一个给 MCP 用的 Token
.venv/Scripts/python.exe mcp_server.py --issue-token ${SEED_ADMIN_USER:-demo} ${SEED_ADMIN_PASS:-demo12345}
```

### 2. stdio 模式（Claude Desktop / Cursor）

配置示例（`claude_desktop_config.json`）：

```json
{
  "mcpServers": {
    "calendar-memo": {
      "command": "E:/WorkBuddy/2026-10-28-20-44-01/.venv/Scripts/python.exe",
      "args": ["E:/WorkBuddy/2026-10-28-20-44-01/mcp_server.py"],
      "env": { "MEMO_TOKEN": "memo_你的token" }
    }
  }
}
```

### 3. HTTP 模式（同端口）

`serve.py` 已自动在 `http://127.0.0.1:8000/mcp` 暴露 Streamable HTTP。
鉴权**在请求头**携带 Token（支持多客户端共用同一服务、各自不同账号）：

```
Authorization: Bearer memo_你的token
```

单独运行：`python mcp_server.py --http --port 8765`

### 4. 工具清单

| 工具 | 用途 |
|------|------|
| `create_event` | 建日程（支持 `text` 自然语言；`repeat` 支持带参复杂规则） |
| `create_events_batch` | 批量建（≤50） |
| `update_event` | 局部更新 |
| `complete_event` | 完成/恢复，重复日程自动生成下一条 |
| `delete_event` | 删除 |
| `toggle_subtask` | 勾选子任务 + 进度 |
| `brief_status` | **现状摘要**（模型首选） |
| `list_events` | 列表过滤 |
| `get_event` | 单条详情 |
| `get_agenda` | 按天议程 |
| `parse_text` | 只解析不落库 |
| `list_tags` | 标签及用量 |
| `create_tag` | 建标签 |
| `get_calendar_month` | 月度打点 |
| `whoami` | 当前绑定账号与权限 |

服务端 `instructions` 已明确告诉模型：不确定意图先 `parse_text`，查现状优先 `brief_status`。

### 5. 验证

```bash
.venv/Scripts/python.exe tests_mcp.py <token>                                   # stdio 16 项
.venv/Scripts/python.exe tests_mcp_http.py http://127.0.0.1:8000/mcp <token>    # HTTP 7 项
```

---

## 三、技术栈

| 层 | 原站 | 本项目 |
|----|------|--------|
| 页面 | 单文件 HTML + 内联 CSS/JS | **逐字节复用**（CSS 969 行零差异，已同步原站 10-28 15:05 改版） |
| 农历库 | `/static/lunar.js` | 原文件复用（lunar-typescript 1.8.6, MIT） |
| 后端 | Flask REST `{code,data,msg}` | Flask 3.1，接口契约完全对齐 |
| 生产服务器 | — | waitress 多线程 + SQLite WAL |
| 数据 | 服务端数据库 | SQLite `data/memo.db` |
| AI 接入 | 无 | REST `/api/ai/*` + MCP (stdio / HTTP) |

### 双通道鉴权

同一套接口实现，`current_user()` 统一两种身份，前端零改动：

- **Cookie 会话** —— 浏览器页面用
- **Bearer Token** —— AI / 脚本 / MCP 用

---

## 四、目录

```
app.py              Web + REST（含 /api/ai/*、鉴权、密码策略）
serve.py            单端口统一服务（Web + REST + MCP HTTP）
mcp_server.py       MCP Server（stdio / HTTP，15 工具）
seed.py             初始化数据库与演示数据
tests_mcp.py        MCP stdio 端到端测试（真实协议）
tests_mcp_http.py   MCP HTTP 端到端测试
web/memo.html       主页面（与原站 CSS 零差异）
web/memo_login.html 登录/注册页
web/memo_reset.html 找回密码页
web/static/lunar.js 农历库
data/memo.db        运行时数据库（自动生成）
docs/ACCEPTANCE.md  1:1 还原验收报告
```

---

## 五、安全加固（相对原站新增）

| 项 | 实现 |
|----|------|
| Token 存储 | 只存 SHA-256 哈希，明文仅签发时返回一次 |
| Token 审计 | 记录 `last_used` / `use_count`，可随时吊销 |
| 密码策略 | ≥8 位、含字母+数字、拦常见弱口令、拦字符重复 |
| 登录锁定 | 同账号+IP 连续 5 次失败锁 15 分钟；**密码正确即解锁**，不把主人锁在门外 |
| 上传限制 | 8MB 请求上限；凭证图 30 天 TTL 自动清理 |
| 并发 | SQLite WAL + busy_timeout，避免读写互斥失败 |

---

## 六、与原站的差异

界面与交互无差异。仅以下外部依赖做了本地替代：

| # | 项 | 原站 | 本地版 |
|---|----|------|--------|
| 1 | 会员开通 | 打赏 + 人工审核 | 提交即生效（演示） |
| 2 | 会员文案 | 引导公众号 | 说明本地演示模式 |
| 3 | AI 日报/周报 | 外部大模型 | 内置模板 `local-summary-v1` |
| 4 | 数据存储 | 服务端数据库 | 本地 SQLite |
| 5 | 找回密码 | 邮件验证码 | 固定 `246810` |
| 6 | 访问埋点 | 上报真实访客 | 仅写本地表 |
| 7 | 页脚 | 原站备案号 | 本地复刻说明 |

---

## 七、重置数据

```bash
.venv/Scripts/python.exe seed.py --force
```

或删除 `data/memo.db`，下次启动自动重建。
