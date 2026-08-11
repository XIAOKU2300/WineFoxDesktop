//! WineFox desktop shell (Tauri 2).
//!
//! Responsibilities:
//!   * single instance (second launch focuses the existing window)
//!   * main window + system tray (show / hide / exit)
//!   * main-window close triggers immediate application shutdown
//!   * sidecar lifecycle (see sidecar.rs) + local startup/error pages
//!   * graceful sidecar cleanup with a force-kill timeout fallback

mod commands;
mod debug_mode;
mod handshake;
mod http_client;
mod job_object;
mod panel_protocol;
mod room_control;
mod sanitize;
mod sidecar;
mod state;
mod tray;
mod tunnel;
mod updater;
mod winhttp;

use sidecar::SidecarManager;
use state::AppState;
use std::sync::Arc;
use tauri::{Emitter, Manager};
use tauri_plugin_single_instance::init as single_instance_init;

pub fn run() {
    let state = AppState::new();
    let protocol_state = Arc::clone(&state);

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
        .plugin(tauri_plugin_dialog::init())
        .manage(state.clone())
        .manage(debug_mode::DebugMode::new())
        .register_asynchronous_uri_scheme_protocol(
            panel_protocol::PANEL_SCHEME,
            move |_ctx, request, responder| {
                let request_state = Arc::clone(&protocol_state);
                std::thread::spawn(move || {
                    responder.respond(panel_protocol::handle(&request_state, request));
                });
            },
        )
        .invoke_handler(tauri::generate_handler![
            commands::get_status,
            commands::start_sidecar,
            commands::restart_sidecar,
            commands::begin_exit,
            commands::confirm_exit,
            commands::cancel_exit,
            commands::save_base64_image,
            commands::check_update,
            commands::get_update_state,
            commands::download_update,
            debug_mode::record_logo_click,
            debug_mode::get_debug_mode,
            room_control::start_room,
            room_control::stop_room,
            room_control::get_room_status,
            room_control::set_room_tunnel,
            room_control::clear_room_tunnel,
            room_control::provision_room_tunnel,
            room_control::stop_room_tunnel,
            room_control::cloudflared_available,
            room_control::open_room_chatroom,
        ])
        .setup(move |app| {
            let manager = SidecarManager::new(app.handle().clone(), state.clone());
            app.manage(Arc::clone(&manager));
            let updater = updater::UpdateManager::new(sidecar::APP_VERSION);
            app.manage(Arc::clone(&updater));
            updater.start_auto_check(app.handle().clone());
            tray::build_tray(app.handle(), &manager)?;
            manager.start();
            Ok(())
        })
        .on_window_event(|window, event| {
            // 聊天室等辅助窗口可独立关闭，不触发退出。
            if window.label() != "main" {
                return;
            }
            // Closing the main window always exits the application. Keep the
            // event loop alive until the sidecar has shut down (or is force
            // killed by the existing timeout fallback).
            if let tauri::WindowEvent::CloseRequested { api, .. } = event {
                api.prevent_close();

                let manager = window.app_handle().state::<Arc<SidecarManager>>();
                let quitting = manager.state.inner.lock().unwrap().quitting;
                if !quitting {
                    commands::confirm_exit_impl(window.app_handle(), &manager, "now");
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
