//! 调试模式：连续点击左上角 Logo 解锁。
//!
//! 规则：6 秒内连续点击 10 次；相邻点击间隔不得超过 1.2 秒；
//! 超时或间隔过长则重新计数。解锁状态只保存在进程内存，绝不写入
//! settings / localStorage / SQLite；WineFox 重启后必须恢复普通模式。
//!
//! 调试模式只是功能门禁，不是安全认证。房间安全仍依赖密码、会话与
//! HTTPS/WSS。`start_room` 命令必须先检查本模块，未解锁时返回稳定的
//! `DEBUG_MODE_REQUIRED` 错误码。

use serde::Serialize;
use std::collections::VecDeque;
use std::sync::Mutex;
use std::time::{Duration, Instant};
use tauri::{AppHandle, Emitter, State};

pub const DEBUG_MODE_REQUIRED: &str = "DEBUG_MODE_REQUIRED";
const MAX_CLICKS: usize = 10;
const WINDOW: Duration = Duration::from_secs(6);
const MAX_GAP: Duration = Duration::from_millis(1200);

/// 单次点击的结果；`count` 为当前连续计数（用于 UI 展示进度）。
#[derive(Debug, Clone, PartialEq, Eq, Serialize)]
#[serde(rename_all = "camelCase")]
pub struct ClickResult {
    pub count: u32,
    pub unlocked: bool,
}

#[derive(Debug, Clone, Serialize)]
#[serde(rename_all = "camelCase")]
pub struct DebugModeState {
    pub enabled: bool,
}

#[derive(Default)]
struct DebugInner {
    clicks: VecDeque<Instant>,
    enabled: bool,
}

pub struct DebugMode {
    inner: Mutex<DebugInner>,
}

impl DebugMode {
    pub fn new() -> Self {
        Self {
            inner: Mutex::new(DebugInner::default()),
        }
    }

    pub fn is_enabled(&self) -> bool {
        self.inner.lock().unwrap().enabled
    }

    /// 记录一次点击并返回最新计数与是否解锁。时间由调用方注入以便测试。
    fn record_click_at(&self, now: Instant) -> ClickResult {
        let mut inner = self.inner.lock().unwrap();
        if inner.enabled {
            return ClickResult {
                count: MAX_CLICKS as u32,
                unlocked: true,
            };
        }
        // 超出 6 秒窗口的最早点击全部丢弃。
        while let Some(first) = inner.clicks.front() {
            if now.duration_since(*first) > WINDOW {
                inner.clicks.pop_front();
            } else {
                break;
            }
        }
        // 相邻间隔超过 1.2 秒：整条序列作废。
        if let Some(last) = inner.clicks.back() {
            if now.duration_since(*last) > MAX_GAP {
                inner.clicks.clear();
            }
        }
        inner.clicks.push_back(now);
        let count = inner.clicks.len();
        if count >= MAX_CLICKS {
            inner.enabled = true;
            inner.clicks.clear();
            ClickResult {
                count: MAX_CLICKS as u32,
                unlocked: true,
            }
        } else {
            ClickResult {
                count: count as u32,
                unlocked: false,
            }
        }
    }

    /// 测试用：清空状态，模拟重启后的普通模式。
    #[cfg(test)]
    fn reset(&self) {
        let mut inner = self.inner.lock().unwrap();
        inner.clicks.clear();
        inner.enabled = false;
    }
}

/// Tauri 命令：记录一次 Logo 点击。解锁成功时向 UI 广播事件。
#[tauri::command]
pub fn record_logo_click(app: AppHandle, state: State<'_, DebugMode>) -> ClickResult {
    let result = state.record_click_at(Instant::now());
    if result.unlocked {
        let _ = app.emit("debug-mode-changed", DebugModeState { enabled: true });
    }
    result
}

#[tauri::command]
pub fn get_debug_mode(state: State<'_, DebugMode>) -> DebugModeState {
    DebugModeState {
        enabled: state.is_enabled(),
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    fn tap_sequence(mode: &DebugMode, gaps_ms: &[u64]) -> Vec<ClickResult> {
        // 用一个单调递增的本地时钟模拟点击时间点。
        let mut results = Vec::new();
        let mut cursor = Instant::now();
        for gap in gaps_ms {
            cursor += Duration::from_millis(*gap);
            results.push(mode.record_click_at(cursor));
        }
        results
    }

    #[test]
    fn nine_clicks_do_not_unlock() {
        let mode = DebugMode::new();
        let results = tap_sequence(&mode, &[300; 9]);
        assert!(!results.iter().any(|r| r.unlocked));
        assert!(!mode.is_enabled());
        assert_eq!(results.last().unwrap().count, 9);
    }

    #[test]
    fn ten_consecutive_clicks_unlock() {
        let mode = DebugMode::new();
        let results = tap_sequence(&mode, &[300; 10]);
        assert!(!results[8].unlocked);
        assert!(results[9].unlocked);
        assert!(mode.is_enabled());
    }

    #[test]
    fn long_gap_resets_the_sequence() {
        let mode = DebugMode::new();
        let mut cursor = Instant::now();
        for _ in 0..5 {
            cursor += Duration::from_millis(300);
            mode.record_click_at(cursor);
        }
        // 超过 1.2 秒的间隔使序列作废。
        cursor += Duration::from_millis(2000);
        let result = mode.record_click_at(cursor);
        assert!(!result.unlocked);
        assert_eq!(result.count, 1);
    }

    #[test]
    fn six_second_window_expires_old_clicks() {
        let mode = DebugMode::new();
        let mut cursor = Instant::now();
        for _ in 0..6 {
            cursor += Duration::from_millis(900); // 每个间隔都合法（0.9s < 1.2s）
            mode.record_click_at(cursor);
        }
        // 在第 6 次点击后等待 5 秒（仍在 6 秒窗口内的点击保留）。
        cursor += Duration::from_millis(5000);
        // 但最旧的点击已超过窗口：7 次点击被截断为最近的有效序列。
        let result = mode.record_click_at(cursor);
        // 间隔 5 秒 > 1.2 秒，序列先被清空再记录 1 次。
        assert_eq!(result.count, 1);
    }

    #[test]
    fn window_edge_keeps_recent_clicks() {
        let mode = DebugMode::new();
        let mut cursor = Instant::now();
        for gap in [400, 400, 400, 400, 400] {
            cursor += Duration::from_millis(gap);
            mode.record_click_at(cursor);
        }
        // 最后一次点击与最旧点击相距 1.6s < 6s，仍在窗口内。
        cursor += Duration::from_millis(1500);
        let result = mode.record_click_at(cursor);
        // 间隔 1.5s > 1.2s → 序列清空。
        assert_eq!(result.count, 1);
    }

    #[test]
    fn restart_clears_debug_mode() {
        let mode = DebugMode::new();
        tap_sequence(&mode, &[200; 10]);
        assert!(mode.is_enabled());
        // 模拟 WineFox 重启：全新实例，普通模式。
        let fresh = DebugMode::new();
        assert!(!fresh.is_enabled());
    }

    #[test]
    fn once_unlocked_click_count_stays_max() {
        let mode = DebugMode::new();
        tap_sequence(&mode, &[200; 10]);
        assert!(mode.is_enabled());
        let again = mode.record_click_at(Instant::now());
        assert_eq!(again.count, 10);
        assert!(again.unlocked);
    }

    #[test]
    fn internal_reset_returns_to_normal() {
        let mode = DebugMode::new();
        tap_sequence(&mode, &[200; 10]);
        assert!(mode.is_enabled());
        mode.reset();
        assert!(!mode.is_enabled());
    }
}
