//! 房间控制：调试模式门禁 + 与 Python sidecar 的 room 命令通道。
//!
//! 房间网关运行在 Python sidecar 进程内（独立 aiohttp 应用、独立端口、
//! 独立 Cookie/安全状态）。Rust 只负责：
//!   * 校验调试模式（start_room 未解锁返回 DEBUG_MODE_REQUIRED）
//!   * 把房间设置透传给 sidecar
//!   * 接收并转发 room_status / 房主令牌
//!   * 为面板代理提供房间端口与房主令牌
//!
//! 房间网关只监听 127.0.0.1:<随机端口>；公网访问只能经由 Tunnel。

use crate::debug_mode::DebugMode;
use crate::sidecar::SidecarManager;
use crate::state::{AppState, RoomStatus};
use crate::tunnel::TunnelProvider;
use serde::{Deserialize, Serialize};
use serde_json::json;
use std::sync::Arc;
use tauri::{AppHandle, Emitter, Manager, State};

/// 房主开启房间时的设置（来自实验室页面，透传给 Python）。
#[derive(Debug, Clone, Serialize, Deserialize, Default)]
#[serde(rename_all = "camelCase")]
pub struct RoomSettings {
    pub name: String,
    pub owner_name: String,
    pub password: String,
    pub max_members: u32,
    pub allow_chat: bool,
    pub allow_upload: bool,
    pub allow_generation: bool,
    pub allow_img2img: bool,
    pub allow_prompt_enhancement: bool,
    pub allowed_model_families: Vec<String>,
    pub allowed_workflow_ids: Vec<String>,
    pub allowed_loras: Vec<String>,
    pub per_member_queue_limit: u32,
    pub room_queue_limit: u32,
    pub message_retention_days: u32,
}

fn send_room_command(
    app: &AppHandle,
    manager: &Arc<SidecarManager>,
    payload: serde_json::Value,
) -> Result<(), String> {
    if !manager.send_command(payload) {
        return Err("本地服务未就绪，无法操作房间".into());
    }
    let _ = app;
    Ok(())
}

/// 开启房间。调试模式未解锁时稳定返回 DEBUG_MODE_REQUIRED。
#[tauri::command]
pub fn start_room(
    app: AppHandle,
    manager: State<'_, Arc<SidecarManager>>,
    debug: State<'_, DebugMode>,
    settings: RoomSettings,
) -> Result<(), String> {
    if !debug.is_enabled() {
        return Err(crate::debug_mode::DEBUG_MODE_REQUIRED.to_string());
    }
    if settings.name.trim().is_empty()
        || settings.owner_name.trim().is_empty()
        || settings.password.is_empty()
    {
        return Err("房间名称、房主名称和密码为必填项".into());
    }
    // 密码明文只经由 stdin 传给同进程 sidecar，不进入事件/日志。
    let payload = json!({
        "cmd": "room_start",
        "settings": settings,
    });
    send_room_command(&app, &manager, payload)
}

/// 关闭房间（优雅：停止接新任务 → 广播 room.closing → 关 WS → 停网关 → 停 Tunnel）。
/// 同时回收自动内网穿透进程（cloudflared）。
#[tauri::command]
pub fn stop_room(
    app: AppHandle,
    manager: State<'_, Arc<SidecarManager>>,
    state: State<'_, Arc<AppState>>,
) -> Result<(), String> {
    let provider = state.inner.lock().unwrap().room_tunnel_provider.take();
    if let Some(mut p) = provider {
        p.stop();
    }
    send_room_command(&app, &manager, json!({ "cmd": "room_stop" }))
}

/// 查询当前房间状态（前端展示 + 更新安装门禁）。
#[tauri::command]
pub fn get_room_status(state: State<'_, Arc<AppState>>) -> RoomStatus {
    state.inner.lock().unwrap().room.clone()
}

/// 设置已由外部 Tunnel（Cloudflare/FRP）解析出的公网 HTTPS 地址。
/// Rust 先做本地校验并验证 /room/v1/info 与 public_id 一致，再交给 sidecar。
#[tauri::command]
pub fn set_room_tunnel(
    app: AppHandle,
    manager: State<'_, Arc<SidecarManager>>,
    state: State<'_, Arc<AppState>>,
    url: String,
) -> Result<(), String> {
    let current_public_id = state.inner.lock().unwrap().room.public_id.clone();
    let verified = crate::tunnel::verify_public_url(&url, current_public_id.as_deref())?;
    send_room_command(&app, &manager, json!({ "cmd": "room_tunnel", "url": verified }))
}

/// 清除已配置的公网 Tunnel（退回本地房间）。
#[tauri::command]
pub fn clear_room_tunnel(
    app: AppHandle,
    manager: State<'_, Arc<SidecarManager>>,
) -> Result<(), String> {
    send_room_command(&app, &manager, json!({ "cmd": "room_clear_tunnel" }))
}

/// 是否安装了可用的 cloudflared（供实验室页面决定是否显示"自动创建"）。
#[tauri::command]
pub fn cloudflared_available() -> bool {
    crate::tunnel::resolve_cloudflared().is_ok()
}

/// 自动创建内网穿透：Rust 本地起 cloudflared → 解析公网地址 → 校验
/// public_id 一致 → 交给 sidecar 进入房间允许列表并广告邀请链接。
/// 调试模式未解锁时稳定返回 DEBUG_MODE_REQUIRED。
#[tauri::command]
pub async fn provision_room_tunnel(
    app: AppHandle,
    manager: State<'_, Arc<SidecarManager>>,
    state: State<'_, Arc<AppState>>,
    debug: State<'_, DebugMode>,
) -> Result<(), String> {
    if !debug.is_enabled() {
        return Err(crate::debug_mode::DEBUG_MODE_REQUIRED.to_string());
    }
    let (port, public_id) = {
        let inner = state.inner.lock().unwrap();
        (inner.room.port, inner.room.public_id.clone())
    };
    let (port, public_id) = match (port, public_id) {
        (Some(port), Some(pid)) if !pid.is_empty() => (port, pid),
        _ => return Err("当前没有运行中的房间".into()),
    };
    {
        let inner = state.inner.lock().unwrap();
        if inner.room_tunnel_provider.is_some() {
            return Err("已有自动 Tunnel 在运行，请先停止".into());
        }
    }

    let (provider, verified) = tauri::async_runtime::spawn_blocking(move || -> Result<(crate::tunnel::CloudflaredProvider, String), String> {
        let mut provider = crate::tunnel::CloudflaredProvider::new()?;
        let url = provider.start(port)?;
        // 隧道刚注册时可能短暂不可达，做几次带退避的重试。
        let mut last_err = "隧道尚未就绪".to_string();
        for attempt in 0..3 {
            match crate::tunnel::verify_public_url(&url, Some(&public_id)) {
                Ok(verified) => return Ok((provider, verified)),
                Err(e) => last_err = e,
            }
            if attempt < 2 {
                std::thread::sleep(std::time::Duration::from_secs(3));
            }
        }
        provider.stop();
        Err(last_err)
    })
    .await
    .map_err(|e| format!("自动 Tunnel 任务异常：{e}"))??;

    state.inner.lock().unwrap().room_tunnel_provider = Some(Box::new(provider));
    send_room_command(&app, &manager, json!({ "cmd": "room_tunnel", "url": verified }))
}

/// 停止自动内网穿透并清除公网地址（退回本地房间）。
#[tauri::command]
pub fn stop_room_tunnel(
    app: AppHandle,
    manager: State<'_, Arc<SidecarManager>>,
    state: State<'_, Arc<AppState>>,
) -> Result<(), String> {
    let provider = state.inner.lock().unwrap().room_tunnel_provider.take();
    if let Some(mut p) = provider {
        p.stop();
    }
    send_room_command(&app, &manager, json!({ "cmd": "room_clear_tunnel" }))
}

/// 处理 sidecar 上报的 room_status 行：更新内存状态并广播到前端。
pub fn handle_room_status(app: &AppHandle, payload: &serde_json::Value) {
    let parsed = parse_room_status(payload);
    let host_token = payload
        .get("host_token")
        .and_then(|v| v.as_str())
        .map(|s| s.to_string());
    {
        let state_guard = app.state::<Arc<AppState>>();
        let mut inner = state_guard.inner.lock().unwrap();
        inner.room = parsed.clone();
        if host_token.is_some() {
            inner.room_host_token = host_token;
        }
    }
    let _ = app.emit("room-status", &parsed);
}

pub fn parse_room_status(value: &serde_json::Value) -> RoomStatus {
    let get = |key: &str| value.get(key).and_then(|v| v.as_str()).map(|s| s.to_string());
    RoomStatus {
        state: get("state").unwrap_or_default(),
        public_id: get("publicId"),
        name: get("name"),
        owner_name: get("ownerName"),
        port: value.get("port").and_then(|v| v.as_u64()).map(|p| p as u16),
        local_url: get("localUrl"),
        tunnel_url: get("tunnelUrl"),
        tunnel_connected: value
            .get("tunnelConnected")
            .and_then(|v| v.as_bool())
            .unwrap_or(false),
        members: value.get("members").and_then(|v| v.as_u64()).unwrap_or(0) as u32,
        max_members: value.get("maxMembers").and_then(|v| v.as_u64()).unwrap_or(0) as u32,
        error: get("error"),
        member_limit_reached: value
            .get("memberLimitReached")
            .and_then(|v| v.as_bool())
            .unwrap_or(false),
        generation_paused: value
            .get("generationPaused")
            .and_then(|v| v.as_bool())
            .unwrap_or(false),
        members_join_paused: value
            .get("membersJoinPaused")
            .and_then(|v| v.as_bool())
            .unwrap_or(false),
        failed_login_attempts: value
            .get("failedLoginAttempts")
            .and_then(|v| v.as_u64())
            .unwrap_or(0) as u32,
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn parses_online_room_status() {
        let value = serde_json::json!({
            "state": "online",
            "publicId": "abc123",
            "name": "测试房间",
            "port": 51234,
            "members": 2,
            "maxMembers": 8,
            "tunnelConnected": true,
            "tunnelUrl": "https://abc.trycloudflare.com",
            "host_token": "tok",
        });
        let status = parse_room_status(&value);
        assert_eq!(status.state, "online");
        assert_eq!(status.public_id.as_deref(), Some("abc123"));
        assert_eq!(status.port, Some(51234));
        assert_eq!(status.members, 2);
        assert!(status.tunnel_connected);
        assert!(status.room_open());
    }

    #[test]
    fn stopped_room_is_not_open() {
        let status = RoomStatus {
            state: "stopped".into(),
            ..RoomStatus::default()
        };
        assert!(!status.room_open());
        assert!(RoomStatus::default().room_open() == false);
    }

    #[test]
    fn room_settings_roundtrip_camel_case() {
        let settings = RoomSettings {
            name: "R".into(),
            owner_name: "O".into(),
            password: "p".into(),
            max_members: 8,
            allow_chat: true,
            ..RoomSettings::default()
        };
        let value = serde_json::to_value(&settings).unwrap();
        assert_eq!(value["name"], "R");
        assert_eq!(value["maxMembers"], 8);
        assert!(value.get("owner_name").is_none());
    }
}
