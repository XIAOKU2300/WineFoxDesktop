//! Tauri commands exposed to the frontend + the shared exit-flow logic
//! (also used by the tray).

use crate::sidecar::{SidecarManager, APP_VERSION};
use crate::state::AppState;
use crate::updater::{UpdateManager, UpdatePhase};
use serde::Serialize;
use std::sync::Arc;
use std::time::Duration;
use tauri::{AppHandle, Emitter, Runtime, State};

/// Result of evaluating an exit request.
#[derive(Debug, Clone, Serialize)]
#[serde(rename_all = "camelCase")]
pub struct ExitDecision {
    pub needs_choice: bool,
    pub busy: bool,
    pub tasks: u32,
}

impl ExitDecision {
    fn direct() -> Self {
        Self {
            needs_choice: false,
            busy: false,
            tasks: 0,
        }
    }

    fn needs_choice(busy: bool, tasks: u32) -> Self {
        Self {
            needs_choice: true,
            busy,
            tasks,
        }
    }
}

#[tauri::command]
pub fn get_status(state: State<'_, Arc<AppState>>) -> crate::state::AppStatus {
    state.snapshot(APP_VERSION, crate::handshake::PROTOCOL)
}

#[tauri::command]
pub fn start_sidecar(manager: State<'_, Arc<SidecarManager>>) {
    manager.start();
}

#[tauri::command]
pub fn restart_sidecar(manager: State<'_, Arc<SidecarManager>>) {
    manager.start();
}

// ---------------------------------------------------------------------
// exit flow (shared with the tray)
// ---------------------------------------------------------------------

pub fn begin_exit_impl<R: Runtime>(app: &AppHandle<R>, manager: &Arc<SidecarManager>) -> ExitDecision {
    {
        let inner = manager.state.inner.lock().unwrap();
        if inner.quitting {
            return ExitDecision::direct();
        }
    }
    let busy = manager.state.inner.lock().unwrap().busy;
    let tasks = manager.state.inner.lock().unwrap().tasks;
    let running = manager.is_running();
    let _ = app;
    if running && busy && tasks > 0 {
        ExitDecision::needs_choice(busy, tasks)
    } else {
        ExitDecision::direct()
    }
}

pub fn confirm_exit_impl<R: Runtime>(app: &AppHandle<R>, manager: &Arc<SidecarManager>, mode: &str) {
    {
        let mut inner = manager.state.inner.lock().unwrap();
        inner.quitting = true;
        if mode == "now" {
            inner.shutdown_pending = true;
            inner.shutdown_mode = Some("now".into());
        }
        drop(inner);
    }
    manager.emit_status();

    let mode = mode.to_string();
    let me = Arc::clone(manager);
    let app = app.clone();
    std::thread::Builder::new()
        .name("exit-flow".into())
        .spawn(move || {
            let timeout = if mode == "wait" {
                Duration::from_secs(300)
            } else {
                Duration::from_secs(10)
            };
            let exited = me.request_shutdown_and_wait(&mode, timeout);
            if !exited {
                // Force kill: Job Object + TerminateProcess fallback.
                me.log("warn", "exit flow: sidecar did not exit in time, force killing");
                me.force_kill();
                std::thread::sleep(Duration::from_millis(300));
            }
            me.log("info", &format!("exit flow: done (exited={exited}), exiting shell"));
            app.exit(0);
        })
        .expect("spawn exit flow thread");
}

pub fn cancel_exit_impl<R: Runtime>(_app: &AppHandle<R>, manager: &Arc<SidecarManager>) {
    {
        let mut inner = manager.state.inner.lock().unwrap();
        if !inner.quitting {
            return;
        }
        inner.quitting = false;
        inner.shutdown_pending = false;
        inner.shutdown_mode = None;
        // Reset the exit-done latch so a later quit waits again.
        *manager.state.exit_done.lock().unwrap() = false;
        drop(inner);
    }
    manager.request_shutdown_cancel();
    manager.emit_status();
}

// ---------------------------------------------------------------------
// command wrappers
// ---------------------------------------------------------------------

#[tauri::command]
pub fn begin_exit<R: Runtime>(
    app: AppHandle<R>,
    manager: State<'_, Arc<SidecarManager>>,
) -> ExitDecision {
    begin_exit_impl(&app, &manager)
}

#[tauri::command]
pub fn confirm_exit<R: Runtime>(
    app: AppHandle<R>,
    manager: State<'_, Arc<SidecarManager>>,
    mode: String,
) {
    if mode != "now" && mode != "wait" {
        return;
    }
    confirm_exit_impl(&app, &manager, &mode);
}

#[tauri::command]
pub fn cancel_exit<R: Runtime>(
    app: AppHandle<R>,
    manager: State<'_, Arc<SidecarManager>>,
) {
    cancel_exit_impl(&app, &manager);
}

// ---------------------------------------------------------------------
// 图片保存：弹出系统“另存为”对话框，由用户选择保存位置
// ---------------------------------------------------------------------

/// 解析 "data:image/png;base64,..." 或纯 base64 为字节。
fn decode_data_url(data_url: &str) -> Result<Vec<u8>, String> {
    let b64 = match data_url.split_once(",") {
        Some((meta, rest)) if meta.contains("base64") => rest,
        _ => data_url,
    };
    use base64::Engine as _;
    base64::engine::general_purpose::STANDARD
        .decode(b64.trim())
        .map_err(|e| format!("图片数据解码失败：{e}"))
}

/// 弹出系统保存对话框并写入图片字节。
/// 返回保存路径；用户取消时返回 "cancelled"。
#[tauri::command]
pub fn save_base64_image(app: AppHandle, data_url: String, filename: String) -> Result<String, String> {
    use tauri_plugin_dialog::DialogExt;
    let bytes = decode_data_url(&data_url)?;
    let file_path = app
        .dialog()
        .file()
        .set_file_name(&filename)
        .add_filter("PNG 图片", &["png"])
        .blocking_save_file();
    let path = match file_path {
        Some(p) => p,
        None => return Ok("cancelled".into()),
    };
    let path = match path.into_path() {
        Ok(p) => p,
        Err(_) => return Err("无法解析保存路径".into()),
    };
    std::fs::write(&path, &bytes).map_err(|e| format!("写入文件失败：{e}"))?;
    Ok(path.to_string_lossy().into_owned())
}

/// 目录选择结果（Rust 侧只取路径；是否可用由后端 re-resolve 验证）。
#[derive(Debug, Clone, Serialize)]
#[serde(rename_all = "camelCase")]
pub struct PickPathResult {
    #[serde(skip_serializing_if = "Option::is_none")]
    pub path: Option<String>,
    pub cancelled: bool,
}

/// 弹出系统目录选择框，让用户指定 ComfyUI 根目录。取消时 `cancelled=true`。
#[tauri::command]
pub fn pick_comfyui_root(app: AppHandle) -> PickPathResult {
    use tauri_plugin_dialog::DialogExt;
    let picked = app.dialog().file().blocking_pick_folder();
    match picked {
        Some(folder) => match folder.into_path() {
            Ok(p) => PickPathResult {
                path: Some(p.to_string_lossy().into_owned()),
                cancelled: false,
            },
            Err(_) => PickPathResult {
                path: None,
                cancelled: true,
            },
        },
        None => PickPathResult {
            path: None,
            cancelled: true,
        },
    }
}

/// 弹出系统文件选择框，让用户指定 ComfyUI 的 Python 可执行文件。
#[tauri::command]
pub fn pick_comfyui_python(app: AppHandle) -> PickPathResult {
    use tauri_plugin_dialog::DialogExt;
    let picked = app
        .dialog()
        .file()
        .set_title("选择 ComfyUI 的 Python 可执行文件")
        .add_filter("Python", &["exe", "bat", "cmd"])
        .blocking_pick_file();
    match picked {
        Some(file) => match file.into_path() {
            Ok(p) => PickPathResult {
                path: Some(p.to_string_lossy().into_owned()),
                cancelled: false,
            },
            Err(_) => PickPathResult {
                path: None,
                cancelled: true,
            },
        },
        None => PickPathResult {
            path: None,
            cancelled: true,
        },
    }
}

#[cfg(test)]
mod tests {
    use super::{ExitDecision, PickPathResult};

    #[test]
    fn exit_decision_serializes_as_flat_camel_case_contract() {
        let value = serde_json::to_value(ExitDecision::needs_choice(true, 2)).unwrap();
        assert_eq!(value["needsChoice"], true);
        assert_eq!(value["busy"], true);
        assert_eq!(value["tasks"], 2);
        assert!(value.get("needs_choice").is_none());
    }

    #[test]
    fn pick_path_result_serializes_camel_case() {
        let ok = serde_json::to_value(PickPathResult {
            path: Some("D:/ComfyUI".into()),
            cancelled: false,
        })
        .unwrap();
        assert_eq!(ok["path"], "D:/ComfyUI");
        assert_eq!(ok["cancelled"], false);
        assert!(ok.get("path").is_some());

        let cancelled = serde_json::to_value(PickPathResult {
            path: None,
            cancelled: true,
        })
        .unwrap();
        assert_eq!(cancelled["cancelled"], true);
        assert!(cancelled.get("path").is_none());
    }
}

// ---------------------------------------------------------------------
// 自动更新（Rust 侧下载/验证/安装，UI 只展示）
// ---------------------------------------------------------------------

#[tauri::command]
pub fn check_update(app: AppHandle, manager: tauri::State<'_, Arc<UpdateManager>>) -> crate::updater::UpdateState {
    let app2 = app.clone();
    let mgr = Arc::clone(manager.inner());
    std::thread::spawn(move || {
        mgr.check(&app2, true);
    });
    manager.snapshot()
}

#[tauri::command]
pub fn get_update_state(manager: tauri::State<'_, Arc<UpdateManager>>) -> crate::updater::UpdateState {
    manager.snapshot()
}

#[tauri::command]
pub fn download_update(app: AppHandle, manager: tauri::State<'_, Arc<UpdateManager>>) -> Result<(), String> {
    let app2 = app.clone();
    let mgr = Arc::clone(manager.inner());
    std::thread::spawn(move || {
        if let Err(e) = mgr.download_and_install(&app2) {
            let mut st = mgr.state.lock().unwrap();
            st.phase = UpdatePhase::Failed;
            st.error = Some(e.clone());
            drop(st);
            let _ = app2.emit("update-status", mgr.snapshot());
        }
    });
    Ok(())
}
