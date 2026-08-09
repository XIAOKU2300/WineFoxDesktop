//! Sidecar stdout line parsing + ready handshake validation.
//!
//! The sidecar prints exactly one JSON object per line on stdout.  Non-JSON
//! lines are skipped (recorded as warnings).  The first `ready` line must
//! validate: protocol == 1 and version == shell version, otherwise the
//! session is rejected.

use serde_json::Value;

pub const PROTOCOL: u32 = 1;

#[derive(Debug, Clone, PartialEq, Eq)]
pub enum Line {
    Ready(ReadyInfo),
    Status { busy: bool, tasks: u32 },
    Log { level: String, message: String },
    Ack { cmd: String, ok: bool },
    Exit { code: i32, reason: String },
    Other(Value),
}

#[derive(Debug, Clone, PartialEq, Eq)]
pub struct ReadyInfo {
    pub protocol: u32,
    pub version: String,
    pub host: String,
    pub port: u16,
    pub bootstrap_path: String,
    pub pid: Option<u32>,
}

/// Parse one stdout line.  Returns `None` for non-JSON / malformed lines
/// (the caller logs a sanitized warning and keeps waiting).
pub fn parse_line(raw: &str) -> Option<Line> {
    let trimmed = raw.trim_end_matches(['\r', '\n']);
    if trimmed.is_empty() {
        return None;
    }
    let v: Value = serde_json::from_str(trimmed).ok()?;
    let obj = v.as_object()?;
    let kind = obj.get("type")?.as_str()?;
    match kind {
        "ready" => {
            let protocol = obj.get("protocol").and_then(Value::as_u64).map(|p| p as u32)?;
            let version = obj.get("version").and_then(Value::as_str)?.to_string();
            let host = obj.get("host").and_then(Value::as_str).unwrap_or("").to_string();
            let port = obj.get("port").and_then(Value::as_u64).map(|p| p as u16)?;
            let bootstrap_path = obj.get("bootstrapPath").and_then(Value::as_str)?.to_string();
            let pid = obj.get("pid").and_then(Value::as_u64).map(|p| p as u32);
            Some(Line::Ready(ReadyInfo { protocol, version, host, port, bootstrap_path, pid }))
        }
        "status" => {
            let busy = obj.get("busy").and_then(Value::as_bool).unwrap_or(false);
            let tasks = obj.get("tasks").and_then(Value::as_u64).unwrap_or(0) as u32;
            Some(Line::Status { busy, tasks })
        }
        "log" => {
            let level = obj.get("level").and_then(Value::as_str).unwrap_or("info").to_string();
            let message = obj.get("message").and_then(Value::as_str).unwrap_or("").to_string();
            Some(Line::Log { level, message })
        }
        "ack" => {
            let cmd = obj.get("cmd").and_then(Value::as_str).unwrap_or("").to_string();
            let ok = obj.get("ok").and_then(Value::as_bool).unwrap_or(false);
            Some(Line::Ack { cmd, ok })
        }
        "exit" => {
            let code = obj.get("code").and_then(Value::as_i64).unwrap_or(-1) as i32;
            let reason = obj.get("reason").and_then(Value::as_str).unwrap_or("").to_string();
            Some(Line::Exit { code, reason })
        }
        _ => Some(Line::Other(v)),
    }
}

/// Validate a ready handshake against the expected protocol + app version.
#[derive(Debug, Clone, PartialEq, Eq)]
pub enum ReadyError {
    ProtocolMismatch { got: u32, expected: u32 },
    VersionMismatch { got: String, expected: String },
    NotLoopback { host: String },
}

pub fn validate_ready(info: &ReadyInfo, expected_version: &str) -> Result<(), ReadyError> {
    if info.protocol != PROTOCOL {
        return Err(ReadyError::ProtocolMismatch { got: info.protocol, expected: PROTOCOL });
    }
    if info.version != expected_version {
        return Err(ReadyError::VersionMismatch {
            got: info.version.clone(),
            expected: expected_version.to_string(),
        });
    }
    if !(info.host == "127.0.0.1" || info.host == "::1" || info.host == "localhost") {
        return Err(ReadyError::NotLoopback { host: info.host.clone() });
    }
    Ok(())
}

#[cfg(test)]
mod tests {
    use super::*;

    fn ready_line() -> String {
        r#"{"type":"ready","protocol":1,"version":"8.5.9","host":"127.0.0.1","port":59265,"bootstrapPath":"/bootstrap/abc123","impl":"winefox-sidecar-fake","implVersion":"0.1.0","pid":1234}"#.to_string()
    }

    #[test]
    fn parses_valid_ready() {
        let line = parse_line(&ready_line()).unwrap();
        match line {
            Line::Ready(info) => {
                assert_eq!(info.protocol, 1);
                assert_eq!(info.version, "8.5.9");
                assert_eq!(info.port, 59265);
                assert_eq!(info.bootstrap_path, "/bootstrap/abc123");
                assert_eq!(info.pid, Some(1234));
                assert!(validate_ready(&info, "8.5.9").is_ok());
            }
            _ => panic!("expected ready"),
        }
    }

    #[test]
    fn rejects_non_json() {
        assert!(parse_line("this is not json").is_none());
        assert!(parse_line("").is_none());
        assert!(parse_line("{\"broken").is_none());
        assert!(parse_line("123").is_none()); // not an object
    }

    #[test]
    fn rejects_missing_fields() {
        assert!(parse_line(r#"{"type":"ready","protocol":1}"#).is_none());
        assert!(parse_line(r#"{"type":"ready","protocol":"x","version":"8.5.9"}"#).is_none());
    }

    #[test]
    fn protocol_mismatch_is_rejected() {
        let line = parse_line(&ready_line().replace("\"protocol\":1", "\"protocol\":2")).unwrap();
        match line {
            Line::Ready(info) => {
                let err = validate_ready(&info, "8.5.9").unwrap_err();
                assert_eq!(err, ReadyError::ProtocolMismatch { got: 2, expected: 1 });
            }
            _ => panic!("expected ready"),
        }
    }

    #[test]
    fn version_mismatch_is_rejected() {
        let line = parse_line(&ready_line().replace("\"8.5.9\"", "\"9.9.9\"")).unwrap();
        match line {
            Line::Ready(info) => {
                let err = validate_ready(&info, "8.5.9").unwrap_err();
                assert_eq!(err, ReadyError::VersionMismatch { got: "9.9.9".into(), expected: "8.5.9".into() });
            }
            _ => panic!("expected ready"),
        }
    }

    #[test]
    fn non_loopback_host_is_rejected() {
        let line = parse_line(&ready_line().replace("127.0.0.1", "0.0.0.0")).unwrap();
        match line {
            Line::Ready(info) => {
                let err = validate_ready(&info, "8.5.9").unwrap_err();
                assert_eq!(err, ReadyError::NotLoopback { host: "0.0.0.0".into() });
            }
            _ => panic!("expected ready"),
        }
    }

    #[test]
    fn parses_status_log_ack_exit() {
        let s = parse_line(r#"{"type":"status","busy":true,"tasks":2,"task":null}"#).unwrap();
        assert_eq!(s, Line::Status { busy: true, tasks: 2 });
        let l = parse_line(r#"{"type":"log","level":"warn","message":"hi"}"#).unwrap();
        assert_eq!(l, Line::Log { level: "warn".into(), message: "hi".into() });
        let a = parse_line(r#"{"type":"ack","cmd":"shutdown","ok":true}"#).unwrap();
        assert_eq!(a, Line::Ack { cmd: "shutdown".into(), ok: true });
        let e = parse_line(r#"{"type":"exit","code":0,"reason":"shutdown-now"}"#).unwrap();
        assert_eq!(e, Line::Exit { code: 0, reason: "shutdown-now".into() });
    }

    #[test]
    fn unknown_types_are_kept() {
        assert!(matches!(parse_line(r#"{"type":"future-thing","x":1}"#), Some(Line::Other(_))));
    }

    #[test]
    fn line_ending_tolerance() {
        assert!(parse_line(&format!("{}\r\n", ready_line())).is_some());
        assert!(parse_line(&format!("{}\n", ready_line())).is_some());
    }
}
