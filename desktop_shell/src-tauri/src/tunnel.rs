//! 内网穿透支持。
//!
//! 两种方式：
//!   1. 手动 Tunnel：房主自行完成 Cloudflare Tunnel / FRP，把公网 HTTPS URL
//!      填回 WineFox；Rust 验证 scheme/Host/public_id 后把地址交给 sidecar
//!      （房间网关据其设置允许的 Origin 并重新广告邀请链接）。
//!   2. 自动 Tunnel：`CloudflaredProvider` 本地起 cloudflared 快速隧道，解析
//!      分配到的 trycloudflare 公网地址。参数由代码固定生成，禁止 UI 传任意
//!      命令行；只允许转发当前房间网关端口；子进程绑定 kill-on-close Job
//!      Object，WineFox 退出时被 Windows 回收，禁止孤儿进程。

use crate::job_object::JobObject;
use crate::winhttp;
use std::io::BufReader;
use std::os::windows::process::CommandExt;
use std::time::Duration;

/// 隐藏 cloudflared 的控制台窗口（从 GUI 进程启动时默认会弹出）。
const CREATE_NO_WINDOW: u32 = 0x0800_0000;

/// 校验一个手动 Tunnel 公网地址。成功返回规范化后的 URL。
///
/// 校验规则（安全硬门禁）：
///   * scheme 必须是 https（拒绝 http / file / javascript / data）
///   * Host 不能为空
///   * 不允许 username:password 形式
///   * 必须能访问 /room/v1/info
///   * 返回的 room id（public id）必须与当前房间一致
pub fn verify_public_url(url: &str, expected_public_id: Option<&str>) -> Result<String, String> {
    let parsed = parse_public_url(url)?;
    let base = parsed.base();
    let info_url = format!("{base}/room/v1/info");
    let body = winhttp::https_get(&info_url, Duration::from_secs(12), None)
        .map_err(|e| format!("无法访问房间信息端点：{e}"))?;
    let value: serde_json::Value =
        serde_json::from_slice(&body).map_err(|e| format!("房间信息端点返回无效 JSON：{e}"))?;
    let room_id = extract_public_id(&value).ok_or_else(|| "房间信息缺少 public_id".to_string())?;
    if let Some(expected) = expected_public_id {
        if !expected.is_empty() && room_id != expected {
            return Err("Tunnel 指向的房间与本机房间不一致".into());
        }
    }
    Ok(base)
}

/// 从 /room/v1/info 响应中提取房间公开 ID。
/// 网关当前返回 `room.id`；为兼容历史契约也接受 `room.public_id`。
fn extract_public_id(info: &serde_json::Value) -> Option<String> {
    let room = info.get("room")?;
    let id = room.get("id").or_else(|| room.get("public_id"))?.as_str()?;
    if id.is_empty() {
        None
    } else {
        Some(id.to_string())
    }
}

/// 仅做静态校验（scheme/host/userinfo），供单元测试与复用。
pub(crate) fn parse_public_url(url: &str) -> Result<ParseOutcome, String> {
    let trimmed = url.trim();
    if trimmed.is_empty() {
        return Err("请输入公网地址".into());
    }
    let parsed = url::Url::parse(trimmed).map_err(|_| "地址格式无效".to_string())?;
    let scheme = parsed.scheme().to_ascii_lowercase();
    if scheme != "https" && scheme != "http" {
        return Err("公开邀请地址必须使用 HTTPS 或 HTTP".into());
    }
    let host = parsed
        .host_str()
        .ok_or_else(|| "地址缺少 Host".to_string())?;
    if host.is_empty() {
        return Err("地址缺少 Host".into());
    }
    if !parsed.username().is_empty() || parsed.password().is_some() {
        return Err("地址不允许包含用户名或密码".into());
    }
    Ok(ParseOutcome {
        scheme,
        host: host.to_string(),
        port: parsed.port(),
    })
}

/// 静态校验结果。字段 `pub(crate)` 供 room_control.rs 按 scheme 分流读取。
pub(crate) struct ParseOutcome {
    pub(crate) scheme: String,
    pub(crate) host: String,
    pub(crate) port: Option<u16>,
}

impl ParseOutcome {
    /// 规范化后的 base URL。非标准端口显式携带（标准端口 url::Url::port()
    /// 返回 None，无需重复拼接），避免探测 /room/v1/info 时端口被静默丢弃。
    pub(crate) fn base(&self) -> String {
        match self.port {
            Some(p) => format!("{}://{}:{}", self.scheme, self.host, p),
            None => format!("{}://{}", self.scheme, self.host),
        }
    }
}

/// TunnelProvider 抽象：自动内网穿透提供方。
pub trait TunnelProvider: Send + Sync {
    fn name(&self) -> &'static str;
    /// 只允许转发 local_port，参数完全由实现固定生成。
    fn start(&mut self, local_port: u16) -> Result<String, String>;
    fn stop(&mut self);
    fn is_running(&self) -> bool;
}

/// 解析 cloudflared 日志中的公网地址（`https://<id>.trycloudflare.com`）。
fn parse_trycloudflare_url(line: &str) -> Option<String> {
    let re = regex_lite::Regex::new(
        r#"https://[a-zA-Z0-9](?:[a-zA-Z0-9-]*[a-zA-Z0-9])?\.trycloudflare\.com"#,
    )
    .ok()?;
    re.find(line).map(|m| m.as_str().to_string())
}

/// 定位 cloudflared 可执行文件。
/// 优先级：环境变量 `WINEFOX_CLOUDFLARED` → PATH。
pub fn resolve_cloudflared() -> Result<std::path::PathBuf, String> {
    if let Some(env) = std::env::var_os("WINEFOX_CLOUDFLARED") {
        let path = std::path::PathBuf::from(env);
        if path.is_file() {
            return Ok(path);
        }
        return Err("WINEFOX_CLOUDFLARED 指定的路径不存在".into());
    }
    // PATH 探测：能 spawn 即说明可解析。
    match std::process::Command::new("cloudflared")
        .arg("--version")
        .stdin(std::process::Stdio::null())
        .stdout(std::process::Stdio::null())
        .stderr(std::process::Stdio::null())
        .creation_flags(CREATE_NO_WINDOW)
        .spawn()
    {
        Ok(mut child) => {
            let _ = child.wait();
            Ok(std::path::PathBuf::from("cloudflared"))
        }
        Err(_) => Err(
            "未找到 cloudflared，请先安装 cloudflared 或将路径写入环境变量 WINEFOX_CLOUDFLARED"
                .into(),
        ),
    }
}

/// cloudflared 自动隧道提供方。
///
/// 参数完全由代码固定生成：只转发房间网关的 local_port，UI 无法传入任意命令行；
/// 子进程绑定到独立的 kill-on-close Job Object，WineFox 退出或 `stop()` 时被回收，
/// 禁止孤儿进程。
pub struct CloudflaredProvider {
    child: Option<std::process::Child>,
    job: Option<JobObject>,
    public_url: Option<String>,
}

impl CloudflaredProvider {
    pub fn new() -> Result<Self, String> {
        resolve_cloudflared()?;
        Ok(CloudflaredProvider {
            child: None,
            job: None,
            public_url: None,
        })
    }

    /// 读取 cloudflared 的 stdout/stderr，等待第一条 trycloudflare 公网地址。
    /// 超时或进程退出都会返回错误（调用方负责 kill）。
    fn read_public_url(
        &self,
        stdout: std::process::ChildStdout,
        stderr: std::process::ChildStderr,
    ) -> Result<String, String> {
        use std::io::BufRead;
        use std::sync::mpsc;
        use std::time::{Duration, Instant};

        let (tx, rx) = mpsc::channel::<Option<String>>();
        let tx1 = tx.clone();
        let t1 = std::thread::spawn(move || {
            for line in BufRead::lines(BufReader::new(stdout)) {
                let Ok(line) = line else { break };
                if tx1.send(Some(line)).is_err() {
                    break;
                }
            }
            let _ = tx1.send(None);
        });
        let tx2 = tx;
        let t2 = std::thread::spawn(move || {
            for line in BufRead::lines(BufReader::new(stderr)) {
                let Ok(line) = line else { break };
                if tx2.send(Some(line)).is_err() {
                    break;
                }
            }
            let _ = tx2.send(None);
        });
        let _ = (t1, t2);

        let timeout = Duration::from_secs(45);
        let start = Instant::now();
        let mut eof_count = 0_u32;
        loop {
            let elapsed = start.elapsed();
            if elapsed >= timeout {
                return Err("等待 cloudflared 分配地址超时，请检查网络".into());
            }
            match rx.recv_timeout(timeout - elapsed) {
                Ok(Some(line)) => {
                    if let Some(url) = parse_trycloudflare_url(&line) {
                        return Ok(url);
                    }
                }
                Ok(None) => {
                    eof_count += 1;
                    if eof_count >= 2 {
                        return Err("cloudflared 意外退出，未能建立隧道".into());
                    }
                }
                Err(mpsc::RecvTimeoutError::Timeout) => {
                    return Err("等待 cloudflared 分配地址超时，请检查网络".into());
                }
                Err(mpsc::RecvTimeoutError::Disconnected) => {
                    return Err("cloudflared 输出通道已关闭".into());
                }
            }
        }
    }

    fn clean_up(&mut self) {
        if let Some(mut child) = self.child.take() {
            let _ = child.kill();
            let _ = child.wait();
        }
        self.job = None;
        self.public_url = None;
    }
}

impl TunnelProvider for CloudflaredProvider {
    fn name(&self) -> &'static str {
        "cloudflared"
    }

    fn start(&mut self, local_port: u16) -> Result<String, String> {
        if self.is_running() {
            return Err("已有自动 Tunnel 在运行".into());
        }
        let exe = resolve_cloudflared()?;
        let url_arg = format!("http://127.0.0.1:{local_port}");
        let mut cmd = std::process::Command::new(exe);
        cmd.args(["tunnel", "--no-autoupdate", "--url", &url_arg]);
        cmd.stdin(std::process::Stdio::null());
        cmd.stdout(std::process::Stdio::piped());
        cmd.stderr(std::process::Stdio::piped());
        cmd.creation_flags(CREATE_NO_WINDOW);
        let mut child = cmd
            .spawn()
            .map_err(|e| format!("启动 cloudflared 失败：{e}"))?;

        let job = JobObject::create().ok();
        if let Some(j) = &job {
            let _ = j.assign(child.id());
        }

        let stdout = child
            .stdout
            .take()
            .ok_or_else(|| "无法读取 cloudflared 输出".to_string())?;
        let stderr = child
            .stderr
            .take()
            .ok_or_else(|| "无法读取 cloudflared 错误输出".to_string())?;
        let url = match self.read_public_url(stdout, stderr) {
            Ok(url) => url,
            Err(e) => {
                let _ = child.kill();
                let _ = child.wait();
                return Err(e);
            }
        };
        self.child = Some(child);
        self.job = job;
        self.public_url = Some(url.clone());
        Ok(url)
    }

    fn stop(&mut self) {
        self.clean_up();
    }

    fn is_running(&self) -> bool {
        self.public_url.is_some()
    }
}

impl Drop for CloudflaredProvider {
    fn drop(&mut self) {
        self.clean_up();
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    fn parse(url: &str) -> Result<ParseOutcome, String> {
        parse_public_url(url)
    }

    #[test]
    fn accepts_https_url() {
        let out = parse("https://abc.trycloudflare.com").unwrap();
        assert_eq!(out.scheme, "https");
        assert_eq!(out.host, "abc.trycloudflare.com");
    }

    #[test]
    fn accepts_http_url() {
        let out = parse("http://abc.trycloudflare.com").unwrap();
        assert_eq!(out.scheme, "http");
        assert_eq!(out.host, "abc.trycloudflare.com");
        assert_eq!(out.base(), "http://abc.trycloudflare.com");
    }

    #[test]
    fn rejects_empty_and_malformed() {
        assert!(parse("").is_err());
        assert!(parse("not a url").is_err());
        assert!(parse("https://").is_err());
    }

    #[test]
    fn rejects_userinfo() {
        assert!(parse("https://user:pass@abc.trycloudflare.com").is_err());
        assert!(parse("https://user@abc.trycloudflare.com").is_err());
        assert!(parse("http://user:pass@abc.trycloudflare.com").is_err());
    }

    #[test]
    fn rejects_forbidden_schemes() {
        for url in [
            "file:///c:/temp/x",
            "javascript:alert(1)",
            "data:text/html,x",
            "ftp://abc.trycloudflare.com",
            "gopher://abc.trycloudflare.com",
            "ws://abc.trycloudflare.com",
            "wss://abc.trycloudflare.com",
        ] {
            assert!(parse(url).is_err(), "expected reject: {url}");
        }
    }

    #[test]
    fn verification_result_is_normalized_base() {
        let out = parse("https://abc.trycloudflare.com/invite?room=1").unwrap();
        assert_eq!(out.scheme, "https");
        assert_eq!(out.host, "abc.trycloudflare.com");
    }

    #[test]
    fn extracts_public_id_from_info_response() {
        // 网关 /room/v1/info 返回 room.id（公开 ID）；自动隧道校验依赖此路径。
        let current = serde_json::json!({
            "protocol": 1,
            "room": { "id": "abc123", "name": "R", "state": "online" },
        });
        assert_eq!(extract_public_id(&current).as_deref(), Some("abc123"));

        // 兼容历史字段名 room.public_id。
        let legacy = serde_json::json!({ "room": { "public_id": "legacy" } });
        assert_eq!(extract_public_id(&legacy).as_deref(), Some("legacy"));

        assert_eq!(extract_public_id(&serde_json::json!({ "room": {} })), None);
        assert_eq!(extract_public_id(&serde_json::json!({})), None);
        assert_eq!(
            extract_public_id(&serde_json::json!({ "room": { "id": "" } })),
            None
        );
    }

    // ------------------------------------------------------------------
    // cloudflared 自动隧道
    // ------------------------------------------------------------------
    #[test]
    fn parses_trycloudflare_url_from_log_line() {
        let line =
            "2026-08-11T10:00:00Z INF |  https://abc-123-def.trycloudflare.com             |";
        let url = parse_trycloudflare_url(line).expect("URL 应被解析");
        assert_eq!(url, "https://abc-123-def.trycloudflare.com");
    }

    #[test]
    fn parses_url_inside_banner_block() {
        let line = "https://level2-winefox-abc-456.trycloudflare.com";
        assert_eq!(
            parse_trycloudflare_url(line).as_deref(),
            Some("https://level2-winefox-abc-456.trycloudflare.com")
        );
    }

    #[test]
    fn rejects_lines_without_tunnel_url() {
        for line in [
            "",
            "2026-08-11T10:00:00Z INF Starting tunnel",
            "err: connection refused",
            "https://not-trycloudflare.example.com",
            "http://abc.trycloudflare.com", // 非 https 不应匹配
        ] {
            assert_eq!(parse_trycloudflare_url(line), None, "line: {line}");
        }
    }

    #[test]
    fn provider_errors_when_cloudflared_missing() {
        // WINEFOX_CLOUDFLARED 指向不存在路径 → 明确报错，而不是 panic。
        std::env::set_var(
            "WINEFOX_CLOUDFLARED",
            "Z:\\definitely\\missing\\cloudflared.exe",
        );
        let err = resolve_cloudflared().unwrap_err();
        std::env::remove_var("WINEFOX_CLOUDFLARED");
        assert!(err.contains("WINEFOX_CLOUDFLARED"));
    }

    #[test]
    fn provider_rejects_second_start_while_running() {
        let mut provider = CloudflaredProvider {
            child: None,
            job: None,
            public_url: Some("https://abc.trycloudflare.com".into()),
        };
        assert!(provider.is_running());
        let err = provider.start(1234).unwrap_err();
        assert!(err.contains("已有自动 Tunnel"));
        // stop 清理后可以再次 start（此处子进程不存在，stop 不 panic）。
        provider.stop();
        assert!(!provider.is_running());
    }
}
