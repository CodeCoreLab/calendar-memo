# -*- mode: python ; coding: utf-8 -*-
"""PyInstaller 打包配置 —— 生产可移植分发包。

产物：dist/日历备忘录/  （onedir 模式）
     onedir 不用 onefile：onefile 每次启动要把 ~15MB 解压到 Temp，
     杀软对"自解压到临时目录"行为敏感，容易误报；onedir 启动更快、
     结构透明、用户能直接备份 data 目录。

关键点：
  1. web 目录整体打包进 _MEIPASS（只读资源）
  2. data 目录**不打进包**，由 app.data_dir() 落到 exe 同级目录（可写）
  3. mcp/uvicorn 大量动态导入，用 collect_all 收全
"""

import os
import sys

from PyInstaller.utils.hooks import collect_all, collect_submodules

block_cipher = None

datas = []
binaries = []
hiddenimports = []

# web 前端资源（476KB，含 lunar.js 与图片）
datas.append((os.path.join("web"), "web"))

# mcp / uvicorn / 相关传递依赖：动态导入多，静态分析容易漏
for pkg in ("mcp", "uvicorn", "anyio", "httpx", "httpx_sse", "sse_starlette", "pydantic",
            "starlette", "flask", "werkzeug", "waitress", "jinja2", "click",
            "itsdangerous", "h11", "httpcore", "sniffio", "idna", "certifi"):
    try:
        d, b, h = collect_all(pkg)
        datas += d
        binaries += b
        hiddenimports += h
    except Exception as exc:
        print(f"[spec] 跳过 {pkg}: {exc}", file=sys.stderr)

# 应用自身的延迟导入点
hiddenimports += collect_submodules("mcp.server")
hiddenimports += collect_submodules("pystray")
hiddenimports += [
    "uvicorn.logging", "uvicorn.loops", "uvicorn.loops.auto",
    "uvicorn.protocols", "uvicorn.protocols.http", "uvicorn.protocols.http.auto",
    "uvicorn.protocols.http.h11_impl", "uvicorn.protocols.websockets",
    "uvicorn.protocols.websockets.auto", "uvicorn.lifespan",
    "uvicorn.lifespan.on", "uvicorn.lifespan.off",
    "sqlite3", "app", "serve", "mcp_server",
    "PIL.Image", "PIL.ImageDraw", "PIL._imaging",
]

# 剔除用不到的大件，减小体积
# 注意：cryptography **不能剔除**。mcp 的 JWT 支持在导入链上硬引用它，
# 剔除后启动只打印 "[warn] MCP 启动失败：No module named 'cryptography'"}，
# 网页与 REST 仍正常 —— 属静默降级，最易漏测。实测记录见 docs/PACKAGING.md。
# PIL 也不能剔除：托盘图标用 Pillow 现画，去掉后托盘起不来。
excludes = [
    "tkinter", "numpy", "pandas", "matplotlib", "scipy",
    "IPython", "pytest", "setuptools", "pip", "wheel",
    "unittest", "pydoc_data", "lib2to3",
]

a = Analysis(
    ["desktop.py"],
    pathex=[],
    binaries=binaries,
    datas=datas,
    hiddenimports=hiddenimports,
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=excludes,
    noarchive=False,
)

pyz = PYZ(a.pure, a.zipped_data, cipher=block_cipher)

exe = EXE(
    pyz,
    a.scripts,
    [],
    exclude_binaries=True,
    name="日历备忘录",
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=False,
    # console=False 去掉控制台黑窗，用户只看到托盘图标。
    # 日志改由 desktop.daemonize_consoleless() 重定向到 data/run.log。
    console=False,
)

coll = COLLECT(
    exe,
    a.binaries,
    a.datas,
    strip=False,
    upx=False,
    name="日历备忘录",
)
