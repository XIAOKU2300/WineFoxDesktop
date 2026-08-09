//! Tauri commands exposed to the frontend + the shared exit-flow logic
//! (also used by the tray).

use crate::sidecar::{SidecarManager, APP_VERSION};
use crate::state::AppState;
use serde::Serialize;
use std::sync::Arc;
use std::time::Duration;
use tauri::{AppHandle, Runtime, State};

/// Result of evaluating an exit request.
#[derive(Debug, Clone, Serialize)]
#[serde(rename_all = "camelCase")]
pub enum ExitDecision {
    /// Sidecar is busy: the UI must ask wait / stop-waiting / return.
    NeedsChoice { busy: bool, tasks: u32 },
    /// Nothing running: quit directly.
    Direct,
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
            return ExitDecision::Direct;
        }
    }
    let busy = manager.state.inner.lock().unwrap().busy;
    let tasks = manager.state.inner.lock().unwrap().tasks;
    let running = manager.is_running();
    let _ = app;
    if running && busy && tasks > 0 {
        ExitDecision::NeedsChoice { busy, tasks }
    } else {
        ExitDecision::Direct
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
