# 架构选型与打包方案（ADR）

- **状态**：已决策并已实测通过
- **日期**：2026-10-29
- **置信度**：高（核心链路已跑通，非纸面推演）

---

## 一、结论

**采用「HTML 界面 + Python 后端 + PyInstaller 打包」**，产出单一压缩包，用户解压双击即用，**无需安装 Python 或任何依赖**。

实测产物：`dist/日历备忘录-Windows-x64.zip`（**23.0 MB**，解压后 52.3 MB）

三条关键路径已实际验证（详见第五节）：

| 验证项 | 结果 |
|--------|------|
| 打包后能否启动 | 通过，Web + REST + MCP 全部正常 |
| 数据库是否落在 exe 旁 | 通过，`data/memo.db` 在 exe 同级目录 |
| 换路径迁移后数据是否存活 | 通过，杀进程重启后账号与日程全部幸存 |

---

## 二、为什么排除 Rust / Go / Qt6

### 2.1 先厘清一个概念问题

「HTML 架构」与「Rust / Go / Qt6」**不是同一层级的东西**：

- Rust、Go、Python 是**后端语言**
- Qt6 是**原生 GUI 工具库**
- HTML 是**界面层**

真正要选的是「界面用什么渲染」+「后端用什么语言」。本项目界面已经是 HTML，所以问题变成：**是否值得为了换后端语言而重写界面？**

### 2.2 候选对比

| 方案 | 体积 | 后端重写 | 界面重写 | 可行性 | 结论 |
|------|------|---------|---------|--------|------|
| **HTML + Python 打包** | 52MB / 压缩 23MB | 0 | 0 | 本机工具链已具备 | **选中** |
| Rust + Tauri | 2–10MB | 3675 行 | 0（复用 HTML） | 本机**未装 Rust 工具链** | 排除 |
| Go + 内嵌资源 | ~15MB | 3675 行 | 0（复用 HTML） | 本机**未装 Go** | 排除 |
| Qt6 + PySide | 60MB+ | 0 | **4829 行全部重画** | 需装 Qt 依赖 | 排除 |
| PHP | — | 3675 行 | — | 桌面分发需捆绑 Web 服务器，**比 Python 更重** | 排除 |

### 2.3 关键理由

**① 4829 行前端是已验证资产，不是普通代码**

这份前端与原站 CSS **零差异**、骨架像素 5 个区域全部 0.00%。这是经过 4 轮迭代的成果。换 Qt6 意味着把这部分全部作废并重画——**用确定性的损失换不确定的收益**。

**② 3675 行后端已修掉 10 个缺陷**

包括 MCP 并发串号（16 次中 6 次串号）、幂等键跨用户冲突这两个严重问题。重写等于把这些坑重新踩一遍。

**③ 体积不是本项目的痛点**

这是单机日历工具，不是海量下载的消费级 App。52MB 与 10MB 的差距，用户感知几乎为零；而重写 3675 行已验证代码的风险是不可逆的。

**④ 本机工具链现状（实测）**

```
go version   → command not found
rustc        → command not found
gcc          → command not found
```

选 Rust/Go 需先装工具链，而 Nuitka（Python 编译方案）在 **Windows + Python 3.13 上不兼容自带 MinGW64**，需 MSVC。PyInstaller **不需要编译器**，这是它今天就能出包的决定性优势。

---

## 三、体积说明

| 项 | 大小 |
|----|------|
| `dist/日历备忘录/` 解压后 | 52.3 MB |
| `_internal/`（运行库） | 44.7 MB |
| `web/`（前端资源） | 0.68 MB |
| **zip 压缩包** | **23.0 MB** |

其中 44.7MB 主要是 CPython 运行时 + Flask/waitress/uvicorn/mcp 及其依赖。这是「用户不需要装 Python」的代价，属于合理交换。

> **修正（2026-10-29）**：上一版此处写「切到 Tauri 外壳体积可降到 ~10MB」，这个说法不准确。
> 详见第九节 —— **保留 Python 后端作为 sidecar 时，体积几乎不降**（Python 运行时照样要带）；
> 要真正到 10MB，必须把后端也改写成 Rust，那是重写而非换壳。

---

## 四、交付形态

```
日历备忘录-Windows-x64.zip   (23 MB)
└── 日历备忘录/
    ├── 日历备忘录.exe        ← 双击即运行
    ├── _internal/            ← 运行库（用户不需关心）
    │   └── web/              ← 前端页面
    └── data/                 ← 首次运行自动创建
        ├── memo.db           ← SQLite，全部数据在这里
        └── uploads/          ← 打赏凭证图
```

**用户操作**：解压 → 双击 `日历备忘录.exe` → 浏览器自动打开登录页。

**备份/迁移**：直接复制 `data` 文件夹即可，换机器/换 U 盘都能带走。

**关闭服务**：关闭控制台窗口，或任务管理器结束进程。

---

## 五、实测证据

### 5.1 打包产物运行

```
$ ./日历备忘录.exe --port 8099 --no-browser
StreamableHTTP session manager started
Serving on http://127.0.0.1:8099

/memo.html             200
/memo_login.html       200
/static/lunar.js       200
```

### 5.2 数据落点（关键风险点）

打包后 `__file__` 会指向临时解压目录 `_MEIPASS`。若沿用原始路径逻辑，**SQLite 写进临时目录，程序退出时随临时目录一起删除，用户数据全部丢失**。

已改造为双路径函数（`app.py`）：

```python
def resource_dir():   # 只读资源 → _MEIPASS
def data_dir():       # 可写数据 → exe 同级 data/（可用 MEMO_DATA_DIR 覆盖）
```

实测结果：

```
data 目录: dist/日历备忘录\data
DB:        dist/日历备忘录\data\memo.db
```

**数据正确落在 exe 旁，不在临时目录。**

### 5.3 重启后数据存活

```
=== 重启后：数据是否幸存 ===
  旧账号登录 200 code=0 msg=登录成功
  日程总数 1  标题=['打包版验证']
```

杀掉进程再启动，账号密码与日程全部幸存。

### 5.4 换全新路径迁移

将 zip 解压到 `E:/usb_test/`（全新位置，无历史数据），直接运行：

```
页面 200
注册  200 code=0
登录  200 code=0
建日程 code=0
统计  {'total': 1, 'doing': 1, ...}
DB 在 exe 同级 data 下: True
```

**可移植性成立。**

### 5.5 MCP 链路

```
MCP 无 Token → 401   （鉴权生效）
MCP 带 Token → 200
```

---

## 六、已知限制

| 项 | 说明 | 影响 |
|----|------|------|
| **Windows 版本差异** | PyInstaller **不是交叉编译器**，Windows 包只能在 Windows 跑 | 要支持 mac/Linux 需在对应平台各打一次 |
| **杀软误报** | 未签名的可执行文件可能被拦截 | 正式对外分发建议买代码签名证书 |
| **中文 exe 名** | 极端环境下路径可能有编码问题 | 已验证本机正常；如需保守可改名 `MemoCalendar.exe` |
| **后端代码可见** | PyInstaller 产物可被反编译出 .pyc | 内部工具可接受；商业分发建议改 Nuitka（需 MSVC） |
| **首次启动较慢** | 3–5 秒（加载 44MB 运行库） | 可接受；后续启动更快 |
| **无自动更新** | 换新版本需手动替换文件 | 如需可后续加 |

---

## 七、如何重新打包

```bash
# 一次性准备
.venv/Scripts/pip install -r requirements.txt pyinstaller

# 打包
.venv/Scripts/python.exe -m PyInstaller --clean --noconfirm memo.spec

# 结果
dist/日历备忘录-Windows-x64.zip
```

或双击 `build.bat` 自动完成。

**新增 Python 依赖时**：需在 `memo.spec` 的 `collect_all` 列表里补上该包，否则运行时会 ImportError。

---

## 八、被否方案记录

| 方案 | 未采纳原因 |
|------|-----------|
| Rust + Tauri | 本机无 Rust 工具链；后端 3675 行需重写；并发/幂等缺陷需重新踩坑。**保留为后续体积优化路径**（只换外壳，界面不动） |
| Go | 本机无 Go；后端与 MCP 全部重写；SQLite 需 CGO 或纯 Go 驱动，MCP SDK 生态不如 Python 成熟 |
| Qt6 + PySide | 4829 行前端全部作废；CSS 零差异成果丢失；体积反而更大（60MB+） |
| PHP | 桌面分发需捆绑 Web 服务器（PHP-FPM/内置服务器），比 Python 更重；且现有 21 张表逻辑需重写 |
| Electron | 打包 100MB+，且需引入 Node 生态，与现有 Python 后端重复 |
| Nuitka | 无 MSVC 编译器时 Windows + Python 3.13 不可用（自带 MinGW64 不兼容 3.13）。**有 MSVC 后可重新评估**，收益是启动更快 + 代码保护 |
| onefile 模式 | 每次启动解压 15MB 到 Temp，杀软敏感（易误报为木马），启动更慢。**选 onedir** |

---

_本文档依据实测，非推演。所有「通过」均为实际运行结果。_

---

## 九、Tauri 换壳的真实取舍

### 9.1 先修正一个错误说法

上一版本节写「换 Tauri 外壳可降到 ~10MB」。**这不成立**，取决于后端怎么处理：

| 方案 | 体积 | 说明 |
|------|------|------|
| Tauri 外壳 + **Python 后端作 sidecar** | **约 40–52 MB** | Python 运行时（CPython + Flask/MCP 依赖）照样要带，只省掉浏览器窗口这个"外壳"，而外壳本来就不是体积大头 |
| Tauri 外壳 + **Rust 重写后端** | 2–10 MB | 必须把 3675 行 Python 改写成 Rust，属重写，非换壳 |

**体积大头从来不是外壳，是 CPython 运行时。** 所以「换壳省体积」是个伪命题 —— 换壳省的是窗口，重写才省体积。

### 9.2 那 Tauri 除了体积还有什么真实好处

按对本项目的实际价值排序：

| 优势 | 真实价值 | 代价 |
|------|---------|------|
| **无控制台黑窗** | ★★★★☆ 当前双击会弹一个 cmd 窗口，观感粗糙。Tauri 是真正的 GUI 窗口，可加托盘图标/菜单/最小化到托盘 | 需要 Rust 工具链 + 前端改动 |
| **系统级能力** | ★★★★☆ 原生文件对话框（导出/导入数据库）、开机自启、全局快捷键、系统通知、拖拽文件进窗口 | 需逐个接 Tauri 插件 |
| **单实例控制** | ★★★☆☆ 用 Tauri 单实例插件管住"用户双击两次开两个库"的并发写风险 | 现在靠 waitress 兜底，但没有单实例锁 |
| **安全边界** | ★★★☆☆ Tauri v2 的 capability 白名单 + CSP，比"监听 localhost 端口"收紧得多。现在任何本机进程都能访问 `127.0.0.1:8000` | 需重新设计前后端通信（IPC 替代 HTTP） |
| **代码保护** | ★★★☆☆ Rust 编译产物比 PyInstaller 的 .pyc 难反编译 | 若后端仍用 Python sidecar，这一点**不成立** |
| **启动速度** | ★★☆☆☆ 冷启动 50ms vs 现在 3–5 秒 | 值得注意的是：如果后端是 Python sidecar，sidecar 启动照样慢，**只有外壳快没用** |
| **自动更新** | ★★☆☆☆ Tauri 有内置 updater | 现在手动替换文件 |
| **跨 macOS** | ★★☆☆☆ WKWebView 系统自带，不像 Windows 需 WebView2 | 但要重打包 + 用户量未知 |

### 9.3 对本项目的诚实结论

**收益最高的是「无黑窗 + 托盘」，其次是「原生文件对话框」**，其余要么代价高要么不成立。

但这里有个更便宜的替代方案，能拿到前两项的大部分收益，且**不需要 Rust**：

| 做法 | 成本 | 拿到什么 |
|------|------|---------|
| `pythonw.exe` + 轻量托盘库（pystray） | 约 1 小时 | **去掉黑窗**、加托盘图标、右键退出 |
| 加单实例锁（已有 socket 逻辑，几十行） | 约 30 分钟 | 防双开写坏库 |
| 加"导出/导入数据库"按钮（走 HTML 文件选择） | 约 2 小时 | 替代原生文件对话框的 80% 用途 |

合计约半天，零新工具链，风险极低。**这是当前性价比最高的路径。**

什么时候才值得上 Tauri：当需要**托盘常驻 + 开机自启 + 系统通知**这套桌面级体验，且准备接受 Rust 工具链的学习与维护成本时。届时体积也不是决策理由 —— 真正的理由是"它像一个桌面软件"。

### 9.4 WebView2 依赖（实测）

本机已装：`C:/Program Files (x86)\Microsoft\EdgeWebView\Application\150.0.4078.65`

- Windows 10/11 绝大多数机器自带（Edge 附带），但**不是 100% 保证**
- 若目标机器缺失，Tauri 安装包需附带 WebView2 Runtime（+约 2MB bootstrap，或让用户装）
- **这是选 Tauri 时新增的一个外部依赖** —— 当前 PyInstaller 方案没有任何这类依赖

### 9.5 一个实测教训（与体积优化相关）

本轮尝试剔除 `cryptography`（占 9.4MB 代码 + 7.6MB libcrypto，合计约 17MB）：

- 依据：源码从未 import cryptography，它由 `mcp → pyjwt[crypto]` 引入，而本项目用自签 Token 不走 JWT
- 结果：体积确实从 52.3MB 降到 **42.3MB**（省 10MB）
- **但 MCP 挂了**：启动只打印一行 `[warn] MCP 启动失败：No module named 'cryptography'`，
  网页与 REST 全部正常 —— 属**静默降级**
- 处理：**已回滚**，并在 `memo.spec` 里写明"cryptography 不能剔除"及原因

> 教训：PyInstaller 的 `excludes` 只看"源码有没有 import"是不够的，
> 依赖方的导入链才是判据。且验证必须覆盖**全部功能面**，否则静默降级测不出来。

### 9.6 轻量路径已实施（2026-10-29，先于 Tauri）

不引入 Rust，已拿到「无黑窗 + 托盘 + 单实例 + 备份」四项，成本约半天：

| 能力 | 实现 | 验证 |
|------|------|------|
| **无控制台黑窗** | `memo.spec` 改 `console=False`；`daemonize_consoleless()` 把 stdout 重定向到 `data/run.log` | 打包版启动无窗口，run.log 记录完整 |
| **系统托盘** | pystray + Pillow 现画图标（不依赖外部图片）；菜单：打开界面 / 立即备份 / 数据目录 / 退出 | 进程存活，服务 200 |
| **单实例锁** | 绑定固定回环端口 47700 做互斥；后来者从锁端口读到工作端口并直接打开界面 | 第二实例退出码 0 并打开 8000 界面 |
| **数据备份/恢复** | `POST /api/memo/backup`、`GET /api/memo/backup/<name>`、`POST /api/memo/restore` | 备份下载 139264 字节、22 表 2 用户 6 日程完整；恢复后数据正确还原 |
| **原生文件对话框替代** | 备份生成文件 + 浏览器下载；恢复走 multipart 上传 | 非法文件（非 SQLite）与错误扩展名均被拒 |

**体积代价**：zip 23.0MB → **30.1MB**（+7.1MB，Pillow 与托盘库）。解压约 59MB。

**本轮修掉的缺陷**：
1. 保护副本与备份重名（同一秒生成会覆盖，恢复场景等于丢掉安全副本）→ 加 `before-restore` 标记 + 序号去重
2. `console=False` 下 `sys.stdout is None` 会让所有 `print` 抛 AttributeError → 启动最早期重定向
3. Pillow 原在 excludes 里，会导致托盘起不来 → 移出并加 hiddenimports

---

## 十、Tauri 落地实录（2026-10-29，跳板机编译 + 本机测试）

### 10.1 为什么在跳板机编译

本机两条硬阻碍（实测）：

| 阻碍 | 证据 |
|------|------|
| 无 C 编译器 | `gcc` / `cl` / `link.exe` 全盘搜索无结果；VS Build Tools 只装了 Installer，无 C++ 工作负载与 Windows SDK |
| Microsoft 域名被代理屏蔽 | `curl https://aka.ms` 与 `download.microsoft.com` 均返回 000；而 `static.rust-lang.org`、GitHub、npm 均 200 |

跳板机条件齐备：Ubuntu 24.04 / rustc **1.99.1** / MinGW-w64 **13.2** /
**`x86_64-pc-windows-gnu` target 已预装** / cargo 已配 **USTC 镜像** / 116G 可用。

### 10.2 架构决策：Tauri 只做外壳，Python 继续管业务

```
memo-desktop.exe (Tauri, 2.7MB)
   └─ 拉起 → memo-server.exe (PyInstaller onefile, 24MB)
                └─ 提供 http://127.0.0.1:<随机端口>
                     └─ Tauri 窗口直接加载该地址
```

**不重写 3675 行 Python** 的理由：那部分含 21 张表、15 个 MCP 工具、86 个路由，
且已修掉 10 个缺陷（含并发串号、幂等冲突）。重写风险不可逆。

代价诚实说明：**体积没降**。压缩包 24.2MB（原 PyInstaller 版 30.1MB，
还小了 6MB —— 因为 sidecar 剔掉了 pystray/PIL）。Tauri 换来的不是体积，
是**原生窗口、无黑窗、进程树管理、后续可加托盘/通知/自动更新的骨架**。

### 10.3 交叉编译实证链路

| 步骤 | 结果 |
|------|------|
| 纯 Rust 交叉编译 | ✅ 1.2MB PE32+ |
| 带外部依赖（serde_json）| ✅ 1.4MB PE32+ |
| Tauri 全依赖（tao/webview2-com/window-vibrancy）| ✅ 编译通过 |
| 带 build.rs（tauri-build）| ✅ 需要 `icons/icon.ico`，缺失即报错 |
| strip+lto+opt-level=z | 21MB → **2.7MB** |
| 本机运行 | ✅ 窗口标题 `日历备忘录 · 127.0.0.1:<port>` |

**必须随包附带 `WebView2Loader.dll`（160KB）**。缺它则启动即
`0xC0000135 STATUS_DLL_NOT_FOUND`，无任何提示 —— 这是 GNU target 的坑。

### 10.4 本轮踩到并修掉的 4 个真实缺陷

| # | 缺陷 | 现象 | 修法 |
|---|------|------|------|
| 1 | Tauri 不支持 `data:` URL | 失败分支 panic：`invalid window url: data URLs are not supported without the webview-data-url feature` | 改写成 HTML 文件，用 `file://` 打开 |
| 2 | windowed 模式 stdout 不可用 | sidecar `print(MEMO_PORT=...)` 抛 `OSError [Errno 22] Invalid argument`，端口永远传不到父进程 | **改用端口文件** `--port-file`，原子写（tmp + os.replace） |
| 3 | `Child::kill()` 杀不掉进程树 | PyInstaller onefile 会 fork 子进程，主程序退出后**两个 memo-server.exe 残留**占端口 | Windows 用 `taskkill /PID <pid> /T /F` 杀整棵树 |
| 4 | 主程序被强杀后无清理 | 任务管理器结束 → Rust 退出回调不执行 → 孤儿进程 | Python 侧加**父进程守护**：每 2 秒 `OpenProcess`+`GetExitCodeProcess` 探测父 PID，失效即 `os._exit(0)` |

### 10.5 退出清理双路径实测

| 场景 | 手法 | 结果 |
|------|------|------|
| 温和关闭 | `terminate()` 主程序 | ✅ 无残留（kill_tree 生效） |
| 强制杀死 | `kill -9` 主程序 | ✅ **6 秒后** sidecar 自动退出（父进程守护生效） |

> 这两条必须都测：只测温和关闭会漏掉「任务管理器强杀」这个最常见的用户行为。

### 10.6 端到端功能验证

窗口标题 `日历备忘录 · 127.0.0.1:49420`，进程链
`memo-desktop.exe → memo-server.exe → memo-server.exe`（onefile 派生），
端口文件 `49420`，登录页 200。经该端口实测：

| 操作 | 结果 |
|------|------|
| 注册 | `code=0` |
| 登录 | `code=0` |
| 建日程 | `code=0` id=1 |
| 统计 | `{total:1, doing:1}` |
| 备份 | `code=0` `memo-backup-20261029-142710.db` |

### 10.7 交付物

```
dist/日历备忘录-Tauri版-Windows-x64.zip   24.2MB（解压 26.1MB）
└── tauri_app/
    ├── memo-desktop.exe      2.7MB  主程序（原生窗口）
    ├── memo-server.exe       24MB   业务服务（自动随主程序启停）
    ├── WebView2Loader.dll    160KB  窗口组件（必需）
    └── 使用说明.txt
```

源码在 `tauri/`（`src/main.rs` 190 行 + `Cargo.toml` + `tauri.conf.json` + 图标）。

### 10.8 当前限制

| 项 | 说明 |
|----|------|
| **仅 Windows** | 交叉编译目标是 windows-gnu；mac 需在 mac 上编 |
| **体积未降** | 仍带完整 Python 运行时。要真正变小必须 Rust 重写后端 |
| **WebView2 依赖** | Win10/11 一般自带，缺失时需装 Runtime |
| **未做 NSIS 安装包** | `tauri.conf.json` 已配 `targets:["nsis"]`，但交叉编译打 NSIS 需额外工具，当前交付为免安装 zip |
| **未加托盘** | 骨架已留好位置，需要时加 `tauri-plugin-*` 即可 |
| 杀软误报 | exe 未签名，同前 |

### 10.9 重新编译方法

```bash
# 在跳板机（或任意有 Rust+MinGW 的 Linux）
cd tauri
cargo build --release --target x86_64-pc-windows-gnu
# 产物：target/x86_64-pc-windows-gnu/release/memo-desktop.exe
# 同时需从同目录取 WebView2Loader.dll 一起分发

# Python sidecar（本机 Windows 或跳板机均可）
python -m PyInstaller --clean --noconfirm memo-server.spec
```

---

## 十一、托盘 + NSIS 安装包（2026-10-29 第二轮）

### 11.1 托盘

Tauri 2.x 托盘是**内置 feature**，不需要额外 plugin：

```toml
tauri = { version = "2", features = ["tray-icon", "image-png"] }
```

菜单：打开界面 / 打开数据目录 / 退出；左键单击 = 唤回窗口。
图标复用 `app.default_window_icon()`，不额外打包 PNG。

**体积代价**：2.7MB → **2.9MB**（+0.2MB，`tray-icon` + `image` crate）。

### 11.2 本轮修掉的 3 个真实缺陷（都是「看起来能用」的隐性 bug）

| # | 缺陷 | 现象 | 修法 |
|---|------|------|------|
| 1 | **关窗即退出，托盘无法常驻** | 点右上角 X → 整个程序退出，托盘图标消失，与"常驻"设计矛盾 | `on_window_event` 拦 `CloseRequested`，`prevent_close()` + `window.hide()`；真退出走托盘菜单 |
| 2 | **缺 GUI 子系统 → 弹黑窗** | PE Subsystem=3(Console)，双击先弹空命令行窗口 | `main.rs` 顶部加 `#![windows_subsystem = "windows"]` → Subsystem=2 |
| 3 | **父进程守护从未生效** | 强杀主程序后 sidecar 残留 **20 秒以上**不退出 | 见 11.3 |

### 11.3 父进程守护失效的两个根因（实测定位）

**根因 A：`os.getppid()` 守护错对象**
PyInstaller onefile 会 fork：`memo-desktop.exe → memo-server.exe(bootloader)` → `memo-server.exe(真正跑 Python)`。
真正跑 Python 的进程，其父进程是 **bootloader**，不是外壳。守护 bootloader 毫无意义。

修法：外壳用 `--parent-pid <pid>` 显式传入该守护谁：
```rust
.arg("--parent-pid").arg(std::process::id().to_string())
```

**根因 B：`GetExitCodeProcess` 判 `STILL_ACTIVE` 不可靠**
实测：父进程已死，该调用仍返回 259，导致守护迟迟不触发。
（用独立实验验证：父进程 3 秒后退出，子进程连续 20 秒探测均得 `alive=True`，
而 `psutil.pid_exists()` 同时刻已返回 False）

修法：改 `SYNCHRONIZE` 句柄 + `WaitForSingleObject`：
```python
SYNCHRONIZE = 0x00100000
WAIT_TIMEOUT = 258
h = OpenProcess(SYNCHRONIZE, False, pid)
if not h: return False                       # 打不开 = 已不存在
return WaitForSingleObject(h, 0) == WAIT_TIMEOUT
```
再加**连续两次判定为死才退出**，避免瞬时失败误杀自己。

修复前后对比：

| | 强杀后 sidecar 退出耗时 |
|---|---|
| 修复前 | **20 秒后仍残留** |
| 修复后 | **6 秒**（日志明确记录「父进程已退出，自动关闭」） |

### 11.4 NSIS 安装包

**为什么手写 .nsi 而不用 `tauri build --bundles nsis`**：Tauri 的 NSIS bundler 依赖
自身模板拉取，在 Linux 交叉编译场景不可靠。直接 `makensis`（跳板机 apt 自带 3.09）更可控。

修复过程中又踩到 2 个安装器缺陷：

| # | 缺陷 | 现象 | 修法 |
|---|------|------|------|
| 4 | **升级安装后主程序缺失** | 卸载后 `memo-desktop.exe` 残留，重装时 `File` 覆盖静默失败 | 安装段先 `taskkill` + `Sleep 1200` + `Delete` 再 `File` |
| 5 | **卸载后文件删不掉** | `taskkill` 返回后句柄未释放，`Delete` 静默失败 | `Sleep 1500` + 宏 `DELRETRY` 三次重试 |

另修 2 处 NSIS 警告：`${INSTDIR}` 误写（应为 `$INSTDIR`）、未使用变量。

**安装包设计**：
- `RequestExecutionLevel user` + 装到 `$LOCALAPPDATA\cn.local.memo`，不需要管理员
- 桌面 + 开始菜单快捷方式
- 控制面板登记（DisplayName/DisplayVersion/UninstallString）
- **卸载默认保留用户数据**，`MessageBox` 明确询问；静默卸载走 `IfSilent keep_data` 自动保留

### 11.5 六项验收（全部实测通过）

| # | 项 | 结果 |
|---|----|------|
| 1 | 静默安装 `/S` | 退出码 0，三件套齐全 |
| 2 | GUI 子系统 | PE Subsystem=2，**无黑窗**（窗口类列表无 `ConsoleWindowClass`） |
| 3 | 关窗入托盘 | 窗口隐藏、进程存活、服务仍可用 |
| 4 | **强杀守护** | 6 秒内 sidecar 自动退出 |
| 5 | 温和退出 | 无残留（`kill_tree` 生效） |
| 6 | 静默卸载 | 程序文件全删、`memo.db` 保留、两处注册表清空、无残留进程 |

### 11.6 环境陷阱：验证时进程「无故消失」

本轮有多次误判为"崩溃"，实际是**工具会话清理子进程**：

| 现象 | 真实原因 | 正确做法 |
|------|---------|---------|
| 启动 15 秒后进程全没 | Bash 会话结束带走子进程组 | 用 `explorer.exe <exe>` 作宿主启动 |
| `Popen` 后秒退 | 父进程（测试脚本）退出触发守护自杀 | 同上，或 `DETACHED_PROCESS` |
| 日志正常就绪却无后续 | 无崩溃记录 = 非崩溃 | 先看 `run.log` 有无异常，再判断 |

判据：`run.log` 显示 sidecar 正常就绪且无崩溃记录 → 是会话清理，不是缺陷。

### 11.7 交付物

```
dist/日历备忘录-1.0.0-安装包.exe   25.3MB（NSIS 安装包，含托盘版）
```

文件结构（安装后 `%LOCALAPPDATA%\cn.local.memo\`）：
`memo-desktop.exe`(2.9MB) / `memo-server.exe`(24MB) / `WebView2Loader.dll` / `data\`
