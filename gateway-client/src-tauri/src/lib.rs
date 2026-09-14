use std::sync::atomic::{AtomicBool, Ordering};
use std::sync::{Mutex, OnceLock};
use std::time::Duration;
#[cfg(target_os = "windows")]
use std::os::windows::process::CommandExt;
use tauri::{
    Manager,
    menu::{Menu, MenuItem},
    tray::{TrayIcon, TrayIconBuilder},
};

static BACKEND_STARTED: AtomicBool = AtomicBool::new(false);
static BACKEND_PORT: Mutex<u16> = Mutex::new(18123);
static TRAY_ICON: OnceLock<TrayIcon> = OnceLock::new();
#[cfg(target_os = "windows")]
const CREATE_NO_WINDOW: u32 = 0x08000000;

fn gateway_dir() -> std::path::PathBuf {
    let exe_dir = std::env::current_exe()
        .unwrap_or_else(|_| std::path::PathBuf::from("."))
        .parent()
        .unwrap_or(&std::path::PathBuf::from("."))
        .to_path_buf();
    let candidates = [
        exe_dir.join("..").join("..").join(".."),
        exe_dir.join("..").join(".."),
        exe_dir.join(".."),
        exe_dir.clone(),
    ];
    for candidate in &candidates {
        // AI-LLM 根目录含 server.py 与 core/
        if candidate.join("server.py").exists() && candidate.join("core").exists() {
            return candidate.canonicalize().unwrap_or_else(|_| candidate.clone());
        }
    }
    exe_dir.join("..").join("..").join("..")
}

fn which(name: &str) -> bool {
    let mut cmd = std::process::Command::new(name);
    cmd.arg("--version");
    #[cfg(target_os = "windows")]
    cmd.creation_flags(CREATE_NO_WINDOW);
    cmd.output()
        .map(|o| o.status.success())
        .unwrap_or(false)
}

fn find_python() -> String {
    let base = gateway_dir();
    let rt = base.join("runtime").join("python.exe");
    if rt.exists() {
        return rt.to_string_lossy().to_string();
    }
    let rt2 = base.join("runtime").join("pythonw.exe");
    if rt2.exists() {
        return rt2.to_string_lossy().to_string();
    }
    for name in ["py.exe", "python.exe", "pythonw.exe"] {
        if which(name) {
            return name.to_string();
        }
    }
    "python.exe".to_string()
}

fn is_port_up(port: u16) -> bool {
    std::net::TcpStream::connect(format!("127.0.0.1:{}", port))
        .map(|_| true)
        .unwrap_or(false)
}

fn start_backend() {
    let port = *BACKEND_PORT.lock().unwrap();
    if is_port_up(port) {
        return;
    }
    let base = gateway_dir();
    let python = find_python();
    let server_py = base.join("server.py");
    if !server_py.exists() {
        return;
    }
    let _ = std::process::Command::new(&python)
        .arg("server.py")
        .current_dir(&base)
        .creation_flags(0x08000000)
        .spawn();
    for _ in 0..30 {
        std::thread::sleep(Duration::from_secs(1));
        if is_port_up(port) {
            return;
        }
    }
}

fn stop_backend() {
    let port = *BACKEND_PORT.lock().unwrap();
    if let Ok(output) = std::process::Command::new("netstat")
        .args(["-ano"])
        .creation_flags(CREATE_NO_WINDOW)
        .output()
    {
        let stdout = String::from_utf8_lossy(&output.stdout);
        for line in stdout.lines() {
            if line.contains(&format!(":{}", port)) && line.contains("LISTENING") {
                let parts: Vec<&str> = line.split_whitespace().collect();
                if let Some(pid_str) = parts.last() {
                    if let Ok(pid) = pid_str.parse::<u32>() {
                        let _ = std::process::Command::new("taskkill")
                            .args(["/F", "/T", "/PID", &pid.to_string()])
                            .creation_flags(CREATE_NO_WINDOW)
                            .output();
                    }
                }
            }
        }
    }
}

fn restart_engine() {
    let port = *BACKEND_PORT.lock().unwrap();
    let _ = std::process::Command::new("powershell")
        .args([
            "-NoProfile",
            "-Command",
            &format!("Invoke-RestMethod -Uri http://127.0.0.1:{}/api/config -Method Get", port),
        ])
        .creation_flags(0x08000000)
        .spawn();
}

#[tauri::command]
fn get_port() -> u16 {
    *BACKEND_PORT.lock().unwrap()
}

#[tauri::command]
fn get_status() -> serde_json::Value {
    let port = *BACKEND_PORT.lock().unwrap();
    let up = is_port_up(port);
    serde_json::json!({
        "backendUp": up,
        "port": port,
        "url": format!("http://127.0.0.1:{}", port),
    })
}

#[tauri::command]
fn cmd_restart_engine() {
    restart_engine();
}

#[tauri::command]
fn cmd_quit(app: tauri::AppHandle) {
    if BACKEND_STARTED.load(Ordering::Relaxed) {
        stop_backend();
    }
    app.exit(0);
}

#[cfg_attr(mobile, tauri::mobile_entry_point)]
pub fn run() {
    tauri::Builder::default()
        .plugin(tauri_plugin_shell::init())
        .setup(|app| {
            let port = *BACKEND_PORT.lock().unwrap();

            let show_item = MenuItem::with_id(app, "show", "显示/隐藏", true, None::<&str>)?;
            let restart_item = MenuItem::with_id(app, "restart", "重启引擎", true, None::<&str>)?;
            let quit_item = MenuItem::with_id(app, "quit", "退出", true, None::<&str>)?;
            let menu = Menu::with_items(app, &[&show_item, &restart_item, &quit_item])?;

            let tray = TrayIconBuilder::new()
                .icon(app.default_window_icon().unwrap().clone())
                .menu(&menu)
                .tooltip("AI-LLM")
                .build(app)?;

            let _ = TRAY_ICON.set(tray);

            app.on_menu_event(move |app, event| {
                match event.id.as_ref() {
                    "show" => {
                        if let Some(win) = app.get_webview_window("main") {
                            let _ = win.show();
                            let _ = win.set_focus();
                        }
                    }
                    "restart" => {
                        restart_engine();
                    }
                    "quit" => {
                        if BACKEND_STARTED.load(Ordering::Relaxed) {
                            stop_backend();
                        }
                        app.exit(0);
                    }
                    _ => {}
                }
            });

            if let Some(win) = app.get_webview_window("main") {
                let handle = app.handle().clone();
                win.on_window_event(move |event| {
                    if let tauri::WindowEvent::CloseRequested { api, .. } = event {
                        api.prevent_close();
                        if let Some(w) = handle.get_webview_window("main") {
                            let _ = w.hide();
                        }
                    }
                });
            }

            let app_handle = app.handle().clone();
            std::thread::spawn(move || {
                start_backend();
                BACKEND_STARTED.store(true, Ordering::Relaxed);
                // 主窗口 = 网关管理台（含聊天/供应商/池/监控/设置五个功能页）
                let url = format!("http://127.0.0.1:{}/", port);
                if let Some(win) = app_handle.get_webview_window("main") {
                    let _ = win.navigate(url.parse().unwrap());
                    let _ = win.show();
                }
            });

            Ok(())
        })
        .invoke_handler(tauri::generate_handler![
            get_port,
            get_status,
            cmd_restart_engine,
            cmd_quit
        ])
        .run(tauri::generate_context!())
        .expect("error while running tauri application");
}
