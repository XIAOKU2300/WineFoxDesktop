//! Minimal HTTPS GET via WinHTTP（系统信任链 + 自动跟随重定向）。
//!
//! 只用于更新检查与安装包下载：避免为 shell 引入 tokio/reqwest 重依赖。
//! 大文件下载支持进度回调（downloaded, total）。

use std::ffi::c_void;
use std::time::Duration;
use windows_sys::Win32::Networking::WinHttp::*;

fn last_error(context: &str) -> String {
    let code = unsafe { windows_sys::Win32::Foundation::GetLastError() };
    format!("{context}（WinHTTP 错误 {code}）")
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
    let user_agent = to_wide("WineFox-Desktop-Updater/8.5");

    unsafe {
        let session = WinHttpOpen(
            user_agent.as_ptr(),
            WINHTTP_ACCESS_TYPE_DEFAULT_PROXY,
            std::ptr::null(),
            std::ptr::null(),
            0,
        );
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

        // 状态码
        let mut status: u32 = 0;
        let mut status_size = std::mem::size_of::<u32>() as u32;
        if WinHttpQueryHeaders(
            request,
            WINHTTP_QUERY_STATUS_CODE,
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

        // Content-Length（可能缺失 → 0）
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
