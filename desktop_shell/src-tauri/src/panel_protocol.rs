//! Custom URI scheme that proxies sidecar HTTP traffic through the shell.
//!
//! WebView2 on Windows can refuse direct loopback HTTP, so the panel iframe
//! loads a Tauri custom protocol (`winefox-panel://localhost/<port>/...`)
//! whose handler fetches the same resources over a plain Rust TCP socket and
//! returns them to the webview. The sidecar keeps its loopback-only binding
//! and WebView2's network stack is never involved for panel traffic.

use crate::http_client;
use crate::state::AppState;
use std::collections::HashSet;
use std::time::Duration;
use tauri::http::{header, Request, Response, StatusCode};

pub const PANEL_SCHEME: &str = "winefox-panel";

const REQUEST_TIMEOUT: Duration = Duration::from_secs(5);

pub fn handle(state: &AppState, request: Request<Vec<u8>>) -> Response<Vec<u8>> {
    let path = request
        .uri()
        .path_and_query()
        .map(|p| p.as_str().to_string())
        .unwrap_or_else(|| request.uri().path().to_string());
    let current_port = match state.inner.lock().unwrap().port {
        Some(port) => port,
        None => return error_response(StatusCode::NOT_FOUND, "sidecar is not ready"),
    };
    // 房间网关端口与房主令牌：代理 `/room/*` 时注入 `X-WineFox-Room-Host`，
    // 使实验室页面可以访问房间网关而无需浏览器 Cookie。
    let (room_port, room_host_token) = {
        let inner = state.inner.lock().unwrap();
        (inner.room.port, inner.room_host_token.clone())
    };
    let (port, sidecar_path) = match resolve_target(&path, current_port, room_port) {
        Some(v) => v,
        None => return error_response(StatusCode::BAD_REQUEST, "bad winefox-panel URL"),
    };

    let url = format!("http://127.0.0.1:{port}{sidecar_path}");
    let mut owned_headers: Vec<(String, String)> = Vec::new();
    for (name, value) in request.headers() {
        let lower = name.as_str().to_ascii_lowercase();
        if !should_forward_request_header(&lower) {
            continue;
        }
        if let Ok(text) = value.to_str() {
            owned_headers.push((lower, text.to_string()));
        }
    }
    // 主面板会话 Cookie 只转发给主 sidecar；房间网关独立 Cookie/安全域，
    // 房主身份完全由 `X-WineFox-Room-Host` 令牌确认。
    if Some(port) != room_port {
        let session_cookie = state.inner.lock().unwrap().session_cookie.clone();
        if let Some(cookie) = session_cookie {
            owned_headers.push(("Cookie".to_string(), cookie));
        }
    }
    if Some(port) == room_port {
        if let Some(token) = room_host_token {
            owned_headers.push(("X-WineFox-Room-Host".to_string(), token));
        }
    }
    let extra_headers: Vec<(&str, &str)> = owned_headers
        .iter()
        .map(|(name, value)| (name.as_str(), value.as_str()))
        .collect();
    let body = if request.body().is_empty() {
        None
    } else {
        Some(request.body().as_slice())
    };
    match http_client::request(
        request.method().as_str(),
        &url,
        &extra_headers,
        body,
        REQUEST_TIMEOUT,
    ) {
        Ok(resp) => {
            let mut builder = Response::builder().status(resp.status);
            let mut forwarded = HashSet::new();
            for (name, value) in &resp.headers {
                let lower = name.to_ascii_lowercase();
                if should_forward_response_header(&lower) && forwarded.insert(lower) {
                    builder = builder.header(name.as_str(), value.as_str());
                }
            }
            if !forwarded.contains("content-type") {
                if let Some(ct) = resp.content_type {
                    builder = builder.header(header::CONTENT_TYPE, ct);
                }
            }
            builder
                .header(header::CACHE_CONTROL, "no-store")
                .body(resp.body)
                .unwrap_or_else(|_| {
                    error_response(StatusCode::INTERNAL_SERVER_ERROR, "bad proxy response")
                })
        }
        Err(e) => error_response(
            StatusCode::BAD_GATEWAY,
            &format!("sidecar request failed: {e}"),
        ),
    }
}

/// Only pass through request headers required by WineFox UI operations.
/// Browser identity, transport and origin headers are regenerated or omitted.
fn should_forward_request_header(lower: &str) -> bool {
    matches!(
        lower,
        "accept"
            | "accept-language"
            | "content-type"
            | "if-modified-since"
            | "if-none-match"
            | "range"
            | "x-winefox-client"
    )
}

/// Only pass through response headers the webview actually needs. Everything
/// else is dropped so Tauri/wry-generated headers never collide with sidecar
/// headers. Cache policy is always replaced with one `no-store` value below.
fn should_forward_response_header(lower: &str) -> bool {
    matches!(
        lower,
        "content-type"
            | "content-encoding"
            | "content-disposition"
            | "location"
            | "etag"
            | "accept-ranges"
            | "content-range"
    )
}

/// Resolve (target_port, sidecar_path) from either a port-prefixed panel URL
/// (`/<port>/<path>`) or a portless absolute path used by the UI's fetch/asset
/// URLs (`/api/v1/...`, `/css/...`).
///
/// 带端口前缀的 URL 按端口路由：匹配主面板端口 → 主 sidecar；匹配房间网关
/// 端口 → 房间网关。无端口绝对路径中 `/room/*` 路由到房间网关（供实验室页面
/// 使用），其余使用主面板端口。
fn resolve_target(path: &str, current_port: u16, room_port: Option<u16>) -> Option<(u16, String)> {
    match parse_panel_path(path) {
        Some((port, sidecar_path)) if port == current_port => Some((port, sidecar_path)),
        Some((port, sidecar_path)) if room_port == Some(port) => Some((port, sidecar_path)),
        Some(_) => None,
        None => match room_port {
            Some(rp) if path.starts_with("/room/") => Some((rp, path.to_string())),
            _ => Some((current_port, path.to_string())),
        },
    }
}

/// Parse `/<port>/<sidecar-path>` out of a `winefox-panel://localhost/...` URI.
fn parse_panel_path(path: &str) -> Option<(u16, String)> {
    let rest = path.strip_prefix('/')?;
    let (port_str, tail) = match rest.split_once('/') {
        Some((port, tail)) => (port, format!("/{tail}")),
        None => (rest, "/".to_string()),
    };
    let port = port_str.parse::<u16>().ok()?;
    let mut sidecar_path = tail;

    // The bootstrap page lives under /bootstrap/<token>/, so its relative
    // `api/...` fetches arrive here with that prefix; the sidecar serves the
    // API at the root. Normalize before forwarding.
    if let Some(after_bootstrap) = sidecar_path.strip_prefix("/bootstrap/") {
        let (_token, after) = after_bootstrap.split_once('/').unwrap_or(("", ""));
        if after.starts_with("api/") {
            sidecar_path = format!("/{after}");
        }
    }
    Some((port, sidecar_path))
}

fn error_response(status: StatusCode, message: &str) -> Response<Vec<u8>> {
    Response::builder()
        .status(status)
        .header(header::CONTENT_TYPE, "text/plain; charset=utf-8")
        .body(message.as_bytes().to_vec())
        .unwrap()
}

#[cfg(test)]
mod tests {
    use super::{parse_panel_path, resolve_target};
    use crate::state::{AppState, SidecarState};
    use std::io::{Read, Write};
    use std::net::TcpListener;
    use std::thread;
    use std::time::Duration;
    use tauri::http::{header, Request, StatusCode};

    #[test]
    fn parses_port_and_sidecar_path() {
        assert_eq!(
            parse_panel_path("/63933/bootstrap/abc/"),
            Some((63933, "/bootstrap/abc/".into()))
        );
        assert_eq!(
            parse_panel_path("/63933/bootstrap/abc/styles.css"),
            Some((63933, "/bootstrap/abc/styles.css".into()))
        );
    }

    #[test]
    fn normalizes_ui_relative_api_calls() {
        assert_eq!(
            parse_panel_path("/63933/bootstrap/abc/api/status"),
            Some((63933, "/api/status".into()))
        );
        assert_eq!(
            parse_panel_path("/63933/api/status"),
            Some((63933, "/api/status".into()))
        );
    }

    #[test]
    fn rejects_missing_or_bad_port() {
        assert_eq!(parse_panel_path("/"), None);
        assert_eq!(parse_panel_path("/abc/status"), None);
    }

    #[test]
    fn portless_paths_use_the_current_sidecar_port() {
        assert_eq!(
            resolve_target("/api/v1/health", 57466, None),
            Some((57466, "/api/v1/health".into()))
        );
        assert_eq!(
            resolve_target("/", 57466, None),
            Some((57466, "/".into()))
        );
    }

    #[test]
    fn mismatched_port_prefix_is_rejected() {
        assert_eq!(resolve_target("/12345/api/status", 57466, None), None);
    }

    #[test]
    fn room_port_prefix_routes_to_room_gateway() {
        assert_eq!(
            resolve_target("/51888/room/lab", 57466, Some(51888)),
            Some((51888, "/room/lab".into()))
        );
        assert_eq!(
            resolve_target("/51888/room/v1/catalog", 57466, Some(51888)),
            Some((51888, "/room/v1/catalog".into()))
        );
    }

    #[test]
    fn unknown_port_still_rejected_when_room_port_set() {
        assert_eq!(resolve_target("/12345/api/status", 57466, Some(51888)), None);
    }

    #[test]
    fn portless_room_paths_route_to_room_gateway() {
        assert_eq!(
            resolve_target("/room/v1/catalog", 57466, Some(51888)),
            Some((51888, "/room/v1/catalog".into()))
        );
        assert_eq!(
            resolve_target("/room/v1/catalog", 57466, None),
            Some((57466, "/room/v1/catalog".into()))
        );
    }

    #[test]
    fn only_whitelisted_request_headers_are_forwarded() {
        use super::should_forward_request_header;

        assert!(should_forward_request_header("accept"));
        assert!(should_forward_request_header("content-type"));
        assert!(should_forward_request_header("x-winefox-client"));
        assert!(!should_forward_request_header("user-agent"));
        assert!(!should_forward_request_header("host"));
        assert!(!should_forward_request_header("cookie"));
        assert!(!should_forward_request_header("origin"));
    }

    #[test]
    fn only_whitelisted_response_headers_are_forwarded() {
        use super::should_forward_response_header;

        assert!(should_forward_response_header("content-type"));
        assert!(should_forward_response_header("content-range"));
        assert!(!should_forward_response_header("cache-control"));
        assert!(!should_forward_response_header("user-agent"));
        assert!(!should_forward_response_header("set-cookie"));
        assert!(!should_forward_response_header("content-length"));
        assert!(!should_forward_response_header("transfer-encoding"));
    }

    #[test]
    fn proxy_deduplicates_headers_and_sends_only_the_session_cookie_pair() {
        let listener = TcpListener::bind("127.0.0.1:0").unwrap();
        let port = listener.local_addr().unwrap().port();
        let capture = thread::spawn(move || {
            let (mut stream, _) = listener.accept().unwrap();
            stream
                .set_read_timeout(Some(Duration::from_secs(2)))
                .unwrap();
            let mut bytes = Vec::new();
            let mut block = [0_u8; 1024];
            while !bytes.windows(4).any(|w| w == b"\r\n\r\n") {
                let read = stream.read(&mut block).unwrap();
                if read == 0 {
                    break;
                }
                bytes.extend_from_slice(&block[..read]);
            }
            stream
                .write_all(
                    b"HTTP/1.1 200 OK\r\nContent-Type: application/json\r\nCache-Control: max-age=600\r\nUser-Agent: backend/1\r\nContent-Length: 2\r\nConnection: close\r\n\r\n{}",
                )
                .unwrap();
            String::from_utf8(bytes).unwrap()
        });

        let state = AppState::new();
        {
            let mut inner = state.inner.lock().unwrap();
            inner.state = SidecarState::Ready;
            inner.port = Some(port);
            inner.session_cookie = Some("winefox_session=abc123".into());
        }
        let request = Request::builder()
            .method("GET")
            .uri("http://winefox-panel.localhost/api/v1/settings")
            .header(header::USER_AGENT, "webview/1")
            .header(header::ACCEPT, "application/json")
            .body(Vec::new())
            .unwrap();
        let response = super::handle(&state, request);
        assert_eq!(response.status(), StatusCode::OK);
        assert_eq!(response.body(), b"{}");
        assert_eq!(
            response.headers().get(header::CACHE_CONTROL).unwrap(),
            "no-store"
        );
        assert_eq!(
            response
                .headers()
                .get_all(header::CACHE_CONTROL)
                .iter()
                .count(),
            1
        );
        assert!(response.headers().get(header::USER_AGENT).is_none());

        let raw = capture.join().unwrap();
        let count = |name: &str| {
            raw.lines()
                .filter(|line| {
                    line.split_once(':')
                        .is_some_and(|(key, _)| key.eq_ignore_ascii_case(name))
                })
                .count()
        };
        assert_eq!(count("User-Agent"), 1);
        assert_eq!(count("Cookie"), 1);
        assert!(raw.contains("User-Agent: winefox-shell/1\r\n"));
        assert!(raw.contains("Cookie: winefox_session=abc123\r\n"));
        assert!(!raw.contains("HttpOnly"));
    }

    #[test]
    fn room_proxy_injects_host_token_only_for_room_gateway() {
        let listener = TcpListener::bind("127.0.0.1:0").unwrap();
        let port = listener.local_addr().unwrap().port();
        let capture = thread::spawn(move || {
            let (mut stream, _) = listener.accept().unwrap();
            stream
                .set_read_timeout(Some(Duration::from_secs(2)))
                .unwrap();
            let mut bytes = Vec::new();
            let mut block = [0_u8; 1024];
            while !bytes.windows(4).any(|w| w == b"\r\n\r\n") {
                let read = stream.read(&mut block).unwrap();
                if read == 0 {
                    break;
                }
                bytes.extend_from_slice(&block[..read]);
            }
            stream
                .write_all(
                    b"HTTP/1.1 200 OK\r\nContent-Type: application/json\r\nContent-Length: 2\r\nConnection: close\r\n\r\n{}",
                )
                .unwrap();
            String::from_utf8(bytes).unwrap()
        });

        let state = AppState::new();
        {
            let mut inner = state.inner.lock().unwrap();
            inner.state = SidecarState::Ready;
            inner.port = Some(1); // main panel port (unused here)
            inner.room.port = Some(port);
            inner.room_host_token = Some("host-token-123".into());
        }
        let request = Request::builder()
            .method("GET")
            .uri(format!("http://winefox-panel.localhost/{port}/room/v1/catalog"))
            .header(header::ACCEPT, "application/json")
            .body(Vec::new())
            .unwrap();
        let response = super::handle(&state, request);
        assert_eq!(response.status(), StatusCode::OK);

        let raw = capture.join().unwrap();
        let first_line = raw.lines().next().unwrap();
        assert!(
            first_line.contains(format!(" /room/v1/catalog HTTP/1.1").as_str()),
            "request line: {first_line}"
        );
        assert!(raw.contains("X-WineFox-Room-Host: host-token-123\r\n"));
        // 主面板会话 Cookie 不应泄漏给房间网关（房间网关只认房主令牌）。
        assert!(!raw.contains("winefox_session"));
    }

    #[test]
    fn room_proxy_omits_host_token_for_main_panel() {
        let listener = TcpListener::bind("127.0.0.1:0").unwrap();
        let port = listener.local_addr().unwrap().port();
        let capture = thread::spawn(move || {
            let (mut stream, _) = listener.accept().unwrap();
            stream
                .set_read_timeout(Some(Duration::from_secs(2)))
                .unwrap();
            let mut bytes = Vec::new();
            let mut block = [0_u8; 1024];
            while !bytes.windows(4).any(|w| w == b"\r\n\r\n") {
                let read = stream.read(&mut block).unwrap();
                if read == 0 {
                    break;
                }
                bytes.extend_from_slice(&block[..read]);
            }
            stream
                .write_all(
                    b"HTTP/1.1 200 OK\r\nContent-Type: application/json\r\nContent-Length: 2\r\nConnection: close\r\n\r\n{}",
                )
                .unwrap();
            String::from_utf8(bytes).unwrap()
        });

        let state = AppState::new();
        {
            let mut inner = state.inner.lock().unwrap();
            inner.state = SidecarState::Ready;
            inner.port = Some(port);
            inner.room.port = Some(9999);
            inner.room_host_token = Some("host-token-123".into());
        }
        let request = Request::builder()
            .method("GET")
            .uri("http://winefox-panel.localhost/api/v1/settings")
            .header(header::ACCEPT, "application/json")
            .body(Vec::new())
            .unwrap();
        let response = super::handle(&state, request);
        assert_eq!(response.status(), StatusCode::OK);
        let raw = capture.join().unwrap();
        assert!(!raw.contains("X-WineFox-Room-Host"));
    }
}
