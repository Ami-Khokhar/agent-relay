//! Storage and routing rules for agent-artifacts.
//!
//! Everything that decides what happens lives here so the HTTP layer stays a
//! thin adapter: on-disk layout, artifact versions, sessions, presence leases,
//! comment claims and delivery states.

use serde::{Deserialize, Serialize};
use serde_json::{json, Map, Value};
use sha2::{Digest, Sha256};
use std::collections::BTreeMap;
use std::fs;
use std::io::Read;
use std::path::{Path, PathBuf};
use std::sync::{Mutex, OnceLock};
use std::time::{SystemTime, UNIX_EPOCH};

pub const OWNER: &str = "@owner";

// ------------------------------------------------------------------ settings

fn env_first(names: &[&str]) -> Option<String> {
    for name in names {
        if let Ok(value) = std::env::var(name) {
            if !value.is_empty() {
                return Some(value);
            }
        }
    }
    None
}

fn int_env(default: i64, names: &[&str]) -> i64 {
    match env_first(names) {
        None => default,
        Some(raw) => raw
            .trim()
            .parse::<i64>()
            .ok()
            .filter(|value| *value > 0)
            .unwrap_or_else(|| {
                eprintln!("{} must be a positive integer", names[0]);
                std::process::exit(2);
            }),
    }
}

pub fn host() -> String {
    env_first(&["AGENT_ARTIFACTS_HOST"]).unwrap_or_else(|| "127.0.0.1".into())
}

pub fn port() -> u16 {
    int_env(43126, &["AGENT_ARTIFACTS_PORT"]) as u16
}

pub fn content_port() -> u16 {
    int_env((port() as i64) + 1, &["AGENT_ARTIFACTS_CONTENT_PORT"]) as u16
}

pub fn max_html() -> usize {
    int_env(8 * 1024 * 1024, &["AGENT_ARTIFACTS_MAX_BYTES"]) as usize
}

pub fn max_body() -> usize {
    int_env(16 * 1024 * 1024, &["AGENT_ARTIFACTS_MAX_BODY_BYTES"]) as usize
}

pub fn max_wait() -> u64 {
    int_env(600, &["AGENT_ARTIFACTS_MAX_WAIT_SECONDS"]) as u64
}

pub fn lease_seconds() -> u64 {
    int_env(30, &["AGENT_ARTIFACTS_LEASE_SECONDS"]) as u64
}

pub fn max_comment() -> usize {
    int_env(20_000, &["AGENT_ARTIFACTS_MAX_COMMENT_CHARS"]) as usize
}

pub fn max_artifacts() -> usize {
    int_env(500, &["AGENT_ARTIFACTS_MAX_COUNT"]) as usize
}

pub fn state_dir() -> PathBuf {
    if let Some(raw) = env_first(&["AGENT_ARTIFACTS_DIR", "A2A_ARTIFACTS_DIR"]) {
        return PathBuf::from(raw);
    }
    let base = env_first(&["XDG_STATE_HOME"]).unwrap_or_else(|| {
        format!("{}/.local/state", std::env::var("HOME").unwrap_or_else(|_| ".".into()))
    });
    PathBuf::from(base).join("agent-relay").join("artifacts")
}

pub fn public_base() -> String {
    if let Some(url) = env_first(&["AGENT_ARTIFACTS_PUBLIC_URL"]) {
        return url;
    }
    format!("http://{}:{}", bind_host(), port())
}

pub fn content_base() -> String {
    if let Some(url) = env_first(&["AGENT_ARTIFACTS_CONTENT_URL"]) {
        return url;
    }
    format!("http://{}:{}", bind_host(), content_port())
}

pub fn bind_host() -> String {
    let host = host();
    if host == "0.0.0.0" || host == "::" {
        "127.0.0.1".into()
    } else {
        host
    }
}

pub fn artifact_csp() -> String {
    format!(
        "default-src 'self' data: blob: https: http:; script-src 'self'; connect-src 'none'; \
         frame-ancestors {}",
        public_base()
    )
}

// --------------------------------------------------------------------- clock

pub fn now_iso() -> String {
    iso_from_secs(now_secs())
}

pub fn now_secs() -> u64 {
    SystemTime::now()
        .duration_since(UNIX_EPOCH)
        .map(|d| d.as_secs())
        .unwrap_or(0)
}

/// RFC 3339 in UTC, second precision, matching the Python implementation.
pub fn iso_from_secs(secs: u64) -> String {
    let days = (secs / 86_400) as i64;
    let rem = secs % 86_400;
    let (year, month, day) = civil_from_days(days);
    format!(
        "{:04}-{:02}-{:02}T{:02}:{:02}:{:02}Z",
        year,
        month,
        day,
        rem / 3600,
        (rem % 3600) / 60,
        rem % 60
    )
}

fn civil_from_days(days: i64) -> (i64, u32, u32) {
    let z = days + 719_468;
    let era = if z >= 0 { z } else { z - 146_096 } / 146_097;
    let doe = (z - era * 146_097) as u64;
    let yoe = (doe - doe / 1460 + doe / 36_524 - doe / 146_096) / 365;
    let y = yoe as i64 + era * 400;
    let doy = doe - (365 * yoe + yoe / 4 - yoe / 100);
    let mp = (5 * doy + 2) / 153;
    let d = (doy - (153 * mp + 2) / 5 + 1) as u32;
    let m = if mp < 10 { mp + 3 } else { mp - 9 } as u32;
    (if m <= 2 { y + 1 } else { y }, m, d)
}

// ---------------------------------------------------------------- randomness

pub fn random_hex(bytes: usize) -> String {
    let mut buf = vec![0u8; bytes];
    if let Ok(mut file) = fs::File::open("/dev/urandom") {
        if file.read_exact(&mut buf).is_ok() {
            return buf.iter().map(|b| format!("{:02x}", b)).collect();
        }
    }
    // Fallback: clock mixed with the process id. Only used if /dev/urandom is
    // missing, which should not happen on macOS or Linux.
    let seed = now_secs()
        .wrapping_mul(1_000_000_007)
        .wrapping_add(std::process::id() as u64);
    (0..bytes)
        .map(|i| format!("{:02x}", (seed >> ((i % 8) * 8)) as u8))
        .collect()
}

// ------------------------------------------------------------------- handles

pub fn norm_handle(value: &str) -> Result<String, String> {
    let trimmed = value.trim().to_lowercase();
    if trimmed.is_empty() {
        return Ok(OWNER.to_string());
    }
    let handle = if trimmed.starts_with('@') {
        trimmed
    } else {
        format!("@{}", trimmed)
    };
    let body = &handle[1..];
    let ok = !body.is_empty()
        && body.len() <= 40
        && body.chars().next().is_some_and(|c| c.is_ascii_alphanumeric())
        && body
            .chars()
            .all(|c| c.is_ascii_lowercase() || c.is_ascii_digit() || matches!(c, '.' | '_' | '-'));
    if ok {
        Ok(handle)
    } else {
        Err(format!("invalid handle: {:?} (use @name with a-z, 0-9, dot, dash)", value))
    }
}

pub fn mentions(text: &str) -> Vec<String> {
    let mut found: Vec<String> = Vec::new();
    let bytes: Vec<char> = text.chars().collect();
    let mut index = 0;
    while index < bytes.len() {
        if bytes[index] == '@' {
            let previous_is_word = index > 0
                && (bytes[index - 1].is_alphanumeric() || bytes[index - 1] == '@' || bytes[index - 1] == '_');
            let mut end = index + 1;
            while end < bytes.len() {
                let c = bytes[end];
                if c.is_ascii_alphanumeric() || matches!(c, '.' | '_' | '-') {
                    end += 1;
                } else {
                    break;
                }
            }
            if !previous_is_word && end > index + 1 {
                let raw: String = bytes[index + 1..end].iter().collect::<String>().to_lowercase();
                let candidate = format!("@{}", raw);
                if candidate != OWNER && norm_handle(&candidate).is_ok() && !found.contains(&candidate) {
                    found.push(candidate);
                }
            }
            index = end.max(index + 1);
        } else {
            index += 1;
        }
    }
    found
}

// -------------------------------------------------------------------- models

#[derive(Serialize, Deserialize, Clone, Debug, Default)]
pub struct Version {
    pub n: u64,
    pub created: String,
    pub bytes: u64,
    pub sha256: String,
    #[serde(default)]
    pub note: Option<String>,
    #[serde(default)]
    pub by: Option<String>,
}

#[derive(Serialize, Deserialize, Clone, Debug, Default)]
pub struct Session {
    pub id: String,
    pub handle: String,
    #[serde(default)]
    pub cwd: Option<String>,
    pub role: String,
    #[serde(rename = "attachedAt")]
    pub attached_at: String,
    #[serde(rename = "lastSeen")]
    pub last_seen: String,
}

#[derive(Serialize, Deserialize, Clone, Debug, Default)]
pub struct Presence {
    pub session: String,
    #[serde(rename = "leaseUntil")]
    pub lease_until: f64,
    #[serde(rename = "lastSeen")]
    pub last_seen: String,
    pub seconds: u64,
}

#[derive(Serialize, Deserialize, Clone, Debug, Default)]
pub struct Claim {
    #[serde(default)]
    pub session: Option<String>,
    pub at: String,
}

#[derive(Serialize, Deserialize, Clone, Debug, Default)]
pub struct Anchor {
    #[serde(default)]
    pub selector: String,
    #[serde(default)]
    pub quote: String,
    #[serde(default)]
    pub x: f64,
    #[serde(default)]
    pub y: f64,
}

#[derive(Serialize, Deserialize, Clone, Debug, Default)]
pub struct Reply {
    pub id: String,
    pub author: String,
    pub body: String,
    pub created: String,
}

#[derive(Serialize, Deserialize, Clone, Debug, Default)]
pub struct Comment {
    pub id: String,
    pub seq: u64,
    pub version: u64,
    pub author: String,
    pub body: String,
    #[serde(default)]
    pub anchor: Option<Anchor>,
    pub status: String,
    pub created: String,
    #[serde(default)]
    pub to: Vec<String>,
    #[serde(default)]
    pub claims: BTreeMap<String, Claim>,
    #[serde(default)]
    pub replies: Vec<Reply>,
    #[serde(default, rename = "resolvedBy", skip_serializing_if = "Option::is_none")]
    pub resolved_by: Option<String>,
    #[serde(default, rename = "resolvedAt", skip_serializing_if = "Option::is_none")]
    pub resolved_at: Option<String>,
}

#[derive(Serialize, Deserialize, Clone, Debug, Default)]
pub struct Artifact {
    pub slug: String,
    pub title: String,
    pub created: String,
    #[serde(default)]
    pub updated: String,
    pub versions: Vec<Version>,
    #[serde(default)]
    pub origin: Option<Value>,
    #[serde(default)]
    pub sessions: Vec<Session>,
    #[serde(default)]
    pub presence: BTreeMap<String, Presence>,
    #[serde(default)]
    pub comments: Vec<Comment>,
    #[serde(default, rename = "nextSeq")]
    pub next_seq: u64,
}

impl Artifact {
    pub fn current_version(&self) -> u64 {
        self.versions.iter().map(|v| v.n).max().unwrap_or(0)
    }

    pub fn handles(&self) -> Vec<String> {
        let mut out: Vec<String> = Vec::new();
        for session in &self.sessions {
            if !out.contains(&session.handle) {
                out.push(session.handle.clone());
            }
        }
        out
    }

    fn normalize(&mut self) {
        let next = self.comments.iter().map(|c| c.seq).max().unwrap_or(0) + 1;
        if self.next_seq < next {
            self.next_seq = next;
        }
        if self.next_seq == 0 {
            self.next_seq = 1;
        }
    }
}

// --------------------------------------------------------------------- store

static LOCK: OnceLock<Mutex<()>> = OnceLock::new();

/// Cross-process lock, so two servers on one state directory cannot both read a
/// file, change it and write it back. Advisory, per state directory.
pub struct ProcessLock(std::fs::File);

impl Drop for ProcessLock {
    fn drop(&mut self) {
        unsafe {
            libc::flock(std::os::unix::io::AsRawFd::as_raw_fd(&self.0), libc::LOCK_UN);
        }
    }
}

pub fn process_lock() -> Option<ProcessLock> {
    let dir = state_dir();
    let _ = fs::create_dir_all(&dir);
    let file = fs::OpenOptions::new()
        .create(true)
        .truncate(false)
        .write(true)
        .open(dir.join(".lock"))
        .ok()?;
    let code = unsafe { libc::flock(std::os::unix::io::AsRawFd::as_raw_fd(&file), libc::LOCK_EX) };
    if code != 0 {
        return None;
    }
    Some(ProcessLock(file))
}

pub fn lock() -> std::sync::MutexGuard<'static, ()> {
    LOCK.get_or_init(|| Mutex::new(()))
        .lock()
        .unwrap_or_else(|poisoned| poisoned.into_inner())
}

pub fn valid_slug(slug: &str) -> bool {
    if slug.is_empty() || slug.len() > 64 {
        return false;
    }
    let first = slug.chars().next().unwrap_or(' ');
    let starts_ok = first.is_ascii_lowercase() || first.is_ascii_digit();
    starts_ok
        && slug
            .chars()
            .all(|c| c.is_ascii_lowercase() || c.is_ascii_digit() || c == '-')
}

pub fn slugify(text: &str) -> String {
    let mut out = String::new();
    let mut last_dash = false;
    for c in text.to_lowercase().chars() {
        if c.is_ascii_alphanumeric() {
            out.push(c);
            last_dash = false;
        } else if !last_dash {
            out.push('-');
            last_dash = true;
        }
        if out.len() >= 48 {
            break;
        }
    }
    let trimmed = out.trim_matches('-').to_string();
    if trimmed.is_empty() {
        "artifact".into()
    } else {
        trimmed
    }
}

fn read_json(path: &Path) -> Option<Value> {
    fs::read_to_string(path).ok().and_then(|text| serde_json::from_str(&text).ok())
}

fn write_json(path: &Path, value: &Value) -> std::io::Result<()> {
    if let Some(parent) = path.parent() {
        // Never resurrect an artifact directory that a delete removed while this
        // request held an older copy.
        if !parent.is_dir() {
            return Err(std::io::Error::new(
                std::io::ErrorKind::NotFound,
                "artifact directory is gone",
            ));
        }
    }
    let tmp = path.with_extension(format!("json.{}.tmp", std::process::id()));
    let text = format!("{}\n", serde_json::to_string_pretty(value).unwrap_or_default());
    fs::write(&tmp, text)?;
    fs::rename(tmp, path)
}

fn artifact_path(slug: &str) -> PathBuf {
    state_dir().join(slug).join("artifact.json")
}

pub fn load_artifact(slug: &str) -> Option<Artifact> {
    if !valid_slug(slug) {
        return None;
    }
    let value = read_json(&artifact_path(slug))?;
    let mut artifact: Artifact = serde_json::from_value(value).ok()?;
    if artifact.slug != slug {
        return None;
    }
    artifact.normalize();
    Some(artifact)
}

pub fn save_artifact(artifact: &mut Artifact) -> std::io::Result<()> {
    artifact.updated = now_iso();
    let value = serde_json::to_value(&*artifact).map_err(|e| std::io::Error::new(std::io::ErrorKind::Other, e))?;
    write_json(&artifact_path(&artifact.slug), &value)
}

pub fn all_artifacts() -> Vec<Artifact> {
    let root = state_dir();
    let mut out = Vec::new();
    if let Ok(entries) = fs::read_dir(&root) {
        for entry in entries.flatten() {
            if !entry.path().is_dir() {
                continue;
            }
            let slug = entry.file_name().to_string_lossy().to_string();
            if let Some(artifact) = load_artifact(&slug) {
                out.push(artifact);
            }
        }
    }
    out.sort_by(|a, b| b.updated.cmp(&a.updated));
    out
}

pub fn version_file(slug: &str, number: u64) -> PathBuf {
    state_dir().join(slug).join("versions").join(format!("{}.html", number))
}

pub fn read_version(slug: &str, number: u64) -> Option<String> {
    let artifact = load_artifact(slug)?;
    if !artifact.versions.iter().any(|v| v.n == number) {
        return None;
    }
    fs::read_to_string(version_file(slug, number)).ok()
}

fn unique_slug(base: &str) -> String {
    let taken: Vec<String> = all_artifacts().into_iter().map(|a| a.slug).collect();
    if !taken.contains(&base.to_string()) {
        return base.to_string();
    }
    let mut n = 2;
    loop {
        let candidate = format!("{}-{}", base, n);
        if !taken.contains(&candidate) {
            return candidate;
        }
        n += 1;
    }
}

// ------------------------------------------------------------------ sessions

pub fn find_session<'a>(artifact: &'a Artifact, handle: &str, session: Option<&str>) -> Option<&'a Session> {
    if let Some(id) = session {
        if let Some(found) = artifact.sessions.iter().find(|s| s.id == id) {
            return Some(found);
        }
    }
    artifact
        .sessions
        .iter()
        .find(|s| s.handle == handle && s.role == "origin")
        .or_else(|| artifact.sessions.iter().find(|s| s.handle == handle))
}

pub fn attach(
    artifact: &mut Artifact,
    handle: &str,
    session: Option<&str>,
    cwd: Option<&str>,
    role: &str,
) -> Result<Session, String> {
    let handle = norm_handle(handle)?;
    let now = now_iso();
    if let Some(id) = session.map(|s| s.trim()).filter(|s| !s.is_empty()) {
        if let Some(existing) = artifact.sessions.iter_mut().find(|s| s.id == id) {
            if existing.handle != handle {
                return Err(format!("session {} is already attached as {}", id, existing.handle));
            }
            existing.last_seen = now;
            if let Some(cwd) = cwd {
                existing.cwd = Some(cwd.to_string());
            }
            return Ok(existing.clone());
        }
        let record = Session {
            id: id.to_string(),
            handle,
            cwd: cwd.map(|s| s.to_string()),
            role: role.to_string(),
            attached_at: now.clone(),
            last_seen: now,
        };
        artifact.sessions.push(record.clone());
        return Ok(record);
    }
    for wanted in ["origin", "attached"] {
        if let Some(existing) = artifact
            .sessions
            .iter_mut()
            .find(|s| s.handle == handle && s.role == wanted)
        {
            existing.last_seen = now;
            if let Some(cwd) = cwd {
                existing.cwd = Some(cwd.to_string());
            }
            return Ok(existing.clone());
        }
    }
    let record = Session {
        id: format!("s-{}", random_hex(4)),
        handle,
        cwd: cwd.map(|s| s.to_string()),
        role: role.to_string(),
        attached_at: now.clone(),
        last_seen: now,
    };
    artifact.sessions.push(record.clone());
    Ok(record)
}

pub fn detach(artifact: &mut Artifact, handle: Option<&str>, session: Option<&str>) -> Result<Vec<Session>, String> {
    let wanted_handle = match handle {
        Some(value) => Some(norm_handle(value)?),
        None => None,
    };
    let mut removed = Vec::new();
    let mut kept = Vec::new();
    for record in std::mem::take(&mut artifact.sessions) {
        let matches = session.map(|id| record.id == id).unwrap_or(false)
            || wanted_handle.as_ref().map(|h| &record.handle == h).unwrap_or(false);
        if matches {
            artifact.presence.remove(&record.handle);
            removed.push(record);
        } else {
            kept.push(record);
        }
    }
    artifact.sessions = kept;
    if removed.is_empty() {
        return Err("no such session".into());
    }
    Ok(removed)
}

pub fn heartbeat(artifact: &mut Artifact, handle: &str, session: Option<&str>, lease: Option<u64>) -> Presence {
    let entry = attach(artifact, handle, session, None, "attached").unwrap_or_else(|_| Session {
        id: format!("s-{}", random_hex(4)),
        handle: norm_handle(handle).unwrap_or_else(|_| OWNER.into()),
        cwd: None,
        role: "attached".into(),
        attached_at: now_iso(),
        last_seen: now_iso(),
    });
    let seconds = lease.unwrap_or_else(lease_seconds).clamp(2, 300);
    let presence = Presence {
        session: entry.id,
        lease_until: now_secs() as f64 + seconds as f64,
        last_seen: now_iso(),
        seconds,
    };
    artifact.presence.insert(entry.handle, presence.clone());
    presence
}

pub fn is_live(artifact: &Artifact, handle: &str) -> bool {
    artifact
        .presence
        .get(handle)
        .map(|entry| entry.lease_until > now_secs() as f64)
        .unwrap_or(false)
}

// ------------------------------------------------------------------ comments

pub fn sanitize_anchor(value: Option<&Value>) -> Option<Anchor> {
    let value = value?.as_object()?;
    let text = |key: &str, limit: usize| {
        value
            .get(key)
            .and_then(|v| v.as_str())
            .unwrap_or("")
            .chars()
            .take(limit)
            .collect::<String>()
    };
    let fraction = |key: &str| {
        value
            .get(key)
            .and_then(|v| v.as_f64())
            .filter(|n| n.is_finite())
            .unwrap_or(0.5)
    };
    Some(Anchor {
        selector: text("selector", 600),
        quote: text("quote", 300),
        x: fraction("x"),
        y: fraction("y"),
    })
}

pub fn add_comment(
    slug: &str,
    body: &str,
    author: &str,
    version: Option<u64>,
    anchor: Option<Anchor>,
    to: Option<Vec<String>>,
) -> Result<Value, String> {
    let body = body.trim();
    if body.is_empty() {
        return Err("body is required".into());
    }
    if body.chars().count() > max_comment() {
        return Err(format!("comment is longer than {} characters", max_comment()));
    }
    let _guard = lock();
    let _process = process_lock();
    let mut artifact = load_artifact(slug).ok_or("not_found")?;
    let number = match version {
        Some(value) if artifact.versions.iter().any(|v| v.n == value) => value,
        _ => artifact.current_version(),
    };
    let mut targets: Vec<String> = Vec::new();
    if let Some(list) = to {
        for value in list {
            let handle = norm_handle(&value)?;
            if handle != OWNER && !targets.contains(&handle) {
                targets.push(handle);
            }
        }
    }
    for handle in mentions(body) {
        if !targets.contains(&handle) {
            targets.push(handle);
        }
    }
    if targets.is_empty() {
        let handles = artifact.handles();
        let origin_handle = artifact
            .origin
            .as_ref()
            .and_then(|origin| origin.get("handle"))
            .and_then(|value| value.as_str())
            .map(|s| s.to_string());
        targets = match origin_handle {
            Some(handle) => vec![handle],
            None => handles,
        };
    }
    let comment = Comment {
        id: format!("c{}", random_hex(3)),
        seq: artifact.next_seq.max(1),
        version: number,
        author: norm_handle(author)?,
        body: body.to_string(),
        anchor,
        status: "open".into(),
        created: now_iso(),
        to: targets,
        claims: BTreeMap::new(),
        replies: Vec::new(),
        resolved_by: None,
        resolved_at: None,
    };
    artifact.next_seq = comment.seq + 1;
    artifact.comments.push(comment.clone());
    save_artifact(&mut artifact).map_err(|e| e.to_string())?;
    Ok(delivery_view(&artifact, &comment))
}

pub fn delivery_view(artifact: &Artifact, comment: &Comment) -> Value {
    let mut delivery = Vec::new();
    if comment.to.is_empty() {
        for (handle, claim) in &comment.claims {
            delivery.push(json!({
                "handle": handle,
                "state": "claimed",
                "live": is_live(artifact, handle),
                "session": claim.session,
                "at": claim.at,
            }));
        }
    } else {
        for handle in &comment.to {
            let claim = comment.claims.get(handle);
            let state = if comment.status == "resolved" {
                "resolved"
            } else if claim.is_some() {
                "claimed"
            } else if is_live(artifact, handle) {
                "queued"
            } else {
                "waiting"
            };
            delivery.push(json!({
                "handle": handle,
                "state": state,
                "live": is_live(artifact, handle),
                "session": claim.and_then(|c| c.session.clone()),
                "at": claim.map(|c| c.at.clone()),
            }));
        }
    }
    let mut value = serde_json::to_value(comment).unwrap_or_else(|_| json!({}));
    if let Some(map) = value.as_object_mut() {
        map.insert("delivery".into(), Value::Array(delivery));
    }
    value
}

pub fn list_comments(slug: &str, since: Option<u64>, open_only: bool) -> Result<Vec<Value>, String> {
    let artifact = load_artifact(slug).ok_or("not_found")?;
    let out = artifact
        .comments
        .iter()
        .filter(|c| since.map(|value| c.seq > value).unwrap_or(true))
        .filter(|c| !open_only || c.status != "resolved")
        .map(|c| delivery_view(&artifact, c))
        .collect();
    Ok(out)
}

pub fn find_comment(artifact: &Artifact, id: &str) -> Option<Comment> {
    artifact.comments.iter().find(|c| c.id == id).cloned()
}

pub fn artifact_of_comment(id: &str) -> Option<Artifact> {
    all_artifacts().into_iter().find(|artifact| artifact.comments.iter().any(|c| c.id == id))
}

pub fn reply(comment_id: &str, body: &str, author: &str) -> Result<Value, String> {
    let body = body.trim();
    if body.is_empty() {
        return Err("body is required".into());
    }
    if body.chars().count() > max_comment() {
        return Err(format!("reply is longer than {} characters", max_comment()));
    }
    let _guard = lock();
    let _process = process_lock();
    let mut artifact = artifact_of_comment(comment_id).ok_or("not_found")?;
    let item = Reply {
        id: format!("r{}", random_hex(3)),
        author: norm_handle(author).unwrap_or_else(|_| "@agent".into()),
        body: body.to_string(),
        created: now_iso(),
    };
    if let Some(comment) = artifact.comments.iter_mut().find(|c| c.id == comment_id) {
        comment.replies.push(item.clone());
    }
    save_artifact(&mut artifact).map_err(|e| e.to_string())?;
    Ok(serde_json::to_value(item).unwrap_or_else(|_| json!({})))
}

pub fn set_status(comment_id: &str, status: &str, author: &str) -> Result<Value, String> {
    if status != "open" && status != "resolved" {
        return Err("status must be open or resolved".into());
    }
    let _guard = lock();
    let _process = process_lock();
    let mut artifact = artifact_of_comment(comment_id).ok_or("not_found")?;
    let mut updated = None;
    if let Some(comment) = artifact.comments.iter_mut().find(|c| c.id == comment_id) {
        comment.status = status.to_string();
        if status == "resolved" {
            comment.resolved_by = Some(norm_handle(author).unwrap_or_else(|_| OWNER.into()));
            comment.resolved_at = Some(now_iso());
        } else {
            comment.resolved_by = None;
            comment.resolved_at = None;
        }
        updated = Some(comment.clone());
    }
    let comment = updated.ok_or("not_found")?;
    save_artifact(&mut artifact).map_err(|e| e.to_string())?;
    Ok(delivery_view(&artifact, &comment))
}

pub fn release(comment_id: &str, handle: &str) -> Result<Value, String> {
    let handle = norm_handle(handle)?;
    let _guard = lock();
    let _process = process_lock();
    let mut artifact = artifact_of_comment(comment_id).ok_or("not_found")?;
    let mut updated = None;
    if let Some(comment) = artifact.comments.iter_mut().find(|c| c.id == comment_id) {
        if comment.claims.remove(&handle).is_none() {
            return Err("not_claimed".into());
        }
        updated = Some(comment.clone());
    }
    let comment = updated.ok_or("not_found")?;
    save_artifact(&mut artifact).map_err(|e| e.to_string())?;
    Ok(delivery_view(&artifact, &comment))
}

pub struct InboxQuery {
    pub handle: String,
    pub session: Option<String>,
    pub wait: u64,
    pub lease: Option<u64>,
    pub include_all: bool,
}

pub fn inbox(slug: &str, query: InboxQuery) -> Result<Value, String> {
    let handle = norm_handle(&query.handle)?;
    let deadline = now_secs() + query.wait.min(max_wait());
    let mut renewed = 0u64;
    loop {
        {
            let _guard = lock();
            let _process = process_lock();
            let mut artifact = load_artifact(slug).ok_or("not_found")?;
            if renewed == 0 || now_secs().saturating_sub(renewed) > 2 {
                heartbeat(&mut artifact, &handle, query.session.as_deref(), query.lease);
                save_artifact(&mut artifact).map_err(|e| e.to_string())?;
                renewed = now_secs();
            }
            // An unattached session id still echoes the handle's own session, the
            // way attach_find falls back.
            let session_id = find_session(&artifact, &handle, query.session.as_deref())
                .map(|s| s.id.clone());
            let mut claimed: Vec<Comment> = Vec::new();
            for comment in artifact.comments.iter_mut() {
                if comment.status == "resolved" {
                    continue;
                }
                let addressed = comment.to.iter().any(|h| h == &handle);
                let broadcast = comment.to.is_empty() && comment.claims.is_empty();
                let others = query.include_all && !comment.to.is_empty() && !addressed;
                if !(addressed || broadcast || others) {
                    continue;
                }
                if comment.claims.contains_key(&handle) {
                    continue;
                }
                comment.claims.insert(
                    handle.clone(),
                    Claim { session: session_id.clone(), at: now_iso() },
                );
                claimed.push(comment.clone());
            }
            if !claimed.is_empty() {
                save_artifact(&mut artifact).map_err(|e| e.to_string())?;
                let views: Vec<Value> = claimed
                    .iter()
                    .map(|c| delivery_view(&artifact, c))
                    .collect();
                return Ok(json!({
                    "slug": slug,
                    "handle": handle,
                    "session": session_id,
                    "comments": views,
                    "presence": artifact.presence.get(&handle),
                }));
            }
            if now_secs() >= deadline {
                return Ok(json!({
                    "slug": slug,
                    "handle": handle,
                    "session": session_id,
                    "comments": [],
                    "presence": Value::Null,
                }));
            }
        }
        std::thread::sleep(std::time::Duration::from_millis(250));
    }
}

// ----------------------------------------------------------------- publishing

pub struct PublishInput {
    pub html: String,
    pub title: Option<String>,
    pub slug: Option<String>,
    pub note: Option<String>,
    pub handle: Option<String>,
    pub session: Option<String>,
    pub cwd: Option<String>,
}

pub fn publish(input: PublishInput) -> Result<Value, String> {
    if input.html.trim().is_empty() {
        return Err("html is required".into());
    }
    let bytes = input.html.as_bytes();
    if bytes.len() > max_html() {
        return Err(format!("artifact is larger than {} bytes", max_html()));
    }
    let _guard = lock();
    let _process = process_lock();
    let mut artifact = match input.slug.as_deref() {
        Some(slug) => {
            if !valid_slug(slug) {
                return Err("slug must match [a-z0-9][a-z0-9-]*".into());
            }
            load_artifact(slug)
        }
        None => None,
    };
    if artifact.is_none() && all_artifacts().len() >= max_artifacts() {
        return Err(format!("artifact limit reached ({})", max_artifacts()));
    }
    let mut artifact = match artifact {
        Some(existing) => existing,
        None => {
            let name = match input.slug.clone() {
                Some(slug) => slug,
                None => unique_slug(&slugify(input.title.as_deref().unwrap_or("artifact"))),
            };
            let now = now_iso();
            Artifact {
                slug: name.clone(),
                title: input.title.clone().unwrap_or(name),
                created: now.clone(),
                updated: now,
                versions: Vec::new(),
                origin: None,
                sessions: Vec::new(),
                presence: BTreeMap::new(),
                comments: Vec::new(),
                next_seq: 1,
            }
        }
    };
    let number = artifact.current_version() + 1;
    let path = version_file(&artifact.slug, number);
    if let Some(parent) = path.parent() {
        fs::create_dir_all(parent).map_err(|e| e.to_string())?;
    }
    fs::write(&path, bytes).map_err(|e| e.to_string())?;
    if let Some(title) = input.title.clone() {
        artifact.title = title;
    }
    let digest = Sha256::digest(bytes);
    artifact.versions.push(Version {
        n: number,
        created: now_iso(),
        bytes: bytes.len() as u64,
        sha256: digest.iter().map(|b| format!("{:02x}", b)).collect(),
        note: input.note.clone(),
        by: match input.handle.as_deref() {
            Some(handle) => Some(norm_handle(handle)?),
            None => None,
        },
    });
    let mut session_id = None;
    if let Some(handle) = input.handle.clone() {
        let record = attach(&mut artifact, &handle, input.session.as_deref(), input.cwd.as_deref(), "origin")?;
        session_id = Some(record.id.clone());
        if artifact.origin.is_none() || number == 1 {
            artifact.origin = Some(json!({
                "handle": record.handle,
                "session": record.id,
                "cwd": input.cwd,
                "at": now_iso(),
            }));
        }
    }
    save_artifact(&mut artifact).map_err(|e| e.to_string())?;
    Ok(json!({
        "slug": artifact.slug,
        "title": artifact.title,
        "version": number,
        "versions": artifact.versions.len(),
        "url": format!("{}/a/{}", public_base(), artifact.slug),
        "raw": format!("{}/a/{}/raw?v={}", content_base(), artifact.slug, number),
        "rawBase": format!("{}/a/{}", content_base(), artifact.slug),
        "bytes": bytes.len(),
        "sha256": artifact.versions.last().map(|v| v.sha256.clone()).unwrap_or_default(),
        "session": session_id,
        "origin": artifact.origin,
    }))
}

// ------------------------------------------------------------------- reading

pub fn session_view(artifact: &Artifact) -> Vec<Value> {
    artifact
        .sessions
        .iter()
        .map(|session| {
            let presence = artifact.presence.get(&session.handle);
            let live = presence
                .map(|p| p.lease_until > now_secs() as f64 && p.session == session.id)
                .unwrap_or(false);
            let left = presence
                .map(|p| (p.lease_until - now_secs() as f64).max(0.0) as u64)
                .filter(|_| live)
                .unwrap_or(0);
            json!({
                "id": session.id,
                "handle": session.handle,
                "cwd": session.cwd,
                "role": session.role,
                "attachedAt": session.attached_at,
                "lastSeen": session.last_seen,
                "live": live,
                "leaseUntil": presence.map(|p| p.lease_until),
                "leaseLeft": left,
            })
        })
        .collect()
}

pub fn summary(artifact: &Artifact) -> Value {
    let open = artifact.comments.iter().filter(|c| c.status != "resolved").count();
    let mut value = json!({
        "slug": artifact.slug,
        "title": artifact.title,
        "created": artifact.created,
        "updated": artifact.updated,
        "currentVersion": artifact.current_version(),
        "versions": artifact.versions,
        "openComments": open,
        "totalComments": artifact.comments.len(),
        "url": format!("{}/a/{}", public_base(), artifact.slug),
        "rawBase": format!("{}/a/{}", content_base(), artifact.slug),
        "origin": artifact.origin,
        "handles": artifact.handles(),
        "sessions": session_view(artifact),
    });
    if let Some(map) = value.as_object_mut() {
        map.insert("comments".into(), Value::Array(Vec::new()));
        map.remove("comments");
    }
    value
}

pub fn summary_with_comments(artifact: &Artifact) -> Value {
    let mut value = summary(artifact);
    let comments: Vec<Value> = artifact
        .comments
        .iter()
        .map(|comment| delivery_view(artifact, comment))
        .collect();
    if let Some(map) = value.as_object_mut() {
        map.insert("comments".into(), Value::Array(comments));
    }
    value
}

pub fn delete_artifact(slug: &str) -> Result<(), String> {
    let _guard = lock();
    let _process = process_lock();
    if load_artifact(slug).is_none() {
        return Err("not_found".into());
    }
    fs::remove_dir_all(state_dir().join(slug)).map_err(|e| e.to_string())
}

// ---------------------------------------------------------------- html parts

const VIEW: &str = include_str!("web/view.html");
const OVERLAY: &str = include_str!("web/overlay.js");
const VERSION_HINT: &str = "<!-- injected by agent-artifacts -->";

pub fn view_html() -> &'static str {
    VIEW
}

pub fn overlay_js() -> &'static str {
    OVERLAY
}

pub fn inject_overlay(html: &str, slug: &str, version: u64) -> String {
    let snippet = format!(
        "\n{}\n<script src=\"/_artifacts/overlay.js\" data-slug=\"{}\" data-version=\"{}\" \
         data-parent=\"{}\"></script>\n",
        VERSION_HINT,
        escape_html(slug),
        version,
        escape_html(&public_base())
    );
    match html.to_lowercase().rfind("</body") {
        Some(index) => {
            let mut out = String::with_capacity(html.len() + snippet.len());
            out.push_str(&html[..index]);
            out.push_str(&snippet);
            out.push_str(&html[index..]);
            out
        }
        None => format!("{}{}", html, snippet),
    }
}

pub fn escape_html(value: &str) -> String {
    value
        .replace('&', "&amp;")
        .replace('<', "&lt;")
        .replace('>', "&gt;")
        .replace('"', "&quot;")
}

pub fn index_page(artifacts: &[Artifact]) -> String {
    let mut rows = String::new();
    for artifact in artifacts {
        let chips: Vec<String> = artifact
            .handles()
            .iter()
            .map(|handle| {
                let live = artifact
                    .sessions
                    .iter()
                    .any(|s| &s.handle == handle && is_live(artifact, handle));
                format!(
                    "<span class=\"chip{}\">{}</span>",
                    if live { " live" } else { "" },
                    escape_html(handle)
                )
            })
            .collect();
        let open = artifact.comments.iter().filter(|c| c.status != "resolved").count();
        rows.push_str(&format!(
            "<tr><td><a href=\"/a/{slug}\">{title}</a><div class=\"chips\">{chips}</div></td>\
             <td class=\"num\">v{n}</td><td class=\"num\">{open}</td>\
             <td class=\"muted\"><time datetime=\"{updated}\">{updated}</time></td></tr>",
            slug = escape_html(&artifact.slug),
            title = escape_html(&artifact.title),
            chips = chips.join(""),
            n = artifact.current_version(),
            open = open,
            updated = escape_html(&artifact.updated),
        ));
    }
    if rows.is_empty() {
        rows.push_str("<tr><td colspan=\"4\" class=\"muted\">No artifacts yet.</td></tr>");
    }
    format!(
        r#"<!DOCTYPE html>
<html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Artifacts</title>
<style>
 :root{{--bg:#f7f7f4;--fg:#161816;--muted:#5f655d;--line:#d8dbd5;--card:#fff;--accent:#2a5db0}}
 @media (prefers-color-scheme:dark){{:root{{--bg:#0f1110;--fg:#e9ebe5;--muted:#98a094;
   --line:#2a2e28;--card:#171a16;--accent:#84aaee}}}}
 body{{margin:0;padding:28px 16px;background:var(--bg);color:var(--fg);
   font:15px/1.5 ui-serif,Georgia,serif}}
 .wrap{{max-width:900px;margin:0 auto}}
 h1{{font-size:1.4rem;margin:0 0 4px}}
 p.sub{{color:var(--muted);font-size:.85rem;margin:0 0 22px}}
 table{{border-collapse:collapse;width:100%;background:var(--card);border:1px solid var(--line);
   border-radius:8px;overflow:hidden;font:.9rem/1.4 ui-sans-serif,system-ui,sans-serif}}
 th,td{{text-align:left;padding:9px 12px;border-bottom:1px solid var(--line)}}
 th{{font-size:.72rem;text-transform:uppercase;letter-spacing:.06em;color:var(--muted)}}
 tr:last-child td{{border-bottom:none}}
 td.num{{text-align:right;font-variant-numeric:tabular-nums}}
 .muted{{color:var(--muted)}}
 td.muted{{white-space:nowrap}}
 .chips{{margin-top:3px}}
 .chip{{display:inline-block;font-size:.7rem;border:1px solid var(--line);border-radius:999px;
   padding:0 6px;margin-right:4px;color:var(--muted)}}
 .chip.live{{color:var(--accent);border-color:var(--accent)}}
 a{{color:var(--accent)}}
</style></head><body><div class="wrap">
<h1>Artifacts</h1>
<p class="sub">Local, reviewable pages published by coding agents. Attach a session, comment on
elements, mention an agent with @name.</p>
<table><thead><tr><th>Artifact</th><th>Version</th><th>Open</th><th>Updated</th></tr></thead>
<tbody>{rows}</tbody></table></div>
<script>
 document.querySelectorAll("time[datetime]").forEach(function (node) {{
   var when = new Date(node.getAttribute("datetime"));
   if (!isNaN(when)) node.textContent = when.toLocaleString();
 }});
</script></body></html>"#,
        rows = rows
    )
}

pub fn health(artifacts: usize) -> Value {
    json!({
        "ok": true,
        "artifacts": artifacts,
        "dir": state_dir().to_string_lossy(),
        "leaseSeconds": lease_seconds(),
    })
}

// ------------------------------------------------------------------ requests

pub struct Request {
    pub method: String,
    pub path: String,
    pub query: BTreeMap<String, String>,
    pub headers: BTreeMap<String, String>,
    pub body: Vec<u8>,
}

impl Request {
    pub fn body_json(&self) -> Result<Map<String, Value>, String> {
        if self.body.is_empty() {
            return Ok(Map::new());
        }
        let value: Value = serde_json::from_slice(&self.body).map_err(|_| "body must be JSON".to_string())?;
        value
            .as_object()
            .cloned()
            .ok_or_else(|| "body must be a JSON object".to_string())
    }

    pub fn query_int(&self, key: &str) -> Option<u64> {
        self.query.get(key).and_then(|value| value.parse::<u64>().ok())
    }

    pub fn query_flag(&self, key: &str) -> bool {
        matches!(self.query.get(key).map(|v| v.as_str()), Some("1") | Some("true") | Some("True"))
    }
}

pub struct Response {
    pub status: u16,
    pub content_type: String,
    pub body: Vec<u8>,
    pub headers: Vec<(String, String)>,
}

impl Response {
    pub fn json(status: u16, value: &Value) -> Self {
        Response {
            status,
            content_type: "application/json".into(),
            body: serde_json::to_vec(value).unwrap_or_default(),
            headers: Vec::new(),
        }
    }

    pub fn error(status: u16, code: &str, message: Option<&str>) -> Self {
        let mut map = Map::new();
        map.insert("error".into(), json!(code));
        if let Some(message) = message {
            map.insert("message".into(), json!(message));
        }
        Self::json(status, &Value::Object(map))
    }

    pub fn html(status: u16, body: String, csp: Option<String>) -> Self {
        let mut headers = Vec::new();
        if let Some(csp) = csp {
            headers.push(("content-security-policy".to_string(), csp));
        }
        Response {
            status,
            content_type: "text/html; charset=utf-8".into(),
            body: body.into_bytes(),
            headers,
        }
    }

    pub fn text_body(status: u16, content_type: &str, body: String) -> Self {
        Response {
            status,
            content_type: content_type.into(),
            body: body.into_bytes(),
            headers: Vec::new(),
        }
    }
}
