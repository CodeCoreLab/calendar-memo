# 代码审计报告 · 日历备忘录

- **审计日期**：2026-10-29
- **审计范围**：全量（11,591 行 / 38 个文件）
- **档位**：对抗档（>400 行，且命中 `auth` `token` `db` `upload` `api` 敏感标签）
- **审计方法**：`code-audit-and-fix` skill v4.0 — 三阶段 × 40 项语言规则 × 七维清单 × 三态判定
- **结论**：**发现 P0 级 5 项，已全部修复并验证**；P1 级 3 项已修 1 项；P2/P3 合计 12 项记录在案

---

## 一、结论摘要

项目整体质量**高于同类个人项目**：SQL 全部参数化、Token 只存哈希、口令用 PBKDF2、登录有防爆破、备份用在线 API、并发有锁保护、上传做双重校验。这些不是自动生成的代码能随手做对的。

但存在 **5 项 P0 缺陷**，其中 2 项会导致严重的实际危害（任何人可重置任意账号密码），3 项是准备公开到 GitHub 时的凭据泄露风险。**已全部修复。**

| 级别 | 数量 | 已修 | 说明 |
|------|------|------|------|
| 🔴 P0 | 5 | **5** | 2 个安全漏洞 + 3 个凭据泄露 |
| 🟡 P1 | 3 | 1 | 异常吞没、测试缺守护 |
| 🟢 P2 | 7 | 0 | 记录待办 |
| 🔵 P3 | 5 | 0 | 记录待办 |

**健康评分：6.5 / 10**（修复前 4.5）—— 架构清晰、安全意识到位，但演示代码与生产代码边界不清是主要失分项。

---

## 二、P0 阻断项（已全部修复）

### 🔴 P0-1 · 找回密码验证码是硬编码常量，任何人可重置任意账号

- **位置**：`app.py:940`（修复前）、`app.py:955`（修复前）
- **规则**：OWASP A07 认证失败 / 黄山版安全规约【强制】8（敏感操作防暴力）
- **标签**：`reliability` → 动词 `guard`

**证据（实测）**：

```
修复前 send_code 返回：{"demoCode": "246810", ...}   ← 直接回给调用方
修复前 reset_password：if code != (session.get("reset_code") or "246810")
                                                  ↑ 全新会话时 session 为空，兜底成常量
```

用 `flask.test_client()` 实测攻击链：

| 步骤 | 结果 |
|------|------|
| 全新会话（未登录、未调 send_code）直接 `POST /api/memo/reset_password {"email":"li@local.dev","code":"246810","password":"Hack12345"}` | **`code=0`，密码已重置** |
| 验证新密码能否登录 | `check_password_hash` → **`True`** |

**根因**：`or "246810"` 的兜底逻辑，让「没走 send_code」等价于「验证码正确」。这不是演示模式的取舍问题，是认证逻辑的空值缺陷。

**修复**：

```python
# send_code：验证码改为随机 + 与会话、邮箱、10 分钟三重绑定
code = f"{secrets.randbelow(1000000):06d}"

# reset_password：四项必须同时满足，缺一即拒
sent_code  = session.get("reset_code")
sent_email = session.get("reset_email")
sent_at    = session.get("reset_at") or 0
if (not sent_code or not sent_email or email != sent_email
        or time.time() - sent_at > RESET_CODE_TTL_SEC
        or not secrets.compare_digest(code, sent_code)):
    return err("验证码不正确或已过期")
```

DEMO 模式（`MEMO_DEMO_RESET=1`，默认开）仍把码回给前端便于离线演示，但**不再能用来重置别的账号**。

**验证（4 条攻击路径全部收敛）**：

| 场景 | 修复前 | 修复后 |
|------|--------|--------|
| 全新会话 + 猜 `246810` | ✅ 重置成功 | ❌ `验证码不正确或已过期` |
| A 会话拿到的码给 B 会话用 | ✅ 成功 | ❌ 拒绝 |
| 用 A 邮箱的码改 B 账号 | ✅ 成功 | ❌ 拒绝 |
| 正常流程（同会话、同邮箱、10 分钟内） | ✅ | ✅ **仍可用** |

---

### 🔴 P0-2 · `/api/visit` 无鉴权且不限长，可无限灌库

- **位置**：`app.py:986`
- **规则**：OWASP A04 不安全设计 / 黄山版【强制】4（用户输入有效性验证）
- **标签**：`reliability` → 动词 `guard`

**证据（实测）**：单个匿名请求写入 100,000 字符成功落库：

```
POST /api/visit {"page": "x"*100000, "title": "y"*100000}  → {"code": 0}
SELECT MAX(LENGTH(page)) FROM visits → 100000
```

`MAX_CONTENT_LENGTH` 是 8MB，只挡单次大小，挡不住「反复写 7MB」。

**修复**：字段截断 200 字符 + 单会话 60 秒限 20 条。

**验证**：

| 项 | 修复前 | 修复后 |
|----|--------|--------|
| 100KB 提交落库长度 | 100,000 | **200** |
| 连打 25 次新增条数 | 25 | **19**（限频生效） |

---

### 🔴 P0-3 · 文档内含**可用的真实 API Token**

- **位置**：`docs/AI_MCP.md:7`、`:18`、`:35`、`:63`、`:73`
- **标签**：`noise`（凭据泄露）→ 动词 `replace`

**证据（哈希比对确认非示例值）**：

```
文档内 token: memo_xxxx…（实际值已脱敏）
sha256 前缀:  f1df…（前 4 字节）
库内 api_tokens: (1, 'mcp-cli', 'f1df…', 'read,write')
                 ↑ 完全匹配
```

这不是示例占位符，是**当前库内有效、scope 为 read,write 的真实凭据**。公开即等于送出读写权限。

**修复**：全部替换为 `<your-token>` / `os.environ["MEMO_AI_TOKEN"]`，并附签发命令。

---

### 🔴 P0-4 · `seed.py` 内嵌真实账号口令

- **位置**：`seed.py:50`（`generate_password_hash("<原站口令>")`）、`seed.py:64`（`"<协作演示口令>"`）
- **标签**：`noise` → 动词 `parameterize`

**证据**：README 明确写「与原站同名同口令」，该口令同时是外部站点 `cpcheng.top` 的账号口令。公开仓库等于同时泄露本项目和外部站点的凭据。

**修复**：改为环境变量注入，默认值换成公开可用的演示口令：

```python
admin_user = os.environ.get("SEED_ADMIN_USER", "demo")
admin_pass = os.environ.get("SEED_ADMIN_PASS", "demo12345")
```

同步清理 `README.md`、`docs/ACCEPTANCE.md`、`docs/AI_MCP.md`、`mcp_server.py`、`tests_concurrency.py` 中的同一口令共 **9 处**。

**验证**：全仓 grep 原始口令/token/账号四项特征串 → **零命中**。

---

### 🔴 P0-5 · `data/memo.db` 含真实数据且无 `.gitignore`

- **位置**：项目根目录（无 `.gitignore`）
- **规则**：UNIV-01 敏感文件入库 / UNIV-02 .gitignore 缺失
- **标签**：`noise` → 动词 `add`

**证据**：`data/memo.db` 139KB，含 2 个真实账号（口令哈希）、6 条日程、1 个 API Token 哈希、1 条访问记录。`git add .` 即全部公开，且**进了 Git 历史后删文件也清不掉**。

**修复**：新建 `.gitignore`，排除 `data/`、`*.db`、`*.db-wal`、`*.db-shm`、`uploads/`、`.env`、`*.pem`、`*.key`、构建产物、`__pycache__` 等。

---

## 三、P1 强制项

### 🟡 P1-1 · `run_housekeeping` 异常静默吞没（**已修**）

- **位置**：`app.py:3665`
- **标签**：`reliability` → 动词 `guard`

**证据**：清理函数内部 `except sqlite3.Error: pass`，失败时 `stats` 显示 `tokens: 0`，日志会打印「过期Token 0」——**看不出是「没有过期」还是「查询失败」**。这是典型的「假零值」误报源。

**修复**：`pass` → 记录错误并置 `stats["error"]`，让日志能区分两种情形。

### 🟡 P1-2 · 核心业务逻辑缺单元测试守护

- **证据**：`tests_concurrency.py`(90) + `tests_mcp.py`(143) + `tests_mcp_http.py`(76) = 309 行，覆盖并发与 MCP 通道，但**认证、权限、备份恢复三条关键路径无测试**。
- **状态**：记录。本轮 P0 修复已用 `flask.test_client()` 做攻击向量验证，但未固化为回归测试文件。

### 🟡 P1-3 · 无 lockfile，依赖版本不可复现

- **证据**：`requirements.txt` 全部为 `>=` 下限（`Flask>=3.0`、`mcp>=2.0`），无 `requirements.lock`。他人 clone 后装的版本可能与本机不同，且 `mcp>=2.0` 跨大版本 API 可能变化。
- **状态**：记录。风险中，但本地单机场景影响有限。

---

## 四、P2 / P3（记录待办，不阻断）

| # | 级别 | 位置 | 问题 | 标签 |
|---|------|------|------|------|
| 1 | P2 | `app.py`（多处） | 无索引：`visits` 表按 `time` 清理、`events` 按 `user_id+start_time` 查询，数据量大后全表扫 | perf |
| 2 | P2 | `app.py:1536` | 上传图片存 `UPLOAD_DIR` 但无访问鉴权路由，靠文件名随机性保护 | reliability |
| 3 | P2 | `web/memo.html` | 145 处 `innerHTML`，抽查过的渲染点均有 `esc()` 包裹（共 16 处显式调用），但**未做全量审计**，新增渲染点易漏 | reliability |
| 4 | P2 | `serve.py:120` | 反代转发未设超时 | reliability |
| 5 | P2 | 全局 | 无结构化日志（无 trace_id），排障靠文本日志 | structure |
| 6 | P2 | `app.py` | `LOGIN_FAIL` 内存字典，进程重启即清零；多进程部署下不共享 | reliability |
| 7 | P2 | `memo.spec` / `memo-server.spec` | `except Exception as exc: print(...)`，构建脚本用 print 而非 log | noise |
| 8 | P3 | `app.py:471` 等 5 处 | `except Exception: pass` 于非关键路径（清缓存 hook、日志写入），可接受但建议统一改 `log.warning` | reliability |
| 9 | P3 | `memo.spec:22-24` | 模块级可变列表（PyInstaller spec 常规写法，非缺陷） | — |
| 10 | P3 | `tests_mcp.py:121,130` | 字典链式 `.get().get()`，测试代码可读性 | noise |
| 11 | P3 | 全仓 | 中文注释为主、标识符英文，风格一致，无问题 | — |
| 12 | P3 | `web/static/lunar.js` | 压缩后的第三方库，无法审计，建议记录来源与版本 | — |

---

## 五、已否定候选（查过且没问题，留档防重复上报）

| # | 候选 | 否定理由 | 验证方式 |
|---|------|---------|---------|
| 1 | **SQL 注入**（PY-14 命中 5 处：`app.py:1136/1388/3195`、`mcp_server.py:374`、`seed.py:41`） | 拼进 SQL 的是**列名白名单字面量**（`fields.append("done=?")`）、**占位符**（`"，".join("?"*n)`）、**硬编码表名元组**，值全部走 `params` 参数绑定。无用户输入进入 SQL 结构 | 逐行读 `fields` 的构造来源 |
| 2 | **路径穿越**（`app.py:824` 兜底路由 `send_from_directory(WEB_DIR, request.path)`） | Werkzeug `send_from_directory` 内部 `safe_join` 已防护 | 实测 6 种攻击路径：`/../app.py`、`/%2e%2e%2fapp.py`、`/..%2fapp.py`、`/static/../../app.py`、`/data/memo.db` **全部 404** |
| 3 | **备份下载路径穿越**（`/api/memo/backup/<name>`） | 有 `re.fullmatch(r"memo-backup-\d{8}-\d{6}(-[a-z-]+|-\d+)?.db", name)` 白名单，且 `os.path.basename` 双重防护 | 读代码 |
| 4 | **弱哈希**（PY-15） | `hashlib.sha256` 仅用于 **Token 哈希**（非口令），口令用 `werkzeug.generate_password_hash`（PBKDF2-SHA256 加盐） | 读 `app.py:477` 与 `:24` |
| 5 | **硬编码 IP**（UNIV-04，12 处） | 全部是 `127.0.0.1` 回环地址，本地单机服务必需，非内网 IP 泄露 | 逐条核对 |
| 6 | **登录可爆破** | 已有 `LOGIN_MAX_FAIL=5` + `LOGIN_LOCK_SEC=15min` + `threading.Lock` 保护字典 | 读 `app.py:355-380` |
| 7 | **上传任意文件** | 扩展名白名单 + `is_image_magic()` 校验真实文件头 + 大小上限，文件名服务端生成（`proof_{uid}_{token_hex}{ext}`） | 读 `app.py:1529-1540` |
| 8 | **`Child::kill()` 杀不掉进程树**（Tauri） | 已用 `taskkill /T /F` 处理，实测强杀后 6 秒 sidecar 自动退出 | 前轮已验证 |

---

## 六、量化评估

### DICE 评分（Top 3）

| 问题 | D | I | C | E | DICE |
|------|---|---|---|---|------|
| P0-1 验证码重置 | 2 | 5 | 4 | 5 | 6+20+8+5 = **39** |
| P0-3/4 凭据泄露 | 1 | 5 | 3 | 5 | 3+20+6+5 = **34** |
| P0-5 数据入库 | 1 | 4 | 3 | 5 | 3+16+6+5 = **30** |

### ROI

以「凭据泄露」为例：修复耗时约 30 分钟（本次实测），若不修，公开后需轮换全部凭据 + 通知下游使用者 + 可能的账号损失，保守估 8 小时以上。
`ROI = 8×12 / 0.5 - 1 = 191` → **远超 10，Sprint 优先**。

### Bus Factor

**= 1**。全部代码出自同一轮 AI 协作 + 单人定稿，无第二人 review 记录。单人项目属正常，但若转为团队维护需立即补 ADR 与结对。

### T-shirt 估算（剩余待办）

| 项 | 尺寸 | 说明 |
|---|------|------|
| 补认证/权限/备份回归测试 | **M** | 1-2 天，需设计测试夹具 |
| 加 lockfile | **S** | <2 小时，`pip freeze` 产出 |
| 索引优化（P2-1） | **S** | <4 小时，3 条 CREATE INDEX |
| `innerHTML` 全量审计（P2-3） | **M** | 1-2 天，145 处需逐类核对 |
| 结构化日志 | **M** | 1-2 天，跨 5 个文件 |

---

## 七、复核清单（修改后必测）

### 必测（P0 链路，100% 通过）

- [x] 登录成功 / 失败（含锁定期）
- [x] 找回密码四路径（正常 / 猜固定码 / 跨会话重放 / 邮箱错配）
- [x] 页面三通道（memo / login / reset）返回 200
- [x] REST 主流程（统计 / 建日程 / 备份 / visit）
- [x] MCP 无 Token 返回 401

### 建议测

- [ ] 备份恢复（上传 → 数据还原）
- [ ] 并发双 Token 隔离（`tests_concurrency.py`）
- [ ] MCP 15 工具全量（`tests_mcp.py`）

### 抽测

- [ ] 会员套餐四档开通
- [ ] 团队协作邀请

---

## 八、审计局限（必须声明）

1. **W3 Spec 忠实轴未审**：无需求文档/验收标准，无法判断「实现是否忠实于需求」。README 提到「与原站像素级一致」，但原站不可访问，无法比对。所需最小输入：原站截图或验收清单。
2. **`web/static/lunar.js` 未审**：压缩产物，无法逐行审计。建议记录来源与版本。
3. **未做运行时模糊测试**：仅针对已知攻击向量做定向验证。
4. **未审计 `mcp>=2.0` 的传递依赖**：无 SCA 扫描工具，依赖漏洞状态未知。
5. **发现者与验证者分离的执行状态**：本轮 P0 候选的独立证伪复核派了子代理，但运行超时被取消（6分15秒）；其有待验证的关键结论（候选 1、2、3）由主流程用 `flask.test_client()` **实际攻击验证**，证据强度高于静态复核。

---

_报告生成：`code-audit-and-fix` skill v4.0 · 三态判定：确认 5 / 已否定 8 / 待验证 0_

---

## 九、剩余待办处理结果（2026-10-29 第二轮，同一日）

首轮报告的 8 项待办已全部处理。**过程中新发现 3 个真缺陷**，均已修复。

### 9.1 新增/修正的事实

| # | 原报告说法 | 实际情况 |
|---|-----------|---------|
| 1 | 「加 3 条索引」 | 报告中提的 `api_tokens(token_hash)` 与 `idempotency(key)` **本来就有隐式索引**（UNIQUE / PRIMARY KEY），那两条是冗余的。真正的问题是 **7 处 `substr(start_time,1,N)` 查询全部索引失效** —— 该列上没有可用的索引，加了也白加 |
| 2 | 「`serve.py:120` 无超时」 | **有 `timeout=300`**。真问题是 300 秒对普通调用过长 + `visits` **根本没有清理函数**（只增不删） |
| 3 | 「P2-3 innerHTML 未全量审计」 | 已全量审计：141 个赋值点、33 处拼接、19 处显式 `esc()`、14 处有理由注释，**0 个真 XSS** |

### 9.2 本轮新发现的 3 个真缺陷（都已修）

#### 🟡 P1-4 · 登录锁定期是攻击者的无限尝试窗口

- **位置**：`app.py` `login_locked()` / `api_login()`
- **证据（实测）**：先打 5 次错误进锁，之后**连续 10 次错误尝试仍在逐个校验密码**，
  且第 11 次用正确密码**直接登录成功**。

```
锁定后再试 10 次: 最后 msg=密码错误次数过多，请 15 分钟后再试
锁定期间用正确密码: code=0 登录成功
```

- **根因**：「锁定期内正确密码放行」是刻意的防误锁设计（不能让攻击者乱试把主人锁在门外），
  但配套的「锁定期内失败尝试计数」缺失 —— `login_fail()` 只在非锁定分支被调用，
  进了锁定分支就直接返回，配额永远不增长。**于是锁定期从「惩罚」变成了「免限速的黄金窗口」**。
- **修法**：两级锁
  - 软锁：仍校验密码，正确放行（保住防误锁）→ 失败也要 `login_fail()`
  - 硬锁：锁定内错误尝试超过 `LOGIN_LOCK_EXTRA=3` → **不再校验密码**，直接拒绝

| 场景 | 修复前 | 修复后 |
|------|--------|--------|
| 锁定期内连试 10 次 | 全部逐个校验 | 超过 3 次后直接拒绝，不碰密码 |
| 锁定期内本人正确密码 | 放行 | 放行（额度内） |
| 锁定期内额度耗尽后正确密码 | 放行 | **拒绝** |

#### 🟡 P1-5 · `urllib.request` 的 timeout 不接受元组（本轮自己引入又修掉）

- **位置**：`serve.py`（给 MCP 转发分级超时时）
- **证据**：`opener.open(url, timeout=(5.0, 60.0))` → `TypeError: 'tuple' object cannot be interpreted as an integer`，
  被 `except Exception` 兜成 **MCP 全链路 502**。

这是我在修「超时」时**自己造的缺陷**，记录在此因为它是典型陷阱：
`(connect, read)` 元组是 **httpx / requests** 的用法，**标准库 urllib 只吃单值**。

- **修法**：改单值，按场景取上界（普通 60s / 流式 300s）
- **验证**：MCP 流式 200、无 Token 401、普通 JSON 406（协议要求 Accept 含 `text/event-stream`，属正确行为）

#### 🟢 P2 · `visits` 表只增不删

- **证据**：`INSERT INTO visits` 有 1 处，**没有任何 DELETE**。长期每日访问会无限增长。
- **修法**：加 `cleanup_visits()`（TTL 180 天）并接入 `run_housekeeping()`，配 `idx_visits_time` 索引。

### 9.3 8 项待办的处理

| # | 待办 | 处理方式 | 验证 |
|---|------|---------|------|
| 1 | 索引优化 | 7 处 `substr` 改半开区间范围查询（`start_time >= lo AND < hi`）+ 4 个索引 | 查询计划确认走 `idx_events_user_arch`；新旧 SQL 结果逐日/逐月比对**完全等价**，跨年边界正确 |
| 2 | 加 lockfile | `requirements.lock`（36 项运行时）+ `requirements-dev.lock`（打包/运维） | 干净 venv 装后版本完全一致，服务可跑（页面 200、登录 code=0） |
| 3 | 认证/权限测试 | 新建 `tests_security.py`（34 项） | **34/34 PASS** |
| 4 | 备份恢复测试 | 新建 `tests_backup.py`（27 项） | **27/27 PASS** |
| 5 | innerHTML 审计 | 全量核对 141 处；1 处属性拼接加白名单；新增 `tests_xss_static.py` 守护 | **6/6 PASS**；注入假漏洞验证检查器能精准抓到 |
| 6 | MCP 转发超时 | 分级：普通 60s / 流式 300s | MCP 流式 200、无 Token 401 |
| 7 | 静默异常吞没 | `clear_token_cache` hook 失败留痕；浏览器打开失败留痕；两处刻意静默加注理由 | 日志实测可见 |
| 8 | 结构化日志 | 自研 `log_event()`（不引三方库）：JSON 行 + `trace_id` + 慢请求/非 2xx 才记 | 实测输出 `{"ts","level","event","trace_id","username","ip"}`；响应头带 `X-Request-Id` |

### 9.4 三个测试脚本的判定口径（防误判）

| 脚本 | 关键判据 | 为什么这样定 |
|------|---------|-------------|
| `tests_security.py` | 断言「攻击失败」而非「正常通过」；每用例独立 client（cookie 不串） | 跨会话攻击只有在 session 隔离时才测得出来 |
| `tests_security.py` | 锁定用例间 `LOGIN_FAIL.clear()` | `LOGIN_FAIL` 是进程级状态，不清会把配额带到下个用例，产生假失败 |
| `tests_xss_static.py` | 判据是 `.innerHTML =`（**带等号**） | 只写 `.innerHTML` 会命中 HTML 里的 `id="hlFoot"`，把静态标签当成渲染点 |
| `tests_xss_static.py` | 多行语句只在「上一行以 `+ ? : (` 结尾」时才续行 | 用括号配平判句界会把整段函数吞成一条语句，产生大片误报 |
| `tests_xss_static.py` | 无 `esc()` 时必须紧邻注释写明理由 | 强制作者写「为什么安全」，比强制写 `esc()` 更能防漏 |

### 9.5 本轮验证总表

| 套件 | 项数 | 结果 |
|------|------|------|
| `tests_security.py` | 34 | ✅ 全通过 |
| `tests_backup.py` | 27 | ✅ 全通过 |
| `tests_xss_static.py` | 6 | ✅ 全通过 |
| 启动级（页面/REST/MCP） | 4 | ✅ 200 / code=0 / 401 / 200（流式） |

### 9.6 仍未处理（明确记录，不隐瞒）

| 项 | 原因 |
|----|------|
| 无 CI 流水线 | 本地单机项目，三套脚本需手动跑。若要自动化，加 `.github/workflows` 跑 `tests_*.py` 即可（需先解决 `tests_mcp.py` 依赖外部 token 的问题） |
| `web/static/lunar.js` 未审 | 压缩产物，无法逐行审计。建议记录来源与版本 |
| 无 SCA 依赖漏洞扫描 | 未接工具，`requirements.lock` 中 36 个包的 CVE 状态未知 |
| Bus Factor = 1 | 单人项目属性，转团队维护前需补 ADR |
