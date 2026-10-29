// 日历备忘录 —— Tauri 外壳
//
// 职责边界（刻意保持极薄）：
//   1. 拉起 Python sidecar（同目录下的 memo-server.exe）
//   2. 从端口文件读到实际端口（不用 stdout —— windowed 模式下 stdout 不可用，
//      PyInstaller console=False 时 print 会抛 OSError [Errno 22]）
//   3. 打开原生窗口指向 http://127.0.0.1:<n>/memo_login.html
//   4. 系统托盘常驻：打开界面 / 打开数据目录 / 退出
//   5. 应用退出时杀掉 sidecar 整棵进程树
//
// 业务逻辑一行都不在这里 —— 全部仍由 Python 承担。

// 以 GUI 子系统运行：不分配控制台，双击不弹黑窗。
// 缺这一行会以 Console 子系统编译，用户会看到一个空的命令行窗口。
// 代价：stdout/stderr 不再可见，诊断信息需自行写入文件（见 error 页面与 sidecar 的 run.log）。
#![windows_subsystem = "windows"]

use std::path::PathBuf;
use std::process::{Child, Command, Stdio};
use std::sync::Mutex;
use std::time::{Duration, Instant};

use tauri::menu::{Menu, MenuItem};
use tauri::tray::{MouseButton, TrayIconBuilder, TrayIconEvent};
use tauri::{AppHandle, Manager, RunEvent, WebviewUrl, WebviewWindowBuilder};

/// 运行期共享状态：sidecar 句柄 + 服务基址 + 数据目录。
struct Runtime {
    child: Mutex<Option<Child>>,
    /// 如 http://127.0.0.1:49420；未就绪时为空串
    base_url: String,
    /// exe 同级 data/，托盘菜单据此打开目录
    data_dir: PathBuf,
}

fn no_window(cmd: &mut Command) -> &mut Command {
    #[cfg(windows)]
    {
        use std::os::windows::process::CommandExt;
        const CREATE_NO_WINDOW: u32 = 0x0800_0000;
        cmd.creation_flags(CREATE_NO_WINDOW)
    }
    #[cfg(not(windows))]
    {
        cmd
    }
}

/// 可写数据目录：与 exe 同级，保证换机器/换 U 盘都能带走数据。
fn data_dir() -> PathBuf {
    std::env::current_exe()
        .ok()
        .and_then(|p| p.parent().map(|x| x.to_path_buf()))
        .unwrap_or_default()
        .join("data")
}

/// 拉起 sidecar 并等待端口文件。返回子进程与端口。
fn spawn_sidecar() -> (Option<Child>, Option<u16>) {
    let exe_dir = match std::env::current_exe()
        .ok()
        .and_then(|p| p.parent().map(|x| x.to_path_buf()))
    {
        Some(d) => d,
        None => return (None, None),
    };

    let server = exe_dir.join("memo-server.exe");
    if !server.exists() {
        eprintln!("[tauri] 未找到 sidecar: {}", server.display());
        return (None, None);
    }

    let ddir = data_dir();
    let _ = std::fs::create_dir_all(&ddir);
    let port_file = ddir.join("sidecar-port.txt");
    // 上次异常退出可能残留旧端口，先删掉，避免读到过期值
    let _ = std::fs::remove_file(&port_file);

    let mut cmd = Command::new(&server);
    cmd.arg("--sidecar")
        .arg("--no-browser")
        .arg("--port-file")
        .arg(&port_file)
        // 显式传父 PID：sidecar 是 PyInstaller onefile，会 fork，
        // 真正跑 Python 的进程的父进程是 bootloader 而不是本程序，
        // 靠 os.getppid() 会守护错对象，必须由外壳告诉它该看谁。
        .arg("--parent-pid")
        .arg(std::process::id().to_string())
        .env("MEMO_DATA_DIR", &ddir)
        .stdout(Stdio::null())
        .stderr(Stdio::inherit());
    no_window(&mut cmd);

    let child = match cmd.spawn() {
        Ok(c) => c,
        Err(e) => {
            eprintln!("[tauri] sidecar 启动失败: {e}");
            return (None, None);
        }
    };

    // 25 秒上限：首启动要建库、跑例行清理，慢些属正常
    let deadline = Instant::now() + Duration::from_secs(25);
    while Instant::now() < deadline {
        if let Ok(text) = std::fs::read_to_string(&port_file) {
            if let Ok(port) = text.trim().parse::<u16>() {
                return (Some(child), Some(port));
            }
        }
        std::thread::sleep(Duration::from_millis(200));
    }

    eprintln!("[tauri] sidecar 25 秒内未就绪");
    let mut c = child;
    let _ = c.kill();
    (None, None)
}

/// 杀掉 sidecar 整棵进程树。
///
/// PyInstaller 的 onefile 会 fork 出子进程，只 kill 父进程会留孤儿占端口。
fn kill_tree(pid: u32) {
    #[cfg(windows)]
    {
        use std::os::windows::process::CommandExt;
        const CREATE_NO_WINDOW: u32 = 0x0800_0000;
        let _ = Command::new("taskkill")
            .args(["/PID", &pid.to_string(), "/T", "/F"])
            .stdout(Stdio::null())
            .stderr(Stdio::null())
            .creation_flags(CREATE_NO_WINDOW)
            .status();
    }
    #[cfg(not(windows))]
    {
        let _ = Command::new("kill").args(["-9", &format!("-{pid}")]).status();
    }
}

/// 失败时显示的错误页（写成本地 HTML 再用 file:// 打开 ——
/// Tauri 默认不支持 data: URL，需额外 feature）。
fn write_error_page(msg: &str) -> PathBuf {
    let dd = data_dir();
    let _ = std::fs::create_dir_all(&dd);
    let f = dd.join("error.html");
    let html = format!(
        "<!DOCTYPE html><meta charset=utf-8>\
         <body style='font-family:-apple-system,\"Segoe UI\",sans-serif;padding:2.5em;color:#2c2c2a'>\
         <h2 style='font-weight:500'>未能启动本地服务</h2>\
         <p style='color:#5f5e5a;font-size:14px'>{msg}</p>\
         <p style='color:#888780;font-size:12px'>请确认 memo-server.exe 与本程序在同一目录。</p>\
         </body>",
        msg = msg
    );
    let _ = std::fs::write(&f, html);
    f
}

/// 把窗口带到前台：已存在就 show+unminimize+focus，不存在就新建。
fn show_main_window(app: &AppHandle, base_url: &str) {
    if let Some(w) = app.get_webview_window("main") {
        let _ = w.show();
        let _ = w.unminimize();
        let _ = w.set_focus();
        return;
    }
    if base_url.is_empty() {
        return;
    }
    let url = format!("{base_url}/memo.html");
    if let Ok(parsed) = url.parse() {
        let _ = WebviewWindowBuilder::new(app, "main", WebviewUrl::External(parsed))
            .title("日历备忘录")
            .inner_size(1440.0, 900.0)
            .min_inner_size(1024.0, 700.0)
            .center()
            .build();
    }
}

/// 在文件管理器中打开目录。
fn open_in_explorer(dir: &PathBuf) {
    #[cfg(windows)]
    {
        use std::os::windows::process::CommandExt;
        const CREATE_NO_WINDOW: u32 = 0x0800_0000;
        let _ = Command::new("explorer")
            .arg(dir)
            .creation_flags(CREATE_NO_WINDOW)
            .spawn();
    }
    #[cfg(not(windows))]
    {
        let _ = Command::new("xdg-open").arg(dir).spawn();
    }
}

fn main() {
    tauri::Builder::default()
        // 点击窗口关闭按钮时只隐藏窗口，程序继续在托盘运行。
        // 不这样处理的话，关窗会触发 ExitRequested → 整个程序退出，
        // 托盘图标随之消失，"常驻"就无从谈起。
        // 真正退出走托盘菜单的「退出」项（app.exit(0)）。
        .on_window_event(|window, event| {
            if let tauri::WindowEvent::CloseRequested { api, .. } = event {
                api.prevent_close();
                let _ = window.hide();
            }
        })
        .setup(|app| {
            let (child, port) = spawn_sidecar();
            let base_url = port
                .map(|p| format!("http://127.0.0.1:{p}"))
                .unwrap_or_default();

            app.manage(Runtime {
                child: Mutex::new(child),
                base_url: base_url.clone(),
                data_dir: data_dir(),
            });

            // ── 主窗口 ──
            match port {
                Some(p) => {
                    let url = format!("http://127.0.0.1:{p}/memo_login.html");
                    WebviewWindowBuilder::new(app, "main", WebviewUrl::External(url.parse()?))
                        .title(format!("日历备忘录 · 127.0.0.1:{p}"))
                        .inner_size(1440.0, 900.0)
                        .min_inner_size(1024.0, 700.0)
                        .center()
                        .build()?;
                }
                None => {
                    let f = write_error_page("服务进程未能就绪。");
                    let u = format!("file:///{}", f.to_string_lossy().replace('\\', "/"));
                    WebviewWindowBuilder::new(app, "main", WebviewUrl::External(u.parse()?))
                        .title("日历备忘录 · 启动失败")
                        .inner_size(900.0, 600.0)
                        .center()
                        .build()?;
                }
            }

            // ── 系统托盘 ──
            // 图标复用窗口图标（tauri.conf.json 已配 icon.ico），不额外打包资源
            let open_i = MenuItem::with_id(app, "open", "打开界面", true, None::<&str>)?;
            let data_i = MenuItem::with_id(app, "data", "打开数据目录", true, None::<&str>)?;
            let quit_i = MenuItem::with_id(app, "quit", "退出", true, None::<&str>)?;
            let menu = Menu::with_items(app, &[&open_i, &data_i, &quit_i])?;

            let mut tray = TrayIconBuilder::with_id("main-tray")
                .tooltip("日历备忘录")
                .menu(&menu)
                .show_menu_on_left_click(false)
                .on_menu_event(|app, event| match event.id.as_ref() {
                    "open" => {
                        let base = app.state::<Runtime>().base_url.clone();
                        show_main_window(app, &base);
                    }
                    "data" => {
                        let d = app.state::<Runtime>().data_dir.clone();
                        open_in_explorer(&d);
                    }
                    "quit" => app.exit(0),
                    _ => {}
                })
                .on_tray_icon_event(|tray, event| {
                    if let TrayIconEvent::Click { button, .. } = event {
                        if button == MouseButton::Left {
                            let app = tray.app_handle();
                            let base = app.state::<Runtime>().base_url.clone();
                            show_main_window(app, &base);
                        }
                    }
                });

            if let Some(icon) = app.default_window_icon().cloned() {
                tray = tray.icon(icon);
            }
            tray.build(app)?;

            Ok(())
        })
        .build(tauri::generate_context!())
        .expect("tauri 初始化失败")
        .run(|app, event| {
            // 退出时杀掉 sidecar 整棵树，否则 PyInstaller 派生的子进程会
            // 残留占端口，导致下次启动失败。
            if matches!(event, RunEvent::ExitRequested { .. } | RunEvent::Exit) {
                if let Some(rt) = app.try_state::<Runtime>() {
                    if let Ok(mut guard) = rt.child.lock() {
                        if let Some(mut c) = guard.take() {
                            kill_tree(c.id());
                            let _ = c.kill();
                            let _ = c.wait();
                        }
                    }
                }
            }
        });
}
