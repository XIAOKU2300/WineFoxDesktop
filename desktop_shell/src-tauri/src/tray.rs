//! System tray: show / hide / exit.  Close-to-tray is handled in lib.rs.

use crate::commands::{begin_exit_impl, confirm_exit_impl, ExitDecision};
use crate::sidecar::SidecarManager;
use std::sync::Arc;
use tauri::menu::{Menu, MenuItem, PredefinedMenuItem};
use tauri::tray::{MouseButton, MouseButtonState, TrayIconBuilder, TrayIconEvent};
use tauri::{AppHandle, Emitter, Manager, Runtime};

pub const TRAY_SHOW: &str = "show";
pub const TRAY_HIDE: &str = "hide";
pub const TRAY_EXIT: &str = "exit";

pub fn build_tray<R: Runtime>(app: &AppHandle<R>, manager: &Arc<SidecarManager>) -> tauri::Result<()> {
    let show = MenuItem::with_id(app, TRAY_SHOW, "显示主窗口", true, None::<&str>)?;
    let hide = MenuItem::with_id(app, TRAY_HIDE, "隐藏窗口", true, None::<&str>)?;
    let exit = MenuItem::with_id(app, TRAY_EXIT, "退出 WineFox", true, None::<&str>)?;
    let menu = Menu::with_items(
        app,
        &[&show, &hide, &PredefinedMenuItem::separator(app)?, &exit],
    )?;

    let icon = app
        .default_window_icon()
        .cloned()
        .ok_or_else(|| tauri::Error::AssetNotFound("default window icon".into()))?;

    let mgr = Arc::clone(manager);

    TrayIconBuilder::with_id("winefox-tray")
        .icon(icon)
        .tooltip("WineFox")
        .menu(&menu)
        .show_menu_on_left_click(false)
        .on_menu_event(move |app, event| match event.id().as_ref() {
            TRAY_SHOW => {
                if let Some(w) = app.get_webview_window("main") {
                    let _ = w.show();
                    let _ = w.set_focus();
                }
            }
            TRAY_HIDE => {
                if let Some(w) = app.get_webview_window("main") {
                    let _ = w.hide();
                }
            }
            TRAY_EXIT => {
                // No tasks -> quit straight away (no JS round-trip).
                // Busy -> show the window and let the frontend modal decide.
                match begin_exit_impl(app, &mgr) {
                    ExitDecision::NeedsChoice { .. } => {
                        if let Some(w) = app.get_webview_window("main") {
                            let _ = w.show();
                            let _ = w.set_focus();
                        }
                        let _ = app.emit("exit-requested", ());
                    }
                    ExitDecision::Direct => {
                        confirm_exit_impl(app, &mgr, "now");
                    }
                }
            }
            _ => {}
        })
        .on_tray_icon_event(|tray, event| {
            if let TrayIconEvent::Click {
                button: MouseButton::Left,
                button_state: MouseButtonState::Up,
                ..
            } = event
            {
                let app = tray.app_handle();
                if let Some(w) = app.get_webview_window("main") {
                    let _ = w.show();
                    let _ = w.set_focus();
                }
            }
        })
        .build(app)?;

    Ok(())
}
