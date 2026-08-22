//! 软件内自动更新（自研，不依赖 tauri-updater 的安装包格式）。
//!
//! 分工：业务 UI（sidecar iframe）只发请求与展示；下载、签名验证、启动
//! 安装器全部在 Shell/Rust 完成；Python sidecar 不参与更新。
//!
//! 状态机：idle → checking → up_to_date / available / failed
//!         → downloading → verifying → installing → restarting
//!
//! 更新源：GitHub Release 静态 latest.json + Ed25519 签名（硬要求）。
//! 公钥编译期内嵌仓库 `packaging/updater/pubkey.b64`（真实发布公钥，仅提交公钥），
//! 可被环境变量 `WINE_FOX_UPDATE_PUBKEY`（base64）覆盖；私钥只在发布机，绝不入库。

use crate::winhttp;
use crate::SidecarManager;
use base64::Engine as _;
use ed25519_dalek::Verifier;
use serde::{Deserialize, Serialize};
use std::path::PathBuf;
use std::sync::{Arc, Mutex};
use std::time::Duration;
use tauri::{AppHandle, Emitter, Manager};

pub const UPDATE_FEED_URL: &str =
    "https://github.com/XIAOKU2300/WineFoxDesktop/releases/latest/download/latest.json";
const CHECK_INTERVAL: Duration = Duration::from_secs(24 * 3600); // 自动检查间隔 24h
const FIRST_CHECK_DELAY: Duration = Duration::from_secs(5); // 启动 5 秒后首次检查

#[derive(Debug, Clone, Copy, PartialEq, Eq, Serialize)]
#[serde(rename_all = "camelCase")]
pub enum UpdatePhase {
    Idle,
    Checking,
    UpToDate,
    Available,
    Failed,
    Downloading,
    Verifying,
    Installing,
    Restarting,
}

#[derive(Debug, Clone, Serialize)]
#[serde(rename_all = "camelCase")]
pub struct UpdateState {
    pub phase: UpdatePhase,
    pub current_version: String,
    pub new_version: Option<String>,
    pub notes: Option<String>,
    pub pub_date: Option<String>,
    pub size_bytes: Option<u64>,
    pub downloaded_bytes: u64,
    pub total_bytes: u64,
    pub speed_bps: u64,
    pub error: Option<String>,
    pub last_checked: Option<String>,
    pub can_install: bool,
    pub busy_tasks: u32,
    pub feed_url: String,
}

impl UpdateState {
    fn idle(version: &str, feed_url: &str) -> Self {
        Self {
            phase: UpdatePhase::Idle,
            current_version: version.to_string(),
            new_version: None,
            notes: None,
            pub_date: None,
            size_bytes: None,
            downloaded_bytes: 0,
            total_bytes: 0,
            speed_bps: 0,
            error: None,
            last_checked: None,
            can_install: false,
            busy_tasks: 0,
            feed_url: feed_url.to_string(),
        }
    }
}

#[derive(Debug, Clone, Deserialize)]
struct UpdateFeed {
    version: String,
    #[serde(default)]
    notes: Option<String>,
    #[serde(default)]
    pub_date: Option<String>,
    platforms: UpdatePlatforms,
}

#[derive(Debug, Clone, Deserialize)]
struct UpdatePlatforms {
    #[serde(rename = "windows-x86_64")]
    windows_x86_64: WindowsRelease,
}

#[derive(Debug, Clone, Deserialize)]
struct WindowsRelease {
    url: String,
    signature: String,
    size: u64,
}

pub struct UpdateManager {
    pub state: Mutex<UpdateState>,
    feed: Mutex<Option<UpdateFeed>>,
}

impl UpdateManager {
    pub fn new(version: &str) -> Arc<Self> {
        Arc::new(Self {
            state: Mutex::new(UpdateState::idle(version, resolve_feed_url())),
            feed: Mutex::new(None),
        })
    }

    pub fn snapshot(&self) -> UpdateState {
        self.state.lock().unwrap().clone()
    }

    fn emit(&self, app: &AppHandle) {
        let _ = app.emit("update-status", self.snapshot());
    }

    pub fn sync_tasks(&self, tasks: u32) -> UpdateState {
        let mut st = self.state.lock().unwrap();
        st.busy_tasks = tasks;
        st.can_install = tasks == 0 && st.phase == UpdatePhase::Available;
        st.clone()
    }

    /// Reserve the update state before a worker thread is spawned. This makes
    /// rapid repeated clicks idempotent instead of starting competing workers.
    pub fn begin_check(&self, app: &AppHandle) -> bool {
        let started = {
            let mut st = self.state.lock().unwrap();
            if matches!(
                st.phase,
                UpdatePhase::Checking
                    | UpdatePhase::Downloading
                    | UpdatePhase::Verifying
                    | UpdatePhase::Installing
                    | UpdatePhase::Restarting
            ) {
                false
            } else {
                st.phase = UpdatePhase::Checking;
                st.error = None;
                st.can_install = false;
                true
            }
        };
        if started {
            self.emit(app);
        }
        started
    }

    /// 启动定时检查：5 秒后一次，之后每 24h 一次。失败静默（只记录状态）。
    pub fn start_auto_check(self: &Arc<Self>, app: AppHandle) {
        let me = Arc::clone(self);
        std::thread::spawn(move || {
            std::thread::sleep(FIRST_CHECK_DELAY);
            me.check(&app);
            loop {
                std::thread::sleep(CHECK_INTERVAL);
                me.check(&app);
            }
        });
    }

    pub fn check(self: &Arc<Self>, app: &AppHandle) -> UpdateState {
        if !self.begin_check(app) {
            return self.snapshot();
        }
        self.finish_check(app)
    }

    /// Execute a check previously reserved by `begin_check`.
    pub fn finish_check(&self, app: &AppHandle) -> UpdateState {
        let feed_url = self.state.lock().unwrap().feed_url.clone();
        // 拉取清单做一次快速重试：GitHub 在代理/弱网环境常见瞬时重置，
        // 第二次往往就通了；清单很小，重试成本可忽略。
        let result = (|| -> Result<UpdateFeed, String> {
            let mut last = String::from("未知错误");
            for attempt in 0..2 {
                if attempt > 0 {
                    std::thread::sleep(Duration::from_millis(1500));
                }
                match winhttp::https_get(&feed_url, Duration::from_secs(15), None) {
                    Ok(body) => return parse_feed(&body),
                    Err(e) => last = e,
                }
            }
            Err(format!("{last}（已重试一次仍失败，若在代理环境请确认系统代理已开启）"))
        })();

        let mut cached_feed = self.feed.lock().unwrap();
        let mut st = self.state.lock().unwrap();
        st.last_checked = Some(now_rfc3339());
        match result {
            Ok(feed) => {
                let version = feed.version.clone();
                let current = st.current_version.clone();
                let newer = compare_versions(&version, &current).is_gt();
                if newer {
                    st.phase = UpdatePhase::Available;
                    st.new_version = Some(version);
                    st.notes = feed.notes.clone();
                    st.pub_date = feed.pub_date.clone();
                    st.size_bytes = Some(feed.platforms.windows_x86_64.size);
                    st.can_install = st.busy_tasks == 0;
                    *cached_feed = Some(feed);
                } else {
                    st.phase = UpdatePhase::UpToDate;
                    st.new_version = None;
                    st.notes = None;
                    st.pub_date = feed.pub_date;
                    st.size_bytes = None;
                    st.can_install = false;
                    *cached_feed = None;
                }
            }
            Err(e) => {
                st.phase = UpdatePhase::Failed;
                st.error = Some(e);
                st.can_install = false;
                *cached_feed = None;
            }
        }
        let snapshot = st.clone();
        drop(st);
        self.emit(app);
        snapshot
    }

    pub fn begin_download(&self, app: &AppHandle, busy_tasks: u32) -> Result<(), String> {
        let result = {
            let mut st = self.state.lock().unwrap();
            st.busy_tasks = busy_tasks;
            st.can_install = busy_tasks == 0 && st.phase == UpdatePhase::Available;
            if busy_tasks > 0 {
                Err(format!("当前还有 {busy_tasks} 个任务，完成或取消后再更新"))
            } else if st.phase != UpdatePhase::Available {
                Err("当前没有可用的更新".into())
            } else {
                st.phase = UpdatePhase::Downloading;
                st.downloaded_bytes = 0;
                st.total_bytes = st.size_bytes.unwrap_or(0);
                st.speed_bps = 0;
                st.error = None;
                st.can_install = false;
                Ok(())
            }
        };
        self.emit(app);
        result
    }

    /// 下载并安装已经检查过的版本。使用缓存清单，避免检查和下载之间
    /// latest Release 发生变化时安装与 UI 展示不一致的版本。
    pub fn download_and_install(self: &Arc<Self>, app: &AppHandle) -> Result<(), String> {
        let feed = self
            .feed
            .lock()
            .unwrap()
            .clone()
            .ok_or_else(|| "更新清单已失效，请重新检查更新".to_string())?;
        let win = feed.platforms.windows_x86_64;
        let exe_url = win.url;
        let signature = win.signature;
        let size_hint = win.size;

        let download_dir = app_data_dir();
        std::fs::create_dir_all(&download_dir).map_err(|e| format!("无法创建更新目录：{e}"))?;
        let installer_path = download_dir.join("WineFox-Setup-latest-x64.exe");
        let _ = std::fs::remove_file(&installer_path);

        // 流式下载（进度回调更新状态并推送事件）
        let me = Arc::clone(self);
        let app2 = app.clone();
        let start = std::time::Instant::now();
        let mut last_emitted = std::time::Instant::now();
        let mut progress = move |downloaded: u64, total: u64| {
            let mut st = me.state.lock().unwrap();
            st.downloaded_bytes = downloaded;
            if total > 0 {
                st.total_bytes = total;
            } else if st.total_bytes == 0 {
                st.total_bytes = size_hint;
            }
            let elapsed = start.elapsed().as_secs_f64().max(0.001);
            st.speed_bps = (downloaded as f64 / elapsed) as u64;
            let snapshot = st.clone();
            drop(st);
            if last_emitted.elapsed() >= Duration::from_millis(200) {
                last_emitted = std::time::Instant::now();
                let _ = app2.emit("update-status", snapshot);
            }
        };

        let body = winhttp::https_get(&exe_url, Duration::from_secs(600), Some(&mut progress))
            .map_err(|e| format!("安装包下载失败：{e}"))?;
        if body.len() as u64 != size_hint {
            return Err(format!(
                "安装包大小不匹配：预期 {size_hint} 字节，实际 {} 字节",
                body.len()
            ));
        }
        std::fs::write(&installer_path, &body).map_err(|e| format!("写入安装包失败：{e}"))?;

        // 签名验证（硬要求）：签名错误直接终止，绝不提供强制安装
        {
            let mut st = self.state.lock().unwrap();
            st.phase = UpdatePhase::Verifying;
            st.speed_bps = 0;
        }
        self.emit(app);

        verify_installer_signature(resolve_pubkey_b64(), &signature, &body).map_err(|e| {
            // 校验失败时清理磁盘上未经验证的文件，避免留毒。
            let _ = std::fs::remove_file(&installer_path);
            e
        })?;

        // 签名通过 → 关闭 sidecar → 启动静默安装 → 退出
        {
            let mut st = self.state.lock().unwrap();
            st.phase = UpdatePhase::Installing;
        }
        self.emit(app);

        let manager = app.state::<Arc<SidecarManager>>();
        manager.request_shutdown_and_wait("now", Duration::from_secs(15));
        let _ = manager.force_kill();

        let mut cmd = std::process::Command::new(&installer_path);
        cmd.arg("/S"); // NSIS 静默安装
        cmd.spawn().map_err(|e| format!("启动安装程序失败：{e}"))?;

        {
            let mut st = self.state.lock().unwrap();
            st.phase = UpdatePhase::Restarting;
        }
        self.emit(app);
        std::thread::sleep(Duration::from_millis(400));
        app.exit(0);
        Ok(())
    }

    pub fn fail(&self, app: &AppHandle, error: String) {
        let mut st = self.state.lock().unwrap();
        st.phase = UpdatePhase::Failed;
        st.error = Some(error);
        st.speed_bps = 0;
        st.can_install = false;
        drop(st);
        self.emit(app);
    }
}

fn resolve_feed_url() -> &'static str {
    option_env!("WINE_FOX_UPDATE_FEED_URL")
        .filter(|url| !url.trim().is_empty())
        .unwrap_or(UPDATE_FEED_URL)
        .trim()
}

fn parse_feed(body: &[u8]) -> Result<UpdateFeed, String> {
    let feed: UpdateFeed =
        serde_json::from_slice(body).map_err(|e| format!("更新清单解析失败：{e}"))?;
    if feed.version.trim().is_empty()
        || !feed
            .version
            .trim_start_matches('v')
            .split('.')
            .all(|part| !part.is_empty() && part.chars().all(|c| c.is_ascii_digit()))
    {
        return Err("更新清单版本号无效".into());
    }
    let win = &feed.platforms.windows_x86_64;
    if !win.url.starts_with("https://") {
        return Err("更新清单中的安装包地址必须使用 HTTPS".into());
    }
    if win.signature.trim().is_empty() {
        return Err("更新清单缺少安装包签名".into());
    }
    if win.size == 0 {
        return Err("更新清单中的安装包大小无效".into());
    }
    Ok(feed)
}

fn app_data_dir() -> PathBuf {
    let base = std::env::var("LOCALAPPDATA").unwrap_or_else(|_| ".".into());
    PathBuf::from(base).join("WineFox").join("updates")
}

fn now_rfc3339() -> String {
    // 简单本地时间（无 chrono 依赖）：ISO8601 本地时间
    let now = std::time::SystemTime::now()
        .duration_since(std::time::UNIX_EPOCH)
        .unwrap_or_default()
        .as_secs() as i64;
    let days = now.div_euclid(86400);
    let secs = now.rem_euclid(86400);
    let (h, m, s) = (secs / 3600, (secs % 3600) / 60, secs % 60);
    // 1970-01-01 起的天数 → 年月日（民用历）
    let (y, mo, d) = civil_from_days(days);
    format!("{y:04}-{mo:02}-{d:02}T{h:02}:{m:02}:{s:02}Z")
}

fn civil_from_days(z: i64) -> (i64, i64, i64) {
    let z = z + 719468;
    let era = z.div_euclid(146097);
    let doe = z.rem_euclid(146097);
    let yoe = (doe - doe / 1460 + doe / 36524 - doe / 146096) / 365;
    let y = yoe + era * 400;
    let doy = doe - (365 * yoe + yoe / 4 - yoe / 100);
    let mp = (5 * doy + 2) / 153;
    let d = doy - (153 * mp + 2) / 5 + 1;
    let m = if mp < 10 { mp + 3 } else { mp - 9 };
    (if m <= 2 { y + 1 } else { y }, m, d)
}

/// 更新公钥：优先环境变量 `WINE_FOX_UPDATE_PUBKEY`（base64），否则编译期内嵌
/// 仓库中的 `packaging/updater/pubkey.b64`（真实发布公钥）。
fn resolve_pubkey_b64() -> &'static str {
    option_env!("WINE_FOX_UPDATE_PUBKEY")
        .filter(|s| !s.is_empty())
        .unwrap_or(include_str!("../../../packaging/updater/pubkey.b64"))
        .trim()
}

/// 校验安装包字节的 Ed25519 签名（公钥与签名均为 base64）。
fn verify_installer_signature(
    pubkey_b64: &str,
    signature_b64: &str,
    body: &[u8],
) -> Result<(), String> {
    if pubkey_b64.is_empty() {
        return Err("更新公钥未配置".into());
    }
    let pubkey_bytes = base64::engine::general_purpose::STANDARD
        .decode(pubkey_b64.trim())
        .map_err(|_| "更新公钥格式错误".to_string())?;
    let key_bytes: [u8; 32] = pubkey_bytes
        .try_into()
        .map_err(|_| "更新公钥长度错误".to_string())?;
    let pubkey = ed25519_dalek::VerifyingKey::from_bytes(&key_bytes)
        .map_err(|_| "更新公钥无效".to_string())?;
    let sig_bytes = base64::engine::general_purpose::STANDARD
        .decode(signature_b64.trim())
        .map_err(|_| "更新签名格式错误".to_string())?;
    let signature = ed25519_dalek::Signature::from_slice(&sig_bytes)
        .map_err(|_| "更新签名长度错误".to_string())?;
    pubkey
        .verify(body, &signature)
        .map_err(|_| "更新签名验证失败：安装包已被篡改或不是官方发布".to_string())?;
    Ok(())
}

/// "8.5.11" 式版本号比较；非数字段按 0 处理。
pub fn compare_versions(a: &str, b: &str) -> std::cmp::Ordering {
    let pa: Vec<u64> = a
        .trim_start_matches('v')
        .split('.')
        .map(|s| {
            s.chars()
                .take_while(|c| c.is_ascii_digit())
                .collect::<String>()
                .parse::<u64>()
                .unwrap_or(0)
        })
        .collect();
    let pb: Vec<u64> = b
        .trim_start_matches('v')
        .split('.')
        .map(|s| {
            s.chars()
                .take_while(|c| c.is_ascii_digit())
                .collect::<String>()
                .parse::<u64>()
                .unwrap_or(0)
        })
        .collect();
    for i in 0..pa.len().max(pb.len()) {
        match pa
            .get(i)
            .copied()
            .unwrap_or(0)
            .cmp(&pb.get(i).copied().unwrap_or(0))
        {
            std::cmp::Ordering::Equal => continue,
            other => return other,
        }
    }
    std::cmp::Ordering::Equal
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn version_compare_works() {
        assert_eq!(
            compare_versions("8.5.12", "8.5.11"),
            std::cmp::Ordering::Greater
        );
        assert_eq!(
            compare_versions("8.5.11", "8.5.11"),
            std::cmp::Ordering::Equal
        );
        assert_eq!(
            compare_versions("8.5.10", "8.5.11"),
            std::cmp::Ordering::Less
        );
        assert_eq!(
            compare_versions("8.10.0", "8.9.9"),
            std::cmp::Ordering::Greater
        );
        assert_eq!(
            compare_versions("v8.5.12", "8.5.11"),
            std::cmp::Ordering::Greater
        );
    }

    #[test]
    fn feed_requires_complete_https_release() {
        let valid = br#"{
            "version":"8.6.7",
            "notes":"fix",
            "platforms":{"windows-x86_64":{
                "url":"https://example.invalid/WineFox-Setup-8.6.7-x64.exe",
                "signature":"c2ln",
                "size":42
            }}
        }"#;
        let feed = parse_feed(valid).expect("完整的 HTTPS 清单应通过");
        assert_eq!(feed.version, "8.6.7");
        assert_eq!(feed.platforms.windows_x86_64.size, 42);

        let insecure = String::from_utf8(valid.to_vec())
            .unwrap()
            .replace("https://", "http://");
        assert!(parse_feed(insecure.as_bytes())
            .unwrap_err()
            .contains("HTTPS"));

        let zero_size = String::from_utf8(valid.to_vec())
            .unwrap()
            .replace("\"size\":42", "\"size\":0");
        assert!(parse_feed(zero_size.as_bytes())
            .unwrap_err()
            .contains("大小"));
    }

    #[test]
    fn civil_date_roundtrip() {
        assert_eq!(civil_from_days(0), (1970, 1, 1));
        assert_eq!(civil_from_days(365), (1971, 1, 1));
    }

    #[test]
    fn verify_installer_signature_accepts_valid_and_rejects_tamper() {
        use ed25519_dalek::Signer as _;

        let signing_key = ed25519_dalek::SigningKey::from_bytes(&[7u8; 32]);
        let pubkey_b64 = base64::engine::general_purpose::STANDARD
            .encode(signing_key.verifying_key().to_bytes());
        let body = b"fake-installer-bytes-8.6.0";
        let signature_b64 =
            base64::engine::general_purpose::STANDARD.encode(signing_key.sign(body).to_bytes());

        verify_installer_signature(&pubkey_b64, &signature_b64, body).expect("有效签名应通过");

        let tampered = b"fake-installer-bytes-8.6.1";
        let err = verify_installer_signature(&pubkey_b64, &signature_b64, tampered)
            .expect_err("篡改内容应拒绝");
        assert!(err.contains("验证失败"));

        let err = verify_installer_signature("", &signature_b64, body).expect_err("空公钥应拒绝");
        assert!(err.contains("未配置"));
    }

    #[test]
    fn committed_pubkey_is_valid_ed25519() {
        let b64 = resolve_pubkey_b64();
        assert!(!b64.is_empty(), "编译期内嵌公钥不应为空");
        let bytes = base64::engine::general_purpose::STANDARD
            .decode(b64.trim())
            .expect("公钥应为合法 base64");
        assert_eq!(bytes.len(), 32, "Ed25519 公钥必须是 32 字节");
        ed25519_dalek::VerifyingKey::from_bytes(&bytes.try_into().unwrap())
            .expect("内嵌公钥应为有效 Ed25519 公钥");
    }

    #[test]
    fn malformed_pubkey_length_is_rejected_without_panicking() {
        let short_key = base64::engine::general_purpose::STANDARD.encode([1u8; 8]);
        let signature = base64::engine::general_purpose::STANDARD.encode([0u8; 64]);
        let err = verify_installer_signature(&short_key, &signature, b"installer")
            .expect_err("错误长度必须被拒绝");
        assert!(err.contains("长度"));
    }
}
