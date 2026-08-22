//! Minimal HTTPS GET via WinHTTP（系统信任链 + 自动跟随重定向）。
//!
//! 只用于更新检查与安装包下载：避免为 shell 引入 tokio/reqwest 重依赖。
//! 大文件下载支持进度回调（downloaded, total）。

use std::ffi::c_void;
use std::time::Duration;
use windows_sys::Win32::Foundation::GlobalFree;
use windows_sys::Win32::Networking::WinHttp::*;

const WINHTTP_QUERY_FLAG_NUMBER: u32 = 0x2000_0000;

fn last_error(context: &str) -> String {
    let code = unsafe { windows_sys::Win32::Foundation::GetLastError() };
    format!("{context}（WinHTTP 错误 {code}）")
}

/// 从 WinHTTP 返回的以 null 结尾的宽字符串中拷贝出_owned Vec<u16>_（含 null）。
unsafe fn wide_from_ptr(ptr: *mut u16) -> Vec<u16> {
    let mut len = 0usize;
    while *ptr.add(len) != 0 {
        len += 1;
    }
    let mut out = Vec::with_capacity(len + 1);
    for i in 0..len {
        out.push(*ptr.add(i));
    }
    out.push(0);
    out
}

/// 解析更新请求应使用的代理（返回宽字符串，随本次请求存活）。
///
/// 优先级：环境变量（HTTPS_PROXY/ALL_PROXY，显式配置）→ 用户系统代理
/// （Clash / v2rayN 等写入 WinINET 的配置）。都没有时返回 None，
/// 保持机器级/直连行为。
///
/// 历史缺陷（已修复）：此前把 WINHTTP_OPTION_PROXY 用 WinHttpSetOption
/// 设在 session 句柄上，WinHTTP 返回 12009 拒绝——代理从未生效过，直连
/// github.com 检查更新几乎必失败。现在改为在 WinHttpOpen 时直接以
/// NAMED_PROXY 打开，这是 WinHTTP 唯一可靠的同步会话代理设置方式。
unsafe fn resolve_proxy() -> (Option<Vec<u16>>, Option<Vec<u16>>) {
    const LOCAL_BYPASS: &str = "localhost;127.0.0.1;<local>";
    for key in ["HTTPS_PROXY", "https_proxy", "ALL_PROXY", "all_proxy"] {
        if let Ok(raw) = std::env::var(key) {
            let value = raw.trim().to_string();
            if value.is_empty() {
                continue;
            }
            let stripped = value
                .strip_prefix("http://")
                .or_else(|| value.strip_prefix("https://"))
                .unwrap_or(&value)
                .trim()
                .trim_end_matches('/')
                .to_string();
            if !stripped.is_empty() {
                return (Some(to_wide(&stripped)), Some(to_wide(LOCAL_BYPASS)));
            }
        }
    }

    let mut cfg = WINHTTP_CURRENT_USER_IE_PROXY_CONFIG {
        fAutoDetect: 0,
        lpszAutoConfigUrl: std::ptr::null_mut(),
        lpszProxy: std::ptr::null_mut(),
        lpszProxyBypass: std::ptr::null_mut(),
    };
    if WinHttpGetIEProxyConfigForCurrentUser(&mut cfg) == 0 {
        return (None, None);
    }
    let mut result = (None, None);
    if !cfg.lpszProxy.is_null() {
        let proxy = wide_from_ptr(cfg.lpszProxy);
        // 空串或仅空白视为未配置
        let meaningful = proxy.iter().take_while(|&&c| c != 0).any(|&c| c != b' ' as u16);
        if meaningful {
            let bypass = if cfg.lpszProxyBypass.is_null() {
                to_wide(LOCAL_BYPASS)
            } else {
                wide_from_ptr(cfg.lpszProxyBypass)
            };
            result = (Some(proxy), Some(bypass));
        }
    }
    // WinHTTP 返回的字符串需用 GlobalFree 释放
    if !cfg.lpszAutoConfigUrl.is_null() {
        GlobalFree(cfg.lpszAutoConfigUrl.cast());
    }
    if !cfg.lpszProxy.is_null() {
        GlobalFree(cfg.lpszProxy.cast());
    }
    if !cfg.lpszProxyBypass.is_null() {
        GlobalFree(cfg.lpszProxyBypass.cast());
    }
    result
}

/// 解析 https://host[:port]/path，返回 (host, port, path)。
fn parse_https_url(url: &str) -> Result<(String, u16, String), String> {
    let rest = url
        .strip_prefix("https://")
        .ok_or_else(|| format!("仅支持 https:// 地址：{url}"))?;
    let (authority, path) = match rest.split_once('/') {
        Some((a, p)) => (a, format!("/{p}")),
        None => (rest, "/".to_string()),
    };
    let (host, port) = match authority.rsplit_once(':') {
        Some((h, p)) => (h.to_string(), p.parse::<u16>().map_err(|e| e.to_string())?),
        None => (authority.to_string(), 443),
    };
    Ok((host, port, path))
}

fn to_wide(s: &str) -> Vec<u16> {
    s.encode_utf16().chain(std::iter::once(0)).collect()
}

/// HTTPS GET。`progress` 可选，收到 (downloaded, total_or_0)。
pub fn https_get(
    url: &str,
    timeout: Duration,
    mut progress: Option<&mut dyn FnMut(u64, u64)>,
) -> Result<Vec<u8>, String> {
    let (host, port, path) = parse_https_url(url)?;
    let host_w = to_wide(&host);
    let path_w = to_wide(&path);
    let user_agent = to_wide("WineFox-Desktop-Updater/8.6");

    unsafe {
        // 在打开会话时直接指定代理（见 resolve_proxy 的说明）。
        // wide 字符串存活到本函数返回，WinHttpOpen 会复制其内容。
        let (proxy_w, bypass_w) = resolve_proxy();
        let (access, proxy_ptr, bypass_ptr) = match &proxy_w {
            Some(p) => (
                WINHTTP_ACCESS_TYPE_NAMED_PROXY,
                p.as_ptr(),
                bypass_w
                    .as_ref()
                    .map_or(std::ptr::null(), |b| b.as_ptr()),
            ),
            None => (WINHTTP_ACCESS_TYPE_DEFAULT_PROXY, std::ptr::null(), std::ptr::null()),
        };
        let session = WinHttpOpen(user_agent.as_ptr(), access, proxy_ptr, bypass_ptr, 0);
        if session.is_null() {
            return Err(last_error("无法初始化 WinHTTP"));
        }
        let _guard_session = HandleGuard(session);

        let connect = WinHttpConnect(session, host_w.as_ptr(), port, 0);
        if connect.is_null() {
            return Err(last_error("无法连接更新服务器"));
        }
        let _guard_connect = HandleGuard(connect);

        let request = WinHttpOpenRequest(
            connect,
            to_wide("GET").as_ptr(),
            path_w.as_ptr(),
            std::ptr::null(),
            std::ptr::null(),
            std::ptr::null(),
            WINHTTP_FLAG_SECURE,
        );
        if request.is_null() {
            return Err(last_error("无法创建更新请求"));
        }
        let _guard_request = HandleGuard(request);

        // 跟随 GitHub Release 的 302 重定向，但禁止 HTTPS→HTTP 降级。
        // 1 = WINHTTP_OPTION_REDIRECT_POLICY_DISALLOW_HTTPS_TO_HTTP
        let redirect_policy: u32 = 1;
        WinHttpSetOption(
            request,
            WINHTTP_OPTION_REDIRECT_POLICY,
            &redirect_policy as *const u32 as *mut c_void,
            std::mem::size_of::<u32>() as u32,
        );

        let ms = timeout.as_millis().clamp(1000, 300_000) as i32;
        WinHttpSetTimeouts(request, ms, ms, ms, ms);

        if WinHttpSendRequest(request, std::ptr::null(), 0, std::ptr::null_mut(), 0, 0, 0) == 0 {
            return Err(last_error("发送更新请求失败"));
        }
        if WinHttpReceiveResponse(request, std::ptr::null_mut()) == 0 {
            return Err(last_error("接收更新响应失败"));
        }

        // 状态码：必须带 WINHTTP_QUERY_FLAG_NUMBER 以数值形式读取；
        // 否则 WinHTTP 返回字符串，4 字节缓冲必然报 122（缓冲不足）。
        let mut status: u32 = 0;
        let mut status_size = std::mem::size_of::<u32>() as u32;
        if WinHttpQueryHeaders(
            request,
            WINHTTP_QUERY_STATUS_CODE | WINHTTP_QUERY_FLAG_NUMBER,
            std::ptr::null(),
            &mut status as *mut u32 as *mut c_void,
            &mut status_size,
            std::ptr::null_mut(),
        ) == 0
        {
            return Err(last_error("读取响应状态失败"));
        }
        if status != 200 {
            return Err(format!("更新服务器返回 HTTP {status}"));
        }

        // Content-Length（可能缺失 → 0）。以字符串形式查询后解析。
        let mut total: u64 = 0;
        let mut total_buf = [0u16; 64];
        let mut total_size = (total_buf.len() * 2) as u32;
        if WinHttpQueryHeaders(
            request,
            WINHTTP_QUERY_CONTENT_LENGTH,
            std::ptr::null(),
            total_buf.as_mut_ptr() as *mut c_void,
            &mut total_size,
            std::ptr::null_mut(),
        ) != 0
        {
            let s = String::from_utf16_lossy(&total_buf[..(total_size as usize / 2).min(64)]);
            total = s.trim().parse::<u64>().unwrap_or(0);
        }

        let mut body: Vec<u8> = Vec::new();
        let mut chunk = [0u8; 64 * 1024];
        loop {
            let mut read = 0u32;
            if WinHttpReadData(
                request,
                chunk.as_mut_ptr() as *mut c_void,
                chunk.len() as u32,
                &mut read,
            ) == 0
            {
                return Err(last_error("读取下载数据失败"));
            }
            if read == 0 {
                break;
            }
            body.extend_from_slice(&chunk[..read as usize]);
            if let Some(cb) = progress.as_deref_mut() {
                cb(body.len() as u64, total);
            }
        }
        Ok(body)
    }
}

struct HandleGuard(*mut c_void);
impl Drop for HandleGuard {
    fn drop(&mut self) {
        unsafe {
            WinHttpCloseHandle(self.0);
        }
    }
}
