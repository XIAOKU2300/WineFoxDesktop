//! Shared application state for the WineFox shell.

use serde::Serialize;
use std::sync::{Arc, Condvar, Mutex};

/// High-level sidecar lifecycle state (mirrored to the frontend).
#[derive(Debug, Clone, Copy, PartialEq, Eq, Serialize, Default)]
#[serde(rename_all = "camelCase")]
pub enum SidecarState {
    #[default]
    Stopped,
    Starting,
    Ready,
    Error,
}

#[derive(Debug, Clone, Serialize)]
#[serde(rename_all = "camelCase")]
pub struct AppStatus {
    pub state: SidecarState,
    pub version: String,
    pub protocol: u32,
    pub port: Option<u16>,
    pub panel_url: Option<String>,
    pub last_error: Option<String>,
    pub busy: bool,
    pub tasks: u32,
    pub restart_count: u32,
    pub quitting: bool,
    pub shutdown_pending: bool,
    pub shutdown_mode: Option<String>,
    pub log_tail: Vec<String>,
    pub pid: Option<u32>,
}

#[derive(Debug, Default)]
pub struct Inner {
    pub state: SidecarState,
    pub last_error: Option<String>,
    pub port: Option<u16>,
    pub bootstrap_path: Option<String>,
    /// Session cookie from the real backend's `/bootstrap/<token>` handshake.
    pub session_cookie: Option<String>,
    /// True when the sidecar serves the panel UI at `/` (real backend);
    /// false keeps the legacy bootstrap-path mount used by the fake sidecar.
    pub panel_root: bool,
    pub sidecar_version: Option<String>,
    pub pid: Option<u32>,
    pub busy: bool,
    pub tasks: u32,
    pub restart_count: u32,
    pub quitting: bool,
    pub shutdown_pending: bool,
    pub shutdown_mode: Option<String>,
    /// Ring buffer of the last sanitized sidecar stderr lines.
    pub log_tail: Vec<String>,
}

impl Inner {
    pub fn push_log(&mut self, line: String) {
        if self.log_tail.len() >= 50 {
            self.log_tail.remove(0);
        }
        self.log_tail.push(line);
    }
}

/// App-wide state: the frontend-facing snapshot plus coordination primitives
/// used by the sidecar manager.
pub struct AppState {
    pub inner: Mutex<Inner>,
    /// Signalled whenever the sidecar exits (for the exit flow waiter).
    pub exit_done: Mutex<bool>,
    pub exit_cond: Condvar,
}

impl AppState {
    pub fn new() -> Arc<Self> {
        Arc::new(AppState {
            inner: Mutex::new(Inner::default()),
            exit_done: Mutex::new(false),
            exit_cond: Condvar::new(),
        })
    }

    pub fn snapshot(&self, version: &str, protocol: u32) -> AppStatus {
        let inner = self.inner.lock().unwrap();
        let panel_url = match (inner.port, &inner.bootstrap_path) {
            (Some(port), Some(path)) => {
                if inner.panel_root {
                    Some(format!("http://winefox-panel.localhost/{port}/"))
                } else {
                    let mut p = path.clone();
                    if !p.ends_with('/') {
                        p.push('/');
                    }
                    Some(format!("http://winefox-panel.localhost/{port}{p}"))
                }
            }
            _ => None,
        };
        AppStatus {
            state: inner.state,
            version: version.to_string(),
            protocol,
            port: inner.port,
            panel_url,
            last_error: inner.last_error.clone(),
            busy: inner.busy,
            tasks: inner.tasks,
            restart_count: inner.restart_count,
            quitting: inner.quitting,
            shutdown_pending: inner.shutdown_pending,
            shutdown_mode: inner.shutdown_mode.clone(),
            log_tail: inner.log_tail.clone(),
            pid: inner.pid,
        }
    }
}
