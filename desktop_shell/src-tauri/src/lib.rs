//! WineFox desktop shell (Tauri 2).
//!
//! Responsibilities:
//!   * single instance (second launch focuses the existing window)
//!   * main window + system tray (show / hide / exit)
//!   * close-to-tray
//!   * sidecar lifecycle (see sidecar.rs) + local startup/error pages
//!   * exit flow: no tasks -> quit; busy -> wait (default) / stop / return

mod commands;
mod handshake;
mod http_client;
mod job_object;
mod sanitize;
mod sidecar;
mod state;
mod tray;

use sidecar::SidecarManager;
use state::AppState;
use std::sync::Arc;
use tauri::{Emitter, Manager};
use tauri_plugin_single_instance::init as single_instance_init;

pub fn run() {
    let state = AppState::new();

    let app = tauri::Builder::default()
        // Single instance must be the first plugin.
        .plugin(single_instance_init(|app, _args, _cwd| {
            // Second launch: focus the existing window.
            if let Some(w) = app.get_webview_window("main") {
                let _ = w.show();
                let _ = w.set_focus();
            }
            let _ = app.emit("focus-requested", ());
        }))
        .manage(state.clone())
        .invoke_handler(tauri::generate_handler![
            commands::get_status,
            commands::start_sidecar,
            commands::restart_sidecar,
            commands::begin_exit,
            commands::confirm_exit,
            commands::cancel_exit,
        ])
        .setup(move |app| {
            let manager = SidecarManager::new(app.handle().clone(), state.clone());
            app.manage(Arc::clone(&manager));
            tray::build_tray(app.handle(), &manager)?;
            manager.start();
            Ok(())
        })
        .on_window_event(|window, event| {
            // Close-to-tray: the X button hides the window unless quitting.
            if let tauri::WindowEvent::CloseRequested { api, .. } = event {
                let quitting = window
                    .app_handle()
                    .state::<Arc<SidecarManager>>()
                    .state
                    .inner
                    .lock()
                    .unwrap()
                    .quitting;
                if !quitting {
                    api.prevent_close();
                    let _ = window.hide();
                }
            }
        })
        .build(tauri::generate_context!())
        .expect("failed to build WineFox shell");

    app.run(|app_handle, event| {
        if let tauri::RunEvent::Exit = event {
            // Best-effort cleanup: terminate the sidecar process if it is
            // still alive.  The Job Object is the primary mechanism
            // (kill-on-close); this covers job-less fallback paths.
            let mgr = app_handle.state::<Arc<SidecarManager>>();
            if mgr.is_running() {
                mgr.force_kill();
            }
        }
    });
}
