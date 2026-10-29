"""桌面启动器：可执行程序的唯一入口。

职责：
  1. 单实例锁 —— 防用户双击两次开两个进程写同一个 SQLite
  2. 后台线程起 Flask/MCP 服务
  3. 等就绪后自动打开默认浏览器
  4. 托盘图标常驻，右键菜单：打开界面 / 数据目录 / 退出

关于「无黑窗」：打包时 console=False 去掉控制台窗口，日志改为写 run.log，
避免用户看到 cmd 黑框。托盘是唯一的可见入口。

用法：
    python desktop.py                # 开发时直接跑（有控制台）
    python desktop.py --port 8000 --no-browser --no-tray
"""

import argparse
import getpass
import os
import socket
import sys
import threading
import time
import webbrowser

import app as memo
import serve as srv

APP_NAME = "日历备忘录"


def instance_lock():
    """单实例锁：绑定固定回环端口做互斥。

    比文件锁可靠——进程崩溃时端口自动释放，不会留下死锁文件。
    返回 (socket, 已存在实例的端口或 None)。socket 必须由调用方持有到退出。
    """
    s = socket.socket()
    s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 0)
    try:
        s.bind(("127.0.0.1", LOCK_PORT))
        s.listen(1)
        return s, None
    except OSError:
        s.close()
        # 端口被占 = 已有实例。尝试读它公布的端口号
        return None, _probe_existing_port()


LOCK_PORT = 47700          # 单实例互斥端口（固定，不用 8000 避免与用户其他服务冲突）


def _probe_existing_port():
    """从已有实例的锁端口读它公布的工作端口，供"再点一次就打开界面"。"""
    try:
        with socket.create_connection(("127.0.0.1", LOCK_PORT), timeout=1.0) as c:
            c.settimeout(1.0)
            data = c.recv(16).decode(errors="replace").strip()
            return int(data) if data.isdigit() else None
    except (OSError, ValueError):
        return None


def publish_port(lock_sock, port):
    """把工作端口写到锁端口，供后来者读取。

    放在后台线程里跑，避免阻塞启动：每次有人连进来就回一行端口号。
    """
    def loop():
        while True:
            try:
                conn, _ = lock_sock.accept()
            except OSError:
                return
            try:
                conn.sendall(str(port).encode())
            except OSError:
                pass
            finally:
                conn.close()
    threading.Thread(target=loop, daemon=True, name="instance-publish").start()


def free_port(preferred=8000):
    """优先用 preferred（默认 8000，用户熟悉），被占则让系统分配。

    preferred 传 None 表示跳过首选，直接用内核分配的端口。
    """
    candidates = (0,) if preferred is None else (preferred, 0)
    for port in candidates:
        s = socket.socket()
        try:
            s.bind(("127.0.0.1", port))
            got = s.getsockname()[1]
            s.close()
            return got
        except OSError:
            s.close()
            continue
    raise RuntimeError("无法分配端口")


def wait_ready(port, timeout=15.0):
    deadline = time.time() + timeout
    while time.time() < deadline:
        try:
            s = socket.create_connection(("127.0.0.1", port), timeout=0.3)
            s.close()
            return True
        except OSError:
            time.sleep(0.15)
    return False


def make_icon_image():
    """生成托盘图标（纯代码画，不依赖外部图片文件）。"""
    from PIL import Image, ImageDraw
    size = 64
    img = Image.new("RGBA", (size, size), (0, 0, 0, 0))
    d = ImageDraw.Draw(img)
    d.rounded_rectangle((4, 6, 60, 60), radius=12, fill=(0x53, 0x4A, 0xB7, 255))
    d.rounded_rectangle((4, 6, 60, 22), radius=12, fill=(0x7F, 0x77, 0xDD, 255))
    for r in range(2):
        for c in range(3):
            x = 14 + c * 14
            y = 32 + r * 13
            d.ellipse((x, y, x + 5, y + 5), fill=(255, 255, 255, 230))
    return img


def run_server(port, no_mcp):
    """后台线程里跑 waitress，直到进程退出。"""
    if not no_mcp:
        mcp_port = srv.pick_port()
        try:
            if srv.start_mcp_http(mcp_port):
                original_wsgi = memo.app.wsgi_app

                def router(environ, start_response):
                    if environ.get("PATH_INFO", "").startswith(srv.MCP_PATH_PREFIX):
                        return srv.MCPForwarder(mcp_port)(environ, start_response)
                    return original_wsgi(environ, start_response)

                memo.app.wsgi_app = router
                memo.TOKEN_CACHE_HOOKS.append(srv.MCPForwarder.invalidate)
            else:
                log("[warn] MCP HTTP 未就绪，仅提供 Web/REST")
        except Exception as exc:
            log(f"[warn] MCP 启动失败：{exc}")

    hs = memo.run_housekeeping()
    log(f"[clean] 凭证图 {hs['uploads']} / 幂等 {hs['idempotency']} / 过期Token {hs['tokens']}")

    # port=0 时内核分配端口，waitress 内部才绑定。先自己探一个可用端口，
    # 这样 _KNOWN_PORT 能立刻反映实际监听地址，sidecar 模式才能回报给父进程。
    if port == 0:
        port = free_port(None)

    from waitress import serve
    _KNOWN_PORT[0] = port
    serve(memo.app, host="127.0.0.1", port=port, threads=8, channel_timeout=300)


def log(msg):
    """无控制台时写 run.log，有控制台时同时打印。"""
    print(msg, flush=True)
    if getattr(sys, "frozen", False) and not sys.stdout:
        return
    try:
        with open(os.path.join(memo.DATA_DIR, "run.log"), "a", encoding="utf-8") as f:
            f.write(f"{time.strftime('%Y-%m-%d %H:%M:%S')} {msg}\n")
    except OSError:
        pass


def daemonize_consoleless():
    """没有控制台时把 stdout/stderr 重定向到日志，避免写满缓冲区异常。

    console=False 打包后 sys.stdout 是 None，任何 print 都会 AttributeError。
    必须在最早处重定向。
    """
    if sys.stdout is None or sys.stderr is None:
        os.makedirs(memo.DATA_DIR, exist_ok=True)
        path = os.path.join(memo.DATA_DIR, "run.log")
        f = open(path, "a", encoding="utf-8", buffering=1)
        if sys.stdout is None:
            sys.stdout = f
        if sys.stderr is None:
            sys.stderr = f


def run_sidecar(args):
    """被 Tauri 外壳拉起的模式。

    与独立运行的差别：
      - 不弹托盘（窗口由 Tauri 提供）
      - 不开浏览器（同上）
      - 不做单实例锁（Tauri 自己保证单实例）
      - 端口就绪后写到 --port-file 指定的文件，父进程按此读取

    为什么用文件而不是 stdout：PyInstaller 的 windowed 模式
    （console=False）下 sys.stdout 不可用，print 会抛
    OSError [Errno 22]。文件是唯一可靠的通路。
    """
    port = args.port or 0
    PARENT_PID[0] = args.parent_pid

    threading.Thread(target=run_server, args=(port, args.no_mcp),
                     daemon=True, name="web").start()

    deadline = time.time() + 25.0
    real = None
    while time.time() < deadline:
        real = _KNOWN_PORT[0]
        if real and _port_alive(real):
            break
        time.sleep(0.2)

    if not real:
        log("[sidecar] 服务未能在 25 秒内就绪")
        return 1

    port_file = args.port_file
    if port_file:
        try:
            os.makedirs(os.path.dirname(port_file) or ".", exist_ok=True)
            # 原子写：先写临时文件再改名，避免父进程读到半截内容
            tmp = port_file + ".tmp"
            with open(tmp, "w", encoding="utf-8") as f:
                f.write(str(real))
            os.replace(tmp, port_file)
            log(f"[sidecar] 端口 {real} 已写入 {port_file}")
        except OSError as exc:
            log(f"[sidecar] 写端口文件失败：{exc}")

    log(f"[sidecar] 已就绪 http://127.0.0.1:{real}")

    # 父进程守护：主程序被强制结束（任务管理器/崩溃）时，Tauri 的退出回调
    # 不会执行，本进程会变孤儿。每 2 秒检查一次父进程是否还在，不在就退出。
    _watch_parent()

    try:
        while True:
            time.sleep(3600)
    except KeyboardInterrupt:
        return 0


def _watch_parent():
    """后台线程：父进程消失时自杀，避免孤儿进程占端口。

    父 PID 由外壳通过 --parent-pid 显式传入，而不是依赖 os.getppid()：
    PyInstaller onefile 会 fork，真正跑 Python 的进程的父进程是 bootloader
    而不是外壳，getppid() 拿到的不是我们想守护的那个进程。
    """
    parent = PARENT_PID[0]
    if parent <= 0:
        return

    def loop():
        # 连续两次判定为死才退出：避免瞬时探测失败误杀自己
        dead_count = 0
        while True:
            time.sleep(2)
            if _pid_alive(parent):
                dead_count = 0
                continue
            dead_count += 1
            if dead_count >= 2:
                try:
                    log("[sidecar] 父进程已退出，自动关闭")
                except Exception:
                    pass
                os._exit(0)

    threading.Thread(target=loop, daemon=True, name="parent-guard").start()


def _pid_alive(pid):
    """探测 PID 是否存活（Windows 规范做法）。

    用 SYNCHRONIZE 句柄 + WaitForSingleObject：
      - WAIT_TIMEOUT(258)   = 仍在运行
      - WAIT_OBJECT_0(0)    = 已退出
    不用 GetExitCodeProcess 判 STILL_ACTIVE —— 实测在父进程刚死的一段时间内
    仍可能返回 259，导致守护迟迟不触发（实测残留 20 秒以上）。
    打不开句柄（OpenProcess 返回 0）= 进程已不存在。
    """
    if pid <= 0:
        return False
    try:
        import ctypes
        SYNCHRONIZE = 0x00100000
        WAIT_TIMEOUT = 258
        h = ctypes.windll.kernel32.OpenProcess(SYNCHRONIZE, False, pid)
        if not h:
            return False
        try:
            return ctypes.windll.kernel32.WaitForSingleObject(h, 0) == WAIT_TIMEOUT
        finally:
            ctypes.windll.kernel32.CloseHandle(h)
    except Exception:
        return True      # 非 Windows 或探测异常：保守认为还活着，不误杀自己


# 外壳传入的父进程 PID（--parent-pid）
PARENT_PID = [0]


def _port_alive(port):
    try:
        s = socket.create_connection(("127.0.0.1", port), timeout=0.3)
        s.close()
        return True
    except OSError:
        return False


_KNOWN_PORT = [0]


def main():
    daemonize_consoleless()

    ap = argparse.ArgumentParser(description="日历备忘录（桌面版）")
    ap.add_argument("--port", type=int, default=0, help="0 = 自动选择")
    ap.add_argument("--no-browser", action="store_true", help="不自动打开浏览器")
    ap.add_argument("--no-mcp", action="store_true", help="不启用 MCP HTTP")
    ap.add_argument("--no-tray", action="store_true", help="不显示托盘（调试用）")
    ap.add_argument("--sidecar", action="store_true",
                    help="被 Tauri 外壳拉起：不开托盘/浏览器，端口写入 --port-file")
    ap.add_argument("--port-file", default="",
                    help="sidecar 模式下，就绪后把实际端口写入该文件")
    ap.add_argument("--parent-pid", type=int, default=0,
                    help="sidecar 模式下，父进程 PID；该进程消失后本进程自动退出")
    args = ap.parse_args()

    if args.sidecar:
        return run_sidecar(args)

    # ── 单实例 ──
    lock, existing_port = instance_lock()
    if existing_port:
        url = f"http://127.0.0.1:{existing_port}/memo.html"
        log(f"已在运行，直接打开界面：{url}")
        webbrowser.open(url)
        return 0
    if lock is None:
        log("检测到已有实例在运行，本次不重复启动")
        return 0

    port = args.port or free_port()
    publish_port(lock, port)

    threading.Thread(target=run_server, args=(port, args.no_mcp),
                     daemon=True, name="web").start()

    url = f"http://127.0.0.1:{port}/memo_login.html"
    log("=" * 56)
    log("  日历备忘录（本地版）已启动")
    log(f"  浏览器访问： {url}")
    log(f"  数据文件：   {memo.DB_PATH}")
    log("  关闭托盘图标即停止服务；数据保存在 data 目录，不会丢失")
    log("=" * 56)

    if not args.no_browser:
        def open_when_ready():
            if wait_ready(port):
                try:
                    webbrowser.open(url)
                except Exception:
                    pass
        threading.Thread(target=open_when_ready, daemon=True).start()

    if args.no_tray:
        # 无托盘：前台阻塞（开发调试时能 Ctrl+C）
        try:
            while True:
                time.sleep(60)
        except KeyboardInterrupt:
            return 0

    # ── 托盘（占主线程，pystray 要求） ──
    try:
        import pystray

        def on_open(icon, item):
            webbrowser.open(url)

        def on_data(icon, item):
            # 打开数据目录，方便用户备份 memo.db
            try:
                os.startfile(memo.DATA_DIR)
            except Exception as exc:
                log(f"打开数据目录失败：{exc}")

        def on_backup(icon, item):
            try:
                path = memo.backup_db()
                log(f"已备份 → {path}")
                os.startfile(memo.DATA_DIR)
            except Exception as exc:
                log(f"备份失败：{exc}")

        def on_quit(icon, item):
            log("用户从托盘退出")
            icon.stop()

        icon = pystray.Icon(
            APP_NAME,
            make_icon_image(),
            APP_NAME,
            menu=pystray.Menu(
                pystray.MenuItem("打开界面", on_open, default=True),
                pystray.MenuItem("立即备份数据", on_backup),
                pystray.MenuItem("数据目录", on_data),
                pystray.Menu.SEPARATOR,
                pystray.MenuItem("退出", on_quit),
            ),
        )
        icon.run()          # 阻塞直到 on_quit
        return 0
    except Exception as exc:
        log(f"[warn] 托盘启动失败：{exc}，改为前台运行")
        try:
            while True:
                time.sleep(60)
        except KeyboardInterrupt:
            return 0


if __name__ == "__main__":
    sys.exit(main() or 0)
