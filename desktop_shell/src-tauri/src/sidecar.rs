//! Sidecar process lifecycle manager.
//!
//! Responsibilities:
//!   * resolve the sidecar binary (env override -> bundled exe -> dev script)
//!   * spawn with piped stdout/stderr/stdin
//!   * assign the child to a kill-on-parent-exit Job Object
//!   * read stdout line by line, validate the ready handshake (protocol +
//!     version + loopback) with a ready timeout
//!   * verify the HTTP surface via a loopback health check
//!   * forward status/exit events to the UI
//!   * crash detection + auto-restart with backoff
//!   * stderr sanitization into a ring buffer + log file
//!   * control commands over child stdin (ping / status / shutdown)

use crate::handshake::{self, Line, ReadyInfo};
use crate::job_object::{JobGuard, JobObject};
use crate::sanitize;
use crate::state::{AppState, SidecarState};
use serde_json::json;
use std::io::{BufRead, BufReader, Write};
use std::process::{Child, ChildStdin, Command, Stdio};
use std::sync::mpsc::{self, Receiver, Sender};
use std::sync::{Arc, Mutex};
use std::time::{Duration, Instant};
use tauri::{AppHandle, Emitter};

pub const APP_VERSION: &str = env!("CARGO_PKG_VERSION");

const READY_TIMEOUT: Duration = Duration::from_secs(15);
const RESTART_BACKOFF_MS: u64 = 800;

enum SessionEvent {
    Line(String),
    StdoutClosed,
}

pub struct SidecarManager {
    pub state: Arc<AppState>,
    pub app: AppHandle,
    child: Mutex<Option<Child>>,
    stdin: Mutex<Option<ChildStdin>>,
    job: JobGuard,
    tx: Sender<SessionEvent>,
    rx: Mutex<Receiver<SessionEvent>>,
}

impl SidecarManager {
    pub fn new(app: AppHandle, state: Arc<AppState>) -> Arc<SidecarManager> {
        let (tx, rx) = mpsc::channel::<SessionEvent>();
        Arc::new(SidecarManager {
            state,
            app,
            child: Mutex::new(None),
            stdin: Mutex::new(None),
            job: JobGuard::new(),
            tx,
            rx: Mutex::new(rx),
        })
    }

    // ------------------------------------------------------------------
    // logging
    // ------------------------------------------------------------------

    fn log_file(&self) -> Option<std::fs::File> {
        let dir = std::env::var("WINE_FOX_LOG_DIR").unwrap_or_else(|_| {
            let base = std::env::var("LOCALAPPDATA").unwrap_or_else(|_| ".".into());
            format!("{base}\\WineFox\\logs")
        });
        let _ = std::fs::create_dir_all(&dir);
        std::fs::OpenOptions::new()
            .create(true)
            .append(true)
            .open(format!("{dir}\\winefox.log"))
            .ok()
    }

    pub fn log(&self, level: &str, msg: &str) {
        let line = format!("[{}] [{level}] {msg}", local_timestamp());
        if let Some(mut f) = self.log_file() {
            let _ = writeln!(f, "{line}");
        }
        if cfg!(debug_assertions) {
            eprintln!("{line}");
        }
    }

    // ------------------------------------------------------------------
    // binary resolution
    // ------------------------------------------------------------------

    /// Resolve (program, args) for the sidecar.  Priority:
    ///   1. WINE_FOX_SIDECAR env var (exe or .py script)
    ///   2. bundled binary next to the shell exe (winefox-sidecar.exe)
    ///   3. src-tauri/binaries/winefox-sidecar.exe (dev tree)
    ///   4. dev fallback: python -m winefox_sidecar from the source tree
    fn resolve(&self) -> Result<(String, Vec<String>), String> {
        if let Ok(over) = std::env::var("WINE_FOX_SIDECAR") {
            let over = over.trim().to_string();
            if !over.is_empty() {
                if over.to_lowercase().ends_with(".py") {
                    let py = std::env::var("WINE_FOX_PYTHON").unwrap_or_else(|_| "python".into());
                    return Ok((py, vec![over, "--allow-simulate".to_string()]));
                }
                return Ok((over, vec!["--allow-simulate".to_string()]));
            }
        }

        let exe_dir = std::env::current_exe()
            .ok()
            .and_then(|p| p.parent().map(|d| d.to_path_buf()));
        if let Some(dir) = &exe_dir {
            let cand = dir.join("winefox-sidecar.exe");
            if cand.is_file() {
                return Ok((
                    cand.to_string_lossy().into_owned(),
                    vec!["--allow-simulate".to_string()],
                ));
            }
        }

        // Dev tree: <repo>/desktop_shell/sidecar/...
        let dev_pkg = std::path::Path::new(env!("CARGO_MANIFEST_DIR"))
            .join("sidecar")
            .join("winefox_sidecar");
        if dev_pkg.join("__main__.py").is_file() {
            let entry = dev_pkg.join("__main__.py");
            let py = std::env::var("WINE_FOX_PYTHON").unwrap_or_else(|_| "python".into());
            return Ok((
                py,
                vec![entry.to_string_lossy().into_owned(), "--allow-simulate".to_string()],
            ));
        }

        Err("sidecar binary not found: set WINE_FOX_SIDECAR or run packaging/build.ps1".into())
    }

    // ------------------------------------------------------------------
    // lifecycle
    // ------------------------------------------------------------------

    /// Begin a session on a background thread.  Safe to call again for
    /// auto-restart and manual retry.
    pub fn start(self: &Arc<Self>) {
        self.spawn_session();
    }

    fn spawn_session(self: &Arc<Self>) {
        {
            let mut inner = self.state.inner.lock().unwrap();
            inner.state = SidecarState::Starting;
            inner.last_error = None;
            inner.port = None;
            inner.bootstrap_path = None;
            inner.pid = None;
            inner.busy = false;
            inner.tasks = 0;
            inner.shutdown_pending = false;
            inner.shutdown_mode = None;
        }
        self.emit_status();
        let me = Arc::clone(self);
        std::thread::Builder::new()
            .name("sidecar-session".into())
            .spawn(move || session_main(&me))
            .expect("failed to spawn session thread");
    }

    // ------------------------------------------------------------------
    // exit / crash handling
    // ------------------------------------------------------------------

    fn on_sidecar_exit(self: &Arc<Self>, code: i32, reason: &str) {
        let quitting = {
            let mut inner = self.state.inner.lock().unwrap();
            let quitting = inner.quitting;
            inner.state = SidecarState::Stopped;
            inner.busy = false;
            inner.tasks = 0;
            inner.pid = None;
            quitting
        };
        self.terminate_child();

        if quitting {
            self.log("info", "sidecar exited during quit flow");
            self.signal_exit_done();
            return;
        }

        // Unexpected exit: crash or self-exit -> auto-restart with backoff.
        let restart_count = {
            let mut inner = self.state.inner.lock().unwrap();
            inner.restart_count += 1;
            inner.restart_count
        };
        if restart_count <= self.max_restarts() {
            let backoff = Duration::from_millis(RESTART_BACKOFF_MS * restart_count as u64);
            self.log(
                "warn",
                &format!("sidecar 意外退出（code={code}, {reason}），{}ms 后自动重启（第 {restart_count} 次）", backoff.as_millis()),
            );
            self.emit_status();
            std::thread::sleep(backoff);
            let quitting = self.state.inner.lock().unwrap().quitting;
            if !quitting {
                self.spawn_session();
            }
        } else {
            self.fail(format!(
                "sidecar 崩溃（退出码 {code}，{reason}）。可点击“重试启动”。"
            ));
        }
    }

    /// The manager is always held as Arc<SidecarManager>.

    fn fail(&self, msg: String) {
        {
            let mut inner = self.state.inner.lock().unwrap();
            inner.state = SidecarState::Error;
            inner.last_error = Some(msg.clone());
            inner.busy = false;
            inner.tasks = 0;
        }
        self.log("error", &msg);
        self.terminate_child();
        self.emit_status();
    }

    fn signal_exit_done(&self) {
        let mut done = self.state.exit_done.lock().unwrap();
        *done = true;
        self.state.exit_cond.notify_all();
    }

    fn terminate_child(&self) {
        let mut guard = self.child.lock().unwrap();
        if let Some(mut child) = guard.take() {
            let _ = child.kill();
            let _ = child.wait();
        }
        *self.stdin.lock().unwrap() = None;
    }

    // ------------------------------------------------------------------
    // control
    // ------------------------------------------------------------------

    pub fn send_command(&self, obj: serde_json::Value) -> bool {
        let mut guard = self.stdin.lock().unwrap();
        match guard.as_mut() {
            Some(stdin) => {
                let line = serde_json::to_string(&obj).unwrap_or_default();
                let ok = stdin
                    .write_all(line.as_bytes())
                    .and_then(|_| stdin.write_all(b"\n"))
                    .and_then(|_| stdin.flush());
                ok.is_ok()
            }
            None => false,
        }
    }

    pub fn is_running(&self) -> bool {
        matches!(
            self.state.inner.lock().unwrap().state,
            SidecarState::Starting | SidecarState::Ready
        )
    }

    /// Force-terminate the sidecar process (fallback when graceful shutdown
    /// times out).  The Job Object kills the whole tree when this shell exits;
    /// this call covers the case where we want it dead right now.
    pub fn force_kill(&self) {
        let pid = self.state.inner.lock().unwrap().pid;
        self.terminate_child();
        if let Some(pid) = pid {
            let _ = crate::job_object::JobObject::terminate_process(pid);
        }
    }

    // ------------------------------------------------------------------
    // exit flow support (called from commands)
    // ------------------------------------------------------------------

    /// Send shutdown to the sidecar and wait for it to exit.
    /// Returns true if the sidecar exited by itself (or wasn't running).
    pub fn request_shutdown_and_wait(&self, mode: &str, timeout: Duration) -> bool {
        if !self.is_running() {
            return true;
        }
        self.log("info", &format!("exit flow: sending shutdown mode={mode}"));
        let _ = self.send_command(json!({"cmd": "shutdown", "mode": mode}));
        let done = self.state.exit_done.lock().unwrap();
        let (guard, _) = self
            .state
            .exit_cond
            .wait_timeout(done, timeout)
            .unwrap_or_else(|poisoned| poisoned.into_inner());
        *guard
    }

    pub fn request_shutdown_cancel(&self) {
        let _ = self.send_command(json!({"cmd": "shutdown-cancel"}));
        {
            let mut inner = self.state.inner.lock().unwrap();
            inner.shutdown_pending = false;
            inner.shutdown_mode = None;
        }
        self.log("info", "exit flow: shutdown cancelled (return to app)");
        self.emit_status();
    }

    // ------------------------------------------------------------------
    // helpers
    // ------------------------------------------------------------------

    pub fn emit_status(&self) {
        let st = self.state.snapshot(APP_VERSION, handshake::PROTOCOL);
        let _ = self.app.emit("sidecar-status", &st);
    }

    fn max_restarts(&self) -> u32 {
        std::env::var("WINE_FOX_MAX_RESTARTS")
            .ok()
            .and_then(|v| v.parse().ok())
            .unwrap_or(2)
    }
}

fn ready_timeout() -> Duration {
    std::env::var("WINE_FOX_READY_TIMEOUT_MS")
        .ok()
        .and_then(|v| v.parse::<u64>().ok())
        .map(Duration::from_millis)
        .unwrap_or(READY_TIMEOUT)
}

// ---------------------------------------------------------------------
// session main loop
// ---------------------------------------------------------------------

fn session_main(me: &Arc<SidecarManager>) {
    me.log("info", "session: resolving sidecar");
    let (program, args) = match me.resolve() {
        Ok(v) => v,
        Err(e) => {
            me.fail(format!("无法解析 sidecar: {e}"));
            return;
        }
    };

    let mut cmd = Command::new(&program);
    cmd.args(&args)
        .stdin(Stdio::piped())
        .stdout(Stdio::piped())
        .stderr(Stdio::piped())
        .env("PYTHONUTF8", "1")
        .env("PYTHONIOENCODING", "utf-8");
    let mut child = match cmd.spawn() {
        Ok(c) => c,
        Err(e) => {
            me.fail(format!("启动 sidecar 失败: {e}"));
            return;
        }
    };
    let pid = child.id();
    me.log("info", &format!("session: spawned {program} pid={pid}"));

    // Kill-on-parent-exit via Job Object (best effort; the sidecar's
    // stdin-EOF watchdog is the backstop).
    match JobObject::create() {
        Ok(job) => match job.assign(pid) {
            Ok(()) => {
                me.job.install(job);
                me.log("info", "session: assigned to kill-on-parent-exit job");
            }
            Err(e) => me.log(
                "warn",
                &format!("session: job assign failed ({e}); relying on stdin-EOF watchdog"),
            ),
        },
        Err(e) => me.log(
            "warn",
            &format!("session: job create failed ({e}); relying on stdin-EOF watchdog"),
        ),
    }

    let stdout = child.stdout.take().expect("stdout piped");
    let stderr = child.stderr.take().expect("stderr piped");
    let stdin = child.stdin.take().expect("stdin piped");
    *me.child.lock().unwrap() = Some(child);
    *me.stdin.lock().unwrap() = Some(stdin);

    let tx = me.tx.clone();
    std::thread::spawn(move || {
        for line in BufReader::new(stdout).lines() {
            match line {
                Ok(l) => {
                    if tx.send(SessionEvent::Line(l)).is_err() {
                        break;
                    }
                }
                Err(_) => break,
            }
        }
        let _ = tx.send(SessionEvent::StdoutClosed);
    });

    let me2 = Arc::clone(me);
    std::thread::spawn(move || {
        for line in BufReader::new(stderr).lines() {
            match line {
                Ok(l) => {
                    let clean = sanitize::sanitize_line(&l);
                    me2.state.inner.lock().unwrap().push_log(clean.clone());
                    me2.log("sidecar-stderr", &clean);
                }
                Err(_) => break,
            }
        }
    });

    let rx = me.rx.lock().unwrap();
    let boot_deadline = Instant::now() + ready_timeout();

    loop {
        let state_now = me.state.inner.lock().unwrap().state;
        let remaining = boot_deadline.saturating_duration_since(Instant::now());
        let ev = if state_now == SidecarState::Starting {
            rx.recv_timeout(remaining)
        } else {
            rx.recv_timeout(Duration::from_millis(300))
        };

        match ev {
            Ok(SessionEvent::Line(raw)) => {
                match handshake::parse_line(&raw) {
                    None => {
                        let clean = sanitize::sanitize_line(&raw);
                        me.log("warn", &format!("ignored non-JSON stdout line: {clean}"));
                    }
                    Some(Line::Ready(info)) => {
                        let st = me.state.inner.lock().unwrap().state;
                        if st == SidecarState::Starting {
                            if let Err(e) = on_ready(me, &info, pid) {
                                me.fail(e);
                                return;
                            }
                        }
                    }
                    Some(Line::Status { busy, tasks }) => {
                        let mut inner = me.state.inner.lock().unwrap();
                        inner.busy = busy;
                        inner.tasks = tasks;
                        drop(inner);
                        me.emit_status();
                    }
                    Some(Line::Log { level, message }) => {
                        me.log(
                            &format!("sidecar[{level}]"),
                            &sanitize::sanitize_line(&message),
                        );
                    }
                    Some(Line::Ack { cmd, ok }) => {
                        me.log("info", &format!("sidecar ack: {cmd} ok={ok}"));
                        if cmd == "shutdown" {
                            let mut inner = me.state.inner.lock().unwrap();
                            inner.shutdown_pending = true;
                            inner.shutdown_mode =
                                Some(if inner.quitting { "now".into() } else { "wait".into() });
                            drop(inner);
                            me.emit_status();
                        }
                    }
                    Some(Line::Exit { code, reason }) => {
                        me.log("info", &format!("sidecar exit event: code={code} reason={reason}"));
                        me.on_sidecar_exit(code, &reason);
                        return;
                    }
                    Some(Line::Other(v)) => {
                        me.log("debug", &format!("sidecar event: {v}"));
                    }
                }
            }
            Ok(SessionEvent::StdoutClosed) => {
                me.log("warn", "sidecar stdout closed");
                me.on_sidecar_exit(-1, "stdout-closed");
                return;
            }
            Err(mpsc::RecvTimeoutError::Timeout) => {
                let st = me.state.inner.lock().unwrap().state;
                if st == SidecarState::Starting && Instant::now() > boot_deadline {
                    me.fail(format!("等待 ready 握手超时（{}s）", ready_timeout().as_secs()));
                    return;
                }
            }
            Err(mpsc::RecvTimeoutError::Disconnected) => {
                me.log("warn", "session event channel closed");
                return;
            }
        }
    }
}

fn on_ready(me: &Arc<SidecarManager>, info: &ReadyInfo, pid: u32) -> Result<(), String> {
    handshake::validate_ready(info, APP_VERSION).map_err(|e| match e {
        handshake::ReadyError::ProtocolMismatch { got, expected } => {
            format!("协议不匹配：sidecar 上报 protocol={got}，期望 {expected}")
        }
        handshake::ReadyError::VersionMismatch { got, expected } => {
            format!("版本不匹配：sidecar 上报 version={got}，期望 {expected}（主程序与 sidecar 必须同版本）")
        }
        handshake::ReadyError::NotLoopback { host } => {
            format!("sidecar 上报非 loopback 地址：{host}")
        }
    })?;

    // Loopback health check before navigating.
    let url = format!("http://127.0.0.1:{}/api/health", info.port);
    match crate::http_client::get_json(&url, Duration::from_secs(3)) {
        Ok(Some(v)) if v.get("ok").and_then(|b| b.as_bool()) == Some(true) => {}
        Ok(Some(v)) => return Err(format!("sidecar health 检查未通过：{v}")),
        Ok(None) => return Err(format!("sidecar health 检查无响应（{url}）")),
        Err(e) => return Err(format!("sidecar health 检查失败：{e}")),
    }

    let mut inner = me.state.inner.lock().unwrap();
    inner.state = SidecarState::Ready;
    inner.port = Some(info.port);
    inner.bootstrap_path = Some(info.bootstrap_path.clone());
    inner.sidecar_version = Some(info.version.clone());
    inner.pid = Some(pid);
    inner.last_error = None;
    drop(inner);
    me.log("info", &format!("sidecar ready: 127.0.0.1:{}", info.port));
    me.emit_status();
    Ok(())
}

// ---------------------------------------------------------------------
// tiny local timestamp (no chrono dependency)
// ---------------------------------------------------------------------

fn local_timestamp() -> String {
    let now = std::time::SystemTime::now()
        .duration_since(std::time::UNIX_EPOCH)
        .unwrap_or_default()
        .as_secs();
    let days = now / 86400;
    let (y, m, d) = civil_from_days(days as i64);
    let secs = now % 86400;
    format!(
        "{y:04}-{m:02}-{d:02} {:02}:{:02}:{:02}",
        secs / 3600,
        (secs % 3600) / 60,
        secs % 60
    )
}

/// Convert days since epoch to (year, month, day) — Howard Hinnant's algorithm.
fn civil_from_days(z: i64) -> (i64, u32, u32) {
    let z = z + 719468;
    let era = if z >= 0 { z } else { z - 146096 } / 146097;
    let doe = (z - era * 146097) as u64;
    let yoe = (doe - doe / 1460 + doe / 36524 - doe / 146096) / 365;
    let y = yoe as i64 + era * 400;
    let doy = doe - (365 * yoe + yoe / 4 - yoe / 100);
    let mp = (5 * doy + 2) / 153;
    let d = (doy - (153 * mp + 2) / 5 + 1) as u32;
    let m = if mp < 10 { mp + 3 } else { mp - 9 } as u32;
    (if m <= 2 { y + 1 } else { y }, m, d)
}