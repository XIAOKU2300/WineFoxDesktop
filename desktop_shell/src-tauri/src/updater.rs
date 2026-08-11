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
use serde::Serialize;
use std::path::PathBuf;
use std::sync::{Arc, Mutex};
use std::time::Duration;
use tauri::{AppHandle, Emitter, Manager};

pub const UPDATE_FEED_URL: &str = "https://github.com/XIAOKU2300/WineFoxDesktop/releases/latest/download/latest.json";
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
            can_install: true,
            busy_tasks: 0,
            feed_url: feed_url.to_string(),
        }
    }
}

pub struct UpdateManager {
    pub state: Mutex<UpdateState>,
    pub busy: Arc<Mutex<bool>>,
}

impl UpdateManager {
    pub fn new(version: &str) -> Arc<Self> {
        Arc::new(Self {
            state: Mutex::new(UpdateState::idle(version, UPDATE_FEED_URL)),
            busy: Arc::new(Mutex::new(false)),
        })
    }

    pub fn snapshot(&self) -> UpdateState {
        self.state.lock().unwrap().clone()
    }

    fn emit(&self, app: &AppHandle) {
        let _ = app.emit("update-status", self.snapshot());
    }

    /// 启动定时检查：5 秒后一次，之后每 24h 一次。失败静默（只记录状态）。
    pub fn start_auto_check(self: &Arc<Self>, app: AppHandle) {
        let me = Arc::clone(self);
        std::thread::spawn(move || {
            std::thread::sleep(FIRST_CHECK_DELAY);
            me.check(&app, false);
            loop {
                std::thread::sleep(CHECK_INTERVAL);
                me.check(&app, false);
            }
        });
    }

    /// 检查更新。force=true 时绕过最近检查记录并允许手动重试。
    pub fn check(self: &Arc<Self>, app: &AppHandle, _force: bool) -> UpdateState {
        {
            let mut st = self.state.lock().unwrap();
            if st.phase == UpdatePhase::Checking || st.phase == UpdatePhase::Downloading {
                return st.clone();
            }
            st.phase = UpdatePhase::Checking;
            st.error = None;
        }
        self.emit(app);

        let result = (|| -> Result<serde_json::Value, String> {
            let body = winhttp::https_get(UPDATE_FEED_URL, Duration::from_secs(15), None)?;
            serde_json::from_slice(&body).map_err(|e| format!("更新清单解析失败：{e}"))
        })();

        let mut st = self.state.lock().unwrap();
        st.last_checked = Some(now_rfc3339());
        match result {
            Ok(feed) => {
                let version = feed.get("version").and_then(|v| v.as_str()).unwrap_or("").to_string();
                let current = st.current_version.clone();
                let newer = !version.is_empty() && compare_versions(&version, &current).is_gt();
                if newer {
                    st.phase = UpdatePhase::Available;
                    st.new_version = Some(version);
                    st.notes = feed.get("notes").and_then(|v| v.as_str()).map(|s| s.to_string());
                    st.pub_date = feed.get("pub_date").and_then(|v| v.as_str()).map(|s| s.to_string());
                    st.size_bytes = feed
                        .pointer("/platforms/windows-x86_64/url")
                        .and_then(|_| feed.get("platforms").and_then(|p| p.get("windows-x86_64")))
                        .and_then(|w| w.get("size"))
                        .and_then(|s| s.as_u64());
                } else {
                    st.phase = UpdatePhase::UpToDate;
                    st.new_version = None;
                    st.notes = None;
                    st.size_bytes = None;
                }
            }
            Err(e) => {
                st.phase = UpdatePhase::Failed;
                st.error = Some(e);
            }
        }
        let snapshot = st.clone();
        drop(st);
        self.emit(app);
        snapshot
    }

    /// 下载并安装：下载 exe → 下载 .sig → Ed25519 验证 → 关闭 sidecar → 启动安装器。
    pub fn download_and_install(self: &Arc<Self>, app: &AppHandle) -> Result<(), String> {
        {
            let mut st = self.state.lock().unwrap();
            if st.phase != UpdatePhase::Available {
                return Err("当前没有可用的更新".into());
            }
            st.phase = UpdatePhase::Downloading;
            st.downloaded_bytes = 0;
            st.total_bytes = st.size_bytes.unwrap_or(0);
            st.speed_bps = 0;
            st.error = None;
        }
        self.emit(app);

        // 解析最新清单中的下载地址
        let feed: serde_json::Value = {
            let body = winhttp::https_get(UPDATE_FEED_URL, Duration::from_secs(15), None)?;
            serde_json::from_slice(&body).map_err(|e| format!("更新清单解析失败：{e}"))?
        };
        let win = feed
            .get("platforms")
            .and_then(|p| p.get("windows-x86_64"))
            .ok_or_else(|| "更新清单缺少 windows-x86_64 平台信息".to_string())?;
        let exe_url = win
            .get("url")
            .and_then(|v| v.as_str())
            .ok_or_else(|| "更新清单缺少安装包地址".to_string())?;
        let signature = win
            .get("signature")
            .and_then(|v| v.as_str())
            .ok_or_else(|| "更新清单缺少签名".to_string())?;
        let size_hint = win.get("size").and_then(|v| v.as_u64()).unwrap_or(0);

        let download_dir = app_data_dir();
        std::fs::create_dir_all(&download_dir).map_err(|e| format!("无法创建更新目录：{e}"))?;
        let installer_path = download_dir.join("WineFox-Setup-latest-x64.exe");

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

        let body = winhttp::https_get(exe_url, Duration::from_secs(600), Some(&mut progress))
            .map_err(|e| format!("安装包下载失败：{e}"))?;
        std::fs::write(&installer_path, &body).map_err(|e| format!("写入安装包失败：{e}"))?;

        // 签名验证（硬要求）：签名错误直接终止，绝不提供强制安装
        {
            let mut st = self.state.lock().unwrap();
            st.phase = UpdatePhase::Verifying;
            st.speed_bps = 0;
        }
        self.emit(app);

        verify_installer_signature(resolve_pubkey_b64(), signature, &body)?;

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
}

fn app_data_dir() -> PathBuf {
    let base = std::env::var("LOCALAPPDATA").unwrap_or_else(|_| ".".into());
    PathBuf::from(base)
        .join("WineFox")
        .join("updates")
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
    let mut key_bytes = [0u8; 32];
    key_bytes.copy_from_slice(&pubkey_bytes[..pubkey_bytes.len().min(32)]);
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
        .map(|s| s.chars().take_while(|c| c.is_ascii_digit()).collect::<String>().parse::<u64>().unwrap_or(0))
        .collect();
    let pb: Vec<u64> = b
        .trim_start_matches('v')
        .split('.')
        .map(|s| s.chars().take_while(|c| c.is_ascii_digit()).collect::<String>().parse::<u64>().unwrap_or(0))
        .collect();
    for i in 0..pa.len().max(pb.len()) {
        match pa.get(i).copied().unwrap_or(0).cmp(&pb.get(i).copied().unwrap_or(0)) {
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
        assert_eq!(compare_versions("8.5.12", "8.5.11"), std::cmp::Ordering::Greater);
        assert_eq!(compare_versions("8.5.11", "8.5.11"), std::cmp::Ordering::Equal);
        assert_eq!(compare_versions("8.5.10", "8.5.11"), std::cmp::Ordering::Less);
        assert_eq!(compare_versions("8.10.0", "8.9.9"), std::cmp::Ordering::Greater);
        assert_eq!(compare_versions("v8.5.12", "8.5.11"), std::cmp::Ordering::Greater);
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
        let pubkey_b64 =
            base64::engine::general_purpose::STANDARD.encode(signing_key.verifying_key().to_bytes());
        let body = b"fake-installer-bytes-8.6.0";
        let signature_b64 = base64::engine::general_purpose::STANDARD
            .encode(signing_key.sign(body).to_bytes());

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
}
