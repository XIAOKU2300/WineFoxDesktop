//! 安装/运行环境预检：WebView2 运行时是 Tauri 窗口的硬依赖。
//!
//! 精简版（Win10/11 覆盖绝大多数场景）：启动主窗口前查询 EdgeUpdate
//! 注册表判断 Evergreen WebView2 是否可用；缺失时用原生 MessageBox
//! 给出明确指引，并可一键打开微软官方下载页（不走第三方网盘）。

#![allow(non_snake_case)]

use windows_sys::Win32::Foundation::{HWND, INVALID_HANDLE_VALUE};
use windows_sys::Win32::UI::Shell::ShellExecuteW;
use windows_sys::Win32::UI::WindowsAndMessaging::{
    MessageBoxW, IDYES, MB_DEFBUTTON1, MB_ICONERROR, MB_TOPMOST, MB_YESNO,
};

const WEBVIEW2_REGISTRY_KEY: &str = concat!(
    "Software\\WOW6432Node\\Microsoft\\EdgeUpdate\\Clients\\",
    "{F3017226-FE2A-4295-8BDF-00C3A9A7E4C5}"
);
const WEBVIEW2_REGISTRY_KEY_USER: &str = concat!(
    "Software\\Microsoft\\EdgeUpdate\\Clients\\",
    "{F3017226-FE2A-4295-8BDF-00C3A9A7E4C5}"
);
const WEBVIEW2_OFFICIAL_URL: &str = "https://developer.microsoft.com/microsoft-edge/webview2/";

fn wide(text: &str) -> Vec<u16> {
    text.encode_utf16().chain(std::iter::once(0)).collect()
}

/// 判断本机是否安装了 Evergreen WebView2 运行时（含固定版本环境变量）。
pub fn webview2_runtime_available() -> bool {
    // 固定版本（Fixed Version）部署通过环境变量指明解压目录。
    for var in ["WEBVIEW2_BROWSER_EXECUTABLE_FOLDER", "WEBVIEW2_ADDITIONAL_BROWSER_ARGUMENTS"] {
        if let Ok(value) = std::env::var(var) {
            if var == "WEBVIEW2_BROWSER_EXECUTABLE_FOLDER" && !value.trim().is_empty() {
                let candidate = std::path::PathBuf::from(&value).join("msedgewebview2.exe");
                if candidate.is_file() {
                    return true;
                }
            }
        }
    }
    for (root, subkey) in [
        (windows_sys::Win32::System::Registry::HKEY_LOCAL_MACHINE, WEBVIEW2_REGISTRY_KEY),
        (windows_sys::Win32::System::Registry::HKEY_CURRENT_USER, WEBVIEW2_REGISTRY_KEY_USER),
    ] {
        if registry_string_value(root, subkey, "pv").is_some() {
            return true;
        }
    }
    false
}

fn registry_string_value(root: windows_sys::Win32::System::Registry::HKEY, subkey: &str, value: &str) -> Option<String> {
    use windows_sys::Win32::System::Registry::{
        RegCloseKey, RegGetValueW, HKEY, RRF_RT_REG_SZ,
    };
    unsafe {
        let subkey_w = wide(subkey);
        let value_w = wide(value);
        let mut handle: HKEY = std::ptr::null_mut();
        if windows_sys::Win32::System::Registry::RegOpenKeyExW(
            root,
            subkey_w.as_ptr(),
            0,
            windows_sys::Win32::System::Registry::KEY_READ,
            &mut handle,
        ) != 0 {
            return None;
        }
        let mut buffer = [0u16; 128];
        let mut size: u32 = (std::mem::size_of::<u16>() * buffer.len()) as u32;
        let result = RegGetValueW(
            handle,
            std::ptr::null(),
            value_w.as_ptr(),
            RRF_RT_REG_SZ,
            std::ptr::null_mut(),
            buffer.as_mut_ptr().cast(),
            &mut size,
        );
        RegCloseKey(handle);
        if result != 0 {
            return None;
        }
        let len = (size as usize) / std::mem::size_of::<u16>();
        let text = String::from_utf16_lossy(&buffer[..len]);
        let text = text.trim_end_matches('\0').trim().to_string();
        // "0.0.0.0" 表示注册表占位而运行时未真正安装。
        if text.is_empty() || text == "0.0.0.0" {
            return None;
        }
        Some(text)
    }
}

fn show_missing_webview2_dialog() -> bool {
    let title = wide("WineFox 无法启动");
    let message = wide(
        "本机缺少 Microsoft WebView2 运行时，窗口无法显示。\n\n\
         点击“是”打开微软官方下载页并安装“Evergreen Bootstrapper”（约 2 MB，\n\
         安装过程会自动联网获取运行时）；安装完成后重新打开 WineFox。\n\n\
         是否现在打开官方下载页面？",
    );
    let choice = unsafe {
        MessageBoxW(
            INVALID_HANDLE_VALUE as HWND,
            message.as_ptr(),
            title.as_ptr(),
            MB_YESNO | MB_ICONERROR | MB_TOPMOST | MB_DEFBUTTON1,
        )
    };
    choice == IDYES
}

fn open_official_download() {
    let verb = wide("open");
    let url = wide(WEBVIEW2_OFFICIAL_URL);
    unsafe {
        ShellExecuteW(
            INVALID_HANDLE_VALUE as HWND,
            verb.as_ptr(),
            url.as_ptr(),
            std::ptr::null(),
            std::ptr::null(),
            windows_sys::Win32::UI::WindowsAndMessaging::SW_SHOWNORMAL,
        );
    }
}

/// 启动预检入口：缺少 WebView2 时弹窗引导并终止启动。
pub fn preflight() {
    if webview2_runtime_available() {
        return;
    }
    if show_missing_webview2_dialog() {
        open_official_download();
    }
    std::process::exit(1);
}
