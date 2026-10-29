# -*- mode: python ; coding: utf-8 -*-
"""PyInstaller 配置 —— 构建 Tauri 外壳用的 Python sidecar（单文件 memo-server.exe）。

与 memo.spec 的差别：
  - **onefile** 而非 onedir：sidecar 要作为单个资源被 Tauri 携带，
    多目录结构在 resources 里不好处理。
  - **console=False**：由 Tauri 用 CREATE_NO_WINDOW 拉起，本就不该有窗口。
  - 端口通过 stdout 的 `MEMO_PORT=<n>` 回报给父进程（见 desktop.py --sidecar）。

产物：dist-sidecar/memo-server.exe
"""

import os
import sys

from PyInstaller.utils.hooks import collect_all, collect_submodules

datas = []
binaries = []
hiddenimports = []

# web 前端资源（sidecar 要自己提供页面）
datas.append((os.path.join("web"), "web"))

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

hiddenimports += collect_submodules("mcp.server")
hiddenimports += [
    "uvicorn.logging", "uvicorn.loops", "uvicorn.loops.auto",
    "uvicorn.protocols", "uvicorn.protocols.http", "uvicorn.protocols.http.auto",
    "uvicorn.protocols.http.h11_impl", "uvicorn.protocols.websockets",
    "uvicorn.protocols.websockets.auto", "uvicorn.lifespan",
    "uvicorn.lifespan.on", "uvicorn.lifespan.off",
    "sqlite3", "app", "serve", "mcp_server",
]

# sidecar 不需要托盘（窗口由 Tauri 提供），所以 pystray/PIL 可以剔除 —— 省约 7MB。
# cryptography 仍不能剔（mcp → pyjwt[crypto] 硬引用）。
excludes = [
    "tkinter", "pystray", "PIL", "numpy", "pandas", "matplotlib", "scipy",
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

pyz = PYZ(a.pure, a.zipped_data, cipher=None)

exe = EXE(
    pyz,
    a.scripts,
    a.binaries,
    a.datas,
    [],
    name="memo-server",
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=False,
    console=False,          # Tauri 用 CREATE_NO_WINDOW 拉起，无需控制台
    disable_windowed_traceback=False,
)
