//! Minimal synchronous HTTP/1.1 client (loopback health checks only).
//!
//! Avoids pulling tokio/reqwest into the shell; the only endpoints used are
//! GET /api/health on 127.0.0.1.

use std::io::{Read, Write};
use std::net::TcpStream;
use std::time::Duration;

/// GET `url`, parse the JSON body if any.
/// Returns Ok(Some(json)) on 200 + JSON, Ok(None) on non-JSON body,
/// Err on connection/parse failures and non-200 statuses.
pub fn get_json(url: &str, timeout: Duration) -> Result<Option<serde_json::Value>, String> {
    let (host, port, path) = parse_url(url)?;
    let addr = format!("{host}:{port}");
    let sock_addr: std::net::SocketAddr = addr.parse().map_err(|_| format!("bad address: {addr}"))?;
    let mut stream = TcpStream::connect_timeout(&sock_addr, timeout)
        .map_err(|e| format!("connect {addr}: {e}"))?;
    stream
        .set_read_timeout(Some(timeout))
        .map_err(|e| e.to_string())?;
    stream
        .set_write_timeout(Some(timeout))
        .map_err(|e| e.to_string())?;

    let req = format!(
        "GET {path} HTTP/1.1\r\nHost: {host}:{port}\r\nConnection: close\r\nAccept: application/json\r\nUser-Agent: winefox-shell/1\r\n\r\n"
    );
    stream.write_all(req.as_bytes()).map_err(|e| e.to_string())?;

    let mut buf = Vec::new();
    stream.read_to_end(&mut buf).map_err(|e| e.to_string())?;
    let text = String::from_utf8_lossy(&buf);
    let (head, body) = match text.split_once("\r\n\r\n") {
        Some((h, b)) => (h.to_string(), b.to_string()),
        None => (text.into_owned(), String::new()),
    };
    let status = head
        .lines()
        .next()
        .and_then(|l| l.split_whitespace().nth(1))
        .and_then(|s| s.parse::<u16>().ok())
        .unwrap_or(0);
    if status != 200 {
        return Err(format!("HTTP {status}"));
    }
    match serde_json::from_str(body.trim()) {
        Ok(v) => Ok(Some(v)),
        Err(_) => Ok(None),
    }
}

fn parse_url(url: &str) -> Result<(String, u16, String), String> {
    let rest = url
        .strip_prefix("http://")
        .ok_or_else(|| format!("unsupported url: {url}"))?;
    let (authority, path) = match rest.split_once('/') {
        Some((a, p)) => (a, format!("/{p}")),
        None => (rest, "/".to_string()),
    };
    let (host, port) = match authority.rsplit_once(':') {
        Some((h, p)) => (h.to_string(), p.parse::<u16>().map_err(|e| e.to_string())?),
        None => (authority.to_string(), 80),
    };
    Ok((host, port, path))
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn url_parsing() {
        assert_eq!(
            parse_url("http://127.0.0.1:18333/api/health").unwrap(),
            ("127.0.0.1".to_string(), 18333, "/api/health".to_string())
        );
        assert!(parse_url("https://example.com").is_err());
    }

    #[test]
    fn connection_refused_is_an_error() {
        // Port 1 is virtually always closed; the client must return Err.
        let r = get_json("http://127.0.0.1:1/api/health", Duration::from_millis(500));
        assert!(r.is_err(), "{r:?}");
    }
}
