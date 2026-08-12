//! stderr sanitization for sidecar output (mirror of the sidecar's own
//! redaction rules in winefox_sidecar/protocol.py).

/// Redact secret-looking content from one line of sidecar stderr.
pub fn sanitize_line(line: &str) -> String {
    if line.is_empty() {
        return line.to_string();
    }
    let mut out = line.to_string();
    // Authorization: <value>
    out = redact_regex(&out, r#"(?i)(authorization\s*:\s*)[^\r\n]+"#, Some(1));
    // key= / token: / secret = / api_key: / password= ... values
    out = redact_regex(
        &out,
        r#"(?i)\b(?:sk|pk|ak|token|secret|password|passwd|api[_-]?key|authorization|bearer|signature|sig)["']?\s*[:=]\s*["']?[A-Za-z0-9_\-./+=]{6,}"#,
        None,
    );
    // PEM private key blocks (dotall so a multi-line block is caught)
    out = redact_regex(
        &out,
        r#"(?s)-----BEGIN [A-Z ]*PRIVATE KEY-----.*?-----END [A-Z ]*PRIVATE KEY-----"#,
        None,
    );
    // Long hex / base64 runs
    out = redact_regex(&out, r#"\b[0-9a-fA-F]{32,}\b"#, None);
    out = redact_regex(&out, r#"\b[A-Za-z0-9+/]{40,}={0,2}\b"#, None);
    out
}

const REDACTED: &str = "[REDACTED]";

fn redact_regex(input: &str, pattern: &str, keep_group: Option<usize>) -> String {
    let Ok(re) = regex_lite::Regex::new(pattern) else {
        return input.to_string();
    };
    re.replace_all(input, |caps: &regex_lite::Captures<'_>| match keep_group {
        Some(g) => match caps.get(g) {
            Some(m) => format!("{}{}", m.as_str(), REDACTED),
            None => REDACTED.to_string(),
        },
        None => REDACTED.to_string(),
    })
    .into_owned()
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn redacts_common_secrets() {
        let s = sanitize_line("api_key=sk-abc123def456 and token: abcdef0123456789");
        assert!(!s.contains("sk-abc123def456"), "{s}");
        assert!(!s.contains("abcdef0123456789"), "{s}");
    }

    #[test]
    fn redacts_authorization_header() {
        let s = sanitize_line("Authorization: Bearer xxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxx");
        assert!(!s.contains("Bearer"), "{s}");
        assert!(s.contains("[REDACTED]"), "{s}");
    }

    #[test]
    fn redacts_long_hex() {
        let hex = "deadbeef".repeat(4);
        let s = sanitize_line(&format!("hash={hex}"));
        assert!(!s.contains(&hex), "{s}");
    }

    #[test]
    fn redacts_private_key_block() {
        let pem = "-----BEGIN RSA PRIVATE KEY-----\nAAAA\n-----END RSA PRIVATE KEY-----";
        let s = sanitize_line(pem);
        assert!(!s.contains("AAAA"), "{s}");
        assert!(s.contains("[REDACTED]"), "{s}");
    }

    #[test]
    fn keeps_plain_text() {
        let s = sanitize_line("sidecar booted ok on port 18333");
        assert_eq!(s, "sidecar booted ok on port 18333");
    }
}
