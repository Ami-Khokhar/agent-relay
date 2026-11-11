//! artifactd: local-only reviewable artifacts for any coding agent.
//!
//! One binary holds the service and the client. `artifactd serve` runs the two
//! loopback listeners; every other subcommand is the CLI an agent calls.

mod http;
mod store;

use serde_json::{json, Map, Value};
use std::collections::BTreeMap;
use std::net::TcpListener;
use std::path::PathBuf;
use std::sync::Arc;
use std::time::Duration;

use store::*;

// --------------------------------------------------------------- api routing

fn api_route(request: &Request) -> Response {
    if !matches!(request.method.as_str(), "GET" | "POST" | "DELETE") {
        return Response::error(501, "unsupported_method", None);
    }
    let needs_guard = matches!(request.method.as_str(), "POST" | "DELETE");
    if needs_guard && request.headers.get("x-artifacts-client").map(|v| v.as_str()) != Some("1") {
        return Response::error(
            400,
            "missing_client_header",
            Some("state changing calls need the x-artifacts-client header"),
        );
    }
    if request.body.len() > max_body() {
        return Response::error(413, "request_failed", Some("body too large"));
    }
    let path = request.path.as_str();
    let method = request.method.as_str();
    // The Python routes are anchored regexes: a trailing slash is a different path.
    if path.len() > 1 && path.ends_with('/') {
        return Response::error(404, "not_found", None);
    }

    match (method, path) {
        ("GET", "/healthz") => Response::json(200, &health(all_artifacts().len())),
        ("GET", "/") => Response::html(200, index_page(&all_artifacts()), None),
        ("GET", "/_artifacts/overlay.js") => Response::text_body(
            200,
            "application/javascript; charset=utf-8",
            overlay_js().to_string(),
        ),
        ("GET", "/api/artifacts") => {
            let items: Vec<Value> = all_artifacts().iter().map(summary).collect();
            Response::json(200, &json!({ "artifacts": items }))
        }
        ("POST", "/api/artifacts") => match request.body_json() {
            Err(message) => Response::error(400, "invalid_request", Some(&message)),
            Ok(body) => {
                let input = PublishInput {
                    html: body.get("html").and_then(|v| v.as_str()).unwrap_or("").to_string(),
                    title: body.get("title").and_then(|v| v.as_str()).map(|s| s.to_string()),
                    slug: body.get("slug").and_then(|v| v.as_str()).map(|s| s.to_string()),
                    note: body.get("note").and_then(|v| v.as_str()).map(|s| s.to_string()),
                    handle: body
                        .get("as")
                        .or_else(|| body.get("handle"))
                        .and_then(|v| v.as_str())
                        .map(|s| s.to_string()),
                    session: body.get("session").and_then(|v| v.as_str()).map(|s| s.to_string()),
                    cwd: body.get("cwd").and_then(|v| v.as_str()).map(|s| s.to_string()),
                };
                match publish(input) {
                    Ok(value) => Response::json(201, &value),
                    Err(message) if message == "not_found" => Response::error(404, "not_found", None),
                    Err(message) => Response::error(400, "invalid_publish", Some(&message)),
                }
            }
        },
        _ => route_artifact(request),
    }
}

fn route_artifact(request: &Request) -> Response {
    let path = request.path.as_str();
    let method = request.method.as_str();
    let segments: Vec<&str> = path.trim_matches('/').split('/').collect();

    if segments.first() == Some(&"api") {
        if segments.get(1) == Some(&"comments") {
            let id = segments.get(2).copied().unwrap_or("");
            let action = segments.get(3).copied().unwrap_or("");
            if segments.len() != 4 || !matches!(action, "reply" | "status" | "release") {
                return Response::error(404, "not_found", None);
            }
            if method != "POST" {
                return Response::error(405, "method_not_allowed", None);
            }
            let body = match request.body_json() {
                Ok(body) => body,
                Err(message) => return Response::error(400, "invalid_request", Some(&message)),
            };
            let author = body
                .get("author")
                .and_then(|v| v.as_str())
                .unwrap_or("agent");
            let result = match action {
                "reply" => reply(id, body.get("body").and_then(|v| v.as_str()).unwrap_or(""), author),
                "status" => set_status(id, body.get("status").and_then(|v| v.as_str()).unwrap_or(""), author),
                "release" => release(
                    id,
                    body.get("author")
                        .and_then(|v| v.as_str())
                        .filter(|value| !value.trim().is_empty())
                        .unwrap_or(OWNER),
                ),
                _ => return Response::error(404, "not_found", None),
            };
            return match result {
                Ok(value) => Response::json(200, &value),
                Err(message) if message == "not_found" => Response::error(404, "not_found", None),
                Err(message) if message == "not_claimed" => Response::error(404, "not_found", None),
                Err(message) => Response::error(400, "invalid_request", Some(&message)),
            };
        }

        if segments.get(1) != Some(&"artifacts") {
            return Response::error(404, "not_found", None);
        }
        let slug = match segments.get(2) {
            Some(slug) => *slug,
            None => {
                return match method {
                    "GET" => {
                        let items: Vec<Value> = all_artifacts().iter().map(summary).collect();
                        Response::json(200, &json!({ "artifacts": items }))
                    }
                    _ => Response::error(405, "method_not_allowed", None),
                }
            }
        };
        let current = match load_artifact(slug) {
            Some(artifact) => artifact,
            None => return Response::error(404, "not_found", None),
        };
        let tail = segments.get(3).copied().unwrap_or("");
        // Only /comments, /sessions and /inbox are real sub-paths; anything deeper
        // is a different route.
        let deepest = if tail == "sessions" { 5 } else { 4 };
        if segments.len() > deepest {
            return Response::error(404, "not_found", None);
        }
        if !tail.is_empty()
            && segments.len() == 4
            && !matches!(tail, "comments" | "sessions" | "inbox")
        {
            return Response::error(404, "not_found", None);
        }
        match (method, tail) {
            ("GET", "") => Response::json(200, &summary_with_comments(&current)),
            ("DELETE", "") => match delete_artifact(slug) {
                Ok(()) => Response::json(200, &json!({ "deleted": slug })),
                Err(_) => Response::error(404, "not_found", None),
            },
            ("GET", "comments") => {
                let since = match request.query.get("since") {
                    None => None,
                    Some(raw) => match raw.parse::<u64>() {
                        Ok(value) => Some(value),
                        Err(_) => return Response::error(400, "invalid_since", None),
                    },
                };
                let open_only = request.query_flag("open");
                let wait = match request.query.get("wait") {
                    None => 0,
                    Some(raw) => match raw.parse::<u64>() {
                        Ok(value) => value,
                        Err(_) => return Response::error(400, "invalid_wait", None),
                    },
                };
                if wait > 0 {
                    // Read-only wait: no handle, no claim.
                    let deadline = now_secs() + wait.min(max_wait());
                    loop {
                        match list_comments(slug, since, open_only) {
                            Ok(comments) if !comments.is_empty() || now_secs() >= deadline => {
                                return Response::json(
                                    200,
                                    &json!({ "slug": slug, "comments": comments, "count": comments.len() }),
                                )
                            }
                            Ok(_) => std::thread::sleep(Duration::from_millis(250)),
                            Err(_) => return Response::error(404, "not_found", None),
                        }
                    }
                }
                match list_comments(slug, since, open_only) {
                    Ok(comments) => Response::json(
                        200,
                        &json!({ "slug": slug, "comments": comments, "count": comments.len() }),
                    ),
                    Err(_) => Response::error(404, "not_found", None),
                }
            }
            ("POST", "comments") => match request.body_json() {
                Err(message) => Response::error(400, "invalid_request", Some(&message)),
                Ok(body) => {
                    let anchor = body.get("anchor").and_then(|value| serde_json::from_value::<Anchor>(value.clone()).ok());
                    let anchor = sanitize_anchor(body.get("anchor"));
                    let to = match body.get("to") {
                        None | Some(Value::Null) => None,
                        Some(Value::Array(list)) => {
                            let mut values = Vec::new();
                            for item in list {
                                match item.as_str() {
                                    Some(text) => values.push(text.to_string()),
                                    None => {
                                        return Response::error(
                                            400,
                                            "invalid_comment",
                                            Some("to must be a list of handles"),
                                        )
                                    }
                                }
                            }
                            Some(values)
                        }
                        Some(_) => {
                            return Response::error(
                                400,
                                "invalid_comment",
                                Some("to must be a list of handles"),
                            )
                        }
                    };
                    let version = body.get("version").and_then(|v| v.as_u64());
                    match add_comment(
                        slug,
                        body.get("body").and_then(|v| v.as_str()).unwrap_or(""),
                        body.get("author").and_then(|v| v.as_str()).unwrap_or("owner"),
                        version,
                        anchor,
                        to,
                    ) {
                        Ok(value) => Response::json(201, &value),
                        Err(message) if message == "not_found" => Response::error(404, "not_found", None),
                        Err(message) => Response::error(400, "invalid_comment", Some(&message)),
                    }
                }
            },
            ("GET", "sessions") => Response::json(
                200,
                &json!({
                    "slug": slug,
                    "sessions": session_view(&current),
                    "handles": current.handles(),
                    "origin": current.origin,
                }),
            ),
            ("POST", "sessions") => match request.body_json() {
                Err(message) => Response::error(400, "invalid_request", Some(&message)),
                Ok(body) => {
                    let mut artifact = current.clone();
                    let handle = body
                        .get("as")
                        .or_else(|| body.get("handle"))
                        .and_then(|v| v.as_str())
                        .unwrap_or("");
                    let session = body.get("session").and_then(|v| v.as_str());
                    let cwd = body.get("cwd").and_then(|v| v.as_str());
                    let role = body.get("role").and_then(|v| v.as_str()).unwrap_or("attached");
                    match attach(&mut artifact, handle, session, cwd, role) {
                        Err(message) => Response::error(400, "invalid_attach", Some(&message)),
                        Ok(record) => {
                            let _ = save_artifact(&mut artifact);
                            return Response::json(
                                201,
                                &json!({
                                    "session": record,
                                    "sessions": session_view(&artifact),
                                    "url": format!("{}/a/{}", public_base(), slug),
                                }),
                            );
                        }
                    }
                }
            },
            ("POST", "inbox") => match request.body_json() {
                Err(message) => Response::error(400, "invalid_request", Some(&message)),
                Ok(body) => {
                    let handle = body
                        .get("as")
                        .or_else(|| body.get("handle"))
                        .and_then(|v| v.as_str())
                        .unwrap_or("");
                    if handle.is_empty() {
                        return Response::error(400, "missing_handle", None);
                    }
                    let query = InboxQuery {
                        handle: handle.to_string(),
                        session: body.get("session").and_then(|v| v.as_str()).map(|s| s.to_string()),
                        wait: number(&body, "wait").unwrap_or(0),
                        lease: number(&body, "lease"),
                        include_all: match body.get("all") {
                            Some(Value::Bool(flag)) => *flag,
                            Some(Value::Number(value)) => value.as_u64().unwrap_or(0) != 0,
                            Some(Value::String(text)) => {
                                matches!(text.to_lowercase().as_str(), "1" | "true")
                            }
                            _ => false,
                        },
                    };
                    match inbox(slug, query) {
                        Ok(value) => Response::json(200, &value),
                        Err(message) if message == "not_found" => Response::error(404, "not_found", None),
                        Err(message) => Response::error(400, "invalid_inbox", Some(&message)),
                    }
                }
            },
            ("DELETE", "sessions") => {
                let session_id = segments.get(4).copied().unwrap_or("");
                if session_id.is_empty() {
                    return Response::error(405, "method_not_allowed", None);
                }
                let mut artifact = current.clone();
                match detach(&mut artifact, None, Some(session_id)) {
                    Err(_) => Response::error(404, "not_found", None),
                    Ok(removed) => {
                        let _ = save_artifact(&mut artifact);
                        Response::json(
                            200,
                            &json!({ "detached": removed, "sessions": session_view(&artifact) }),
                        )
                    }
                }
            }
            ("GET", "inbox") => Response::error(
                405,
                "method_not_allowed",
                Some("claiming is a POST; a GET would let any subresource claim a comment"),
            ),
            _ => Response::error(404, "not_found", None),
        }
    } else if segments.len() == 3 && segments[0] == "a" && segments[2] == "sessions" {
        Response::error(404, "not_found", None)
    } else if segments.len() >= 4 && segments[0] == "a" && segments[2] == "sessions" {
        if method != "DELETE" {
            return Response::error(405, "method_not_allowed", None);
        }
        let slug = segments[1];
        let session_id = segments[3];
        let mut artifact = match load_artifact(slug) {
            Some(artifact) => artifact,
            None => return Response::error(404, "not_found", None),
        };
        match detach(&mut artifact, None, Some(session_id)) {
            Err(_) => Response::error(404, "not_found", None),
            Ok(removed) => {
                let _ = save_artifact(&mut artifact);
                Response::json(
                    200,
                    &json!({ "detached": removed, "sessions": session_view(&artifact) }),
                )
            }
        }
    } else if segments.len() == 2 && segments[0] == "a" {
        let slug = segments[1];
        if load_artifact(slug).is_none() {
            return Response::error(404, "not_found", None);
        }
        Response::html(200, view_html().to_string(), None)
    } else {
        Response::error(404, "not_found", None)
    }
}

// ----------------------------------------------------------- content routing

fn content_route(request: &Request) -> Response {
    if request.method != "GET" {
        return Response::error(501, "unsupported_method", None);
    }
    match (request.method.as_str(), request.path.as_str()) {
        ("GET", "/healthz") => Response::text_body(200, "text/plain; charset=utf-8", "ok".into()),
        ("GET", "/_artifacts/overlay.js") => Response::text_body(
            200,
            "application/javascript; charset=utf-8",
            overlay_js().to_string(),
        ),
        ("GET", path) => {
            let segments: Vec<&str> = path.trim_matches('/').split('/').collect();
            if segments.len() != 3 || segments[0] != "a" || segments[2] != "raw" {
                return Response::error(404, "not_found", None);
            }
            let slug = segments[1];
            let artifact = match load_artifact(slug) {
                Some(artifact) => artifact,
                None => return Response::error(404, "not_found", None),
            };
            let version = match request.query.get("v") {
                None => artifact.current_version(),
                Some(raw) => match raw.parse::<u64>() {
                    Ok(0) => artifact.current_version(),
                    Ok(value) => value,
                    Err(_) => return Response::error(400, "invalid_version", None),
                },
            };
            let html = match read_version(slug, version) {
                Some(html) => html,
                None => return Response::error(404, "not_found", None),
            };
            let body = if request.query.get("review").map(|v| v.as_str()) == Some("1") {
                inject_overlay(&html, slug, version)
            } else {
                html
            };
            Response::html(200, body, Some(artifact_csp()))
        }
        _ => Response::error(404, "not_found", None),
    }
}

// ---------------------------------------------------------------------- serve

fn serve() -> std::io::Result<()> {
    let host = store::host();
    if !matches!(host.as_str(), "127.0.0.1" | "localhost" | "::1")
        && std::env::var("AGENT_ARTIFACTS_ALLOW_REMOTE").ok().as_deref() != Some("1")
    {
        eprintln!(
            "artifactd refuses to bind {}: it is a local-only service with no\nauthentication. \
             Set AGENT_ARTIFACTS_ALLOW_REMOTE=1 only if you have put an\nauthenticated proxy in front of it.",
            host
        );
        std::process::exit(2);
    }
    std::fs::create_dir_all(state_dir())?;
    let api = TcpListener::bind((host.as_str(), port()))?;
    let content = TcpListener::bind((host.as_str(), content_port()))?;
    println!(
        "artifactd listening on http://{}:{} (artifacts on http://{}:{}, state {})",
        host,
        port(),
        host,
        content_port(),
        state_dir().to_string_lossy()
    );
    let api_handler = Arc::new(|request: &Request| api_route(request));
    let content_handler = Arc::new(|request: &Request| content_route(request));
    std::thread::spawn(move || http::serve(content, content_handler));
    http::serve(api, api_handler);
    Ok(())
}

// ------------------------------------------------------------------------ cli

fn env_first(names: &[&str]) -> Option<String> {
    names.iter().find_map(|name| {
        std::env::var(name).ok().filter(|value| !value.is_empty())
    })
}

fn guess_agent() -> String {
    if let Some(value) = env_first(&["AI_AGENT", "AGENT_NAME"]) {
        return value.to_lowercase();
    }
    if env_first(&["CLAUDECODE", "CLAUDE_CODE_ENTRYPOINT"]).is_some() {
        return "claude".into();
    }
    if env_first(&["CODEX_SANDBOX", "CODEX_HOME"]).is_some() {
        return "codex".into();
    }
    if env_first(&["OPENCODE"]).is_some() {
        return "opencode".into();
    }
    if env_first(&["PI_SESSION_ID", "PI_CODING_AGENT"]).is_some() {
        return "pi".into();
    }
    "agent".into()
}

fn guess_model() -> String {
    env_first(&[
        "AGENT_ARTIFACTS_MODEL",
        "PI_MODEL",
        "CLAUDE_MODEL",
        "ANTHROPIC_MODEL",
        "OPENAI_MODEL",
        "CODEX_MODEL",
        "GEMINI_MODEL",
    ])
    .unwrap_or_default()
}

fn clean(text: &str) -> String {
    let mut out = String::new();
    let mut dash = false;
    for c in text.to_lowercase().chars() {
        if c.is_ascii_alphanumeric() {
            out.push(c);
            dash = false;
        } else if !dash {
            out.push('-');
            dash = true;
        }
    }
    out.trim_matches('-').to_string()
}

fn guess_handle() -> String {
    if let Some(value) = env_first(&["AGENT_ARTIFACTS_AS"]) {
        return norm_handle(&value).unwrap_or_else(|_| "@agent".into());
    }
    let agent = clean(&guess_agent());
    let family = guess_model()
        .split('-')
        .next()
        .map(clean)
        .unwrap_or_default();
    let handle = if !family.is_empty() && family != agent {
        format!("{}-{}", agent, family)
    } else {
        agent
    };
    norm_handle(&handle).unwrap_or_else(|_| "@agent".into())
}

fn guess_session() -> Option<String> {
    env_first(&[
        "AGENT_ARTIFACTS_SESSION",
        "PI_SESSION_ID",
        "CLAUDE_SESSION_ID",
        "CODEX_SESSION_ID",
        "OPENCODE_SESSION_ID",
    ])
}

fn session_for(slug: &str, handle: &str, new: bool) -> String {
    if let Some(session) = guess_session() {
        if handle == guess_handle() {
            return session;
        }
    }
    if new {
        return format!("s-{}", random_hex(4));
    }
    let cache_path = state_dir().join(slug).join("cli-sessions.json");
    let mut cache: Map<String, Value> = std::fs::read_to_string(&cache_path)
        .ok()
        .and_then(|text| serde_json::from_str::<Value>(&text).ok())
        .and_then(|value| value.as_object().cloned())
        .unwrap_or_default();
    if let Some(existing) = cache.get(handle).and_then(|v| v.as_str()) {
        if !existing.is_empty() {
            return existing.to_string();
        }
    }
    let session = format!("s-{}", random_hex(4));
    cache.insert(handle.to_string(), json!(session));
    if let Some(parent) = cache_path.parent() {
        let _ = std::fs::create_dir_all(parent);
    }
    let _ = std::fs::write(&cache_path, serde_json::to_string_pretty(&cache).unwrap_or_default());
    session
}

fn base_url() -> String {
    env_first(&["AGENT_ARTIFACTS_URL"])
        .unwrap_or_else(|| format!("http://127.0.0.1:{}", store::port()))
        .trim_end_matches('/')
        .to_string()
}

fn healthy(base: &str, timeout: Duration) -> bool {
    http::request(base, "GET", "/healthz", None, timeout).is_ok_and(|r| r.status == 200)
}

fn ensure_server() {
    let base = base_url();
    if healthy(&base, Duration::from_millis(800)) {
        return;
    }
    let is_loopback = base.contains("127.0.0.1") || base.contains("localhost");
    if !is_loopback {
        eprintln!("artifact: {} is not running", base);
        std::process::exit(1);
    }
    let log_path = state_dir().join("server.log");
    if let Some(parent) = log_path.parent() {
        let _ = std::fs::create_dir_all(parent);
    }
    let log = std::fs::OpenOptions::new().create(true).append(true).open(&log_path).ok();
    let exe = std::env::current_exe().unwrap_or_else(|_| PathBuf::from("artifactd"));
    let mut command = std::process::Command::new(exe);
    command.arg("serve").stdin(std::process::Stdio::null());
    // Start on the address the caller is already pointed at, not on whatever the
    // environment happens to say.
    if let Some(authority) = base.trim_start_matches("http://").trim_end_matches('/').split('/').next() {
        let (host, port) = match authority.rsplit_once(':') {
            Some((host, port)) => (host.to_string(), port.to_string()),
            None => (authority.to_string(), store::port().to_string()),
        };
        command.env("AGENT_ARTIFACTS_HOST", host);
        command.env("AGENT_ARTIFACTS_PORT", port);
    }
    match log {
        Some(file) => {
            let clone = file.try_clone().ok();
            command.stdout(std::process::Stdio::from(file));
            if let Some(clone) = clone {
                command.stderr(std::process::Stdio::from(clone));
            }
        }
        None => {
            command.stdout(std::process::Stdio::null()).stderr(std::process::Stdio::null());
        }
    }
    if let Err(error) = command.spawn() {
        eprintln!("artifact: could not start the server ({})", error);
        std::process::exit(1);
    }
    for _ in 0..80 {
        if healthy(&base, Duration::from_millis(400)) {
            return;
        }
        std::thread::sleep(Duration::from_millis(100));
    }
    let tail = std::fs::read_to_string(&log_path).unwrap_or_default();
    eprintln!("artifact: server did not start\n{}", tail);
    std::process::exit(1);
}

fn call(method: &str, path: &str, body: Option<&str>, timeout: Duration) -> Value {
    match http::request(&base_url(), method, path, body, timeout) {
        Err(error) => {
            eprintln!("artifact: cannot reach {} ({})", base_url(), error);
            std::process::exit(1);
        }
        Ok(response) => {
            if response.status >= 400 {
                let parsed: Value = serde_json::from_str(&response.body).unwrap_or(json!({}));
                let code = parsed.get("error").and_then(|v| v.as_str()).unwrap_or("error");
                let message = parsed.get("message").and_then(|v| v.as_str()).unwrap_or("");
                eprintln!("artifact: {} {}{}", response.status, code,
                          if message.is_empty() { String::new() } else { format!(": {}", message) });
                std::process::exit(1);
            }
            serde_json::from_str(&response.body).unwrap_or(json!({}))
        }
    }
}

fn number(body: &Map<String, Value>, key: &str) -> Option<u64> {
    match body.get(key) {
        Some(Value::Number(value)) => value.as_u64(),
        Some(Value::String(text)) => text.trim().parse::<u64>().ok(),
        _ => None,
    }
}

fn str_of(value: &Value, key: &str) -> String {
    value.get(key).and_then(|v| v.as_str()).unwrap_or("").to_string()
}

fn int_of(value: &Value, key: &str) -> u64 {
    value.get(key).and_then(|v| v.as_u64()).unwrap_or(0)
}

fn delivery_label(state: &str) -> &'static str {
    match state {
        "claimed" => "delivered",
        "queued" => "active, not taken yet",
        "waiting" => "awaits that agent",
        "resolved" => "closed",
        _ => "",
    }
}

fn fmt_comment(comment: &Value, index: Option<usize>) -> String {
    let id = str_of(comment, "id");
    let head = match index {
        Some(n) => format!("{}. [{}]", n, id),
        None => format!("[{}]", id),
    };
    let mut lines = vec![format!(
        "{} {} · v{} · {} · {}",
        head,
        str_of(comment, "author"),
        int_of(comment, "version"),
        str_of(comment, "created"),
        str_of(comment, "status")
    )];
    if let Some(delivery) = comment.get("delivery").and_then(|v| v.as_array()) {
        let parts: Vec<String> = delivery
            .iter()
            .map(|item| {
                format!(
                    "{} {}",
                    str_of(item, "handle"),
                    delivery_label(str_of(item, "state").as_str())
                )
            })
            .collect();
        if !parts.is_empty() {
            lines.push(format!("   to: {}", parts.join(", ")));
        }
    }
    if let Some(anchor) = comment.get("anchor") {
        let quote = str_of(anchor, "quote");
        let selector = str_of(anchor, "selector");
        if !quote.is_empty() {
            lines.push(format!("   on: {}", quote.chars().take(160).collect::<String>()));
        }
        if !selector.is_empty() {
            lines.push(format!("   at: {}", selector.chars().take(160).collect::<String>()));
        }
    }
    lines.push(format!("   {}", str_of(comment, "body").replace('\n', "\n   ")));
    if let Some(replies) = comment.get("replies").and_then(|v| v.as_array()) {
        for reply in replies {
            lines.push(format!(
                "   ↳ {}: {}",
                str_of(reply, "author"),
                str_of(reply, "body")
            ));
        }
    }
    lines.join("\n")
}

fn usage() -> String {
    [
        "artifactd: local-only reviewable artifacts",
        "",
        "Usage: artifactd <command> [options]",
        "",
        "Commands:",
        "  serve                         run the artifact server",
        "  publish <file> [--title T] [--slug S] [--note N] [--new-session]",
        "  list | map | show <slug>",
        "  attach|detach <slug> [--new-session]",
        "  sessions <slug>",
        "  inbox <slug> [--wait S] [--all]",
        "  wait <slug> [--timeout S] [--all]",
        "  comments <slug> [--open] [--since N]",
        "  reply <comment-id> <body>",
        "  resolve|reopen <comment-id>",
        "  release <comment-id>",
        "  open <slug> [--raw] | rm <slug> | whoami",
        "",
        "Options: --as @handle, --session ID, --json (before or after the command)",
    ]
    .join("\n")
}

struct Args {
    json: bool,
    as_handle: Option<String>,
    session: Option<String>,
    new_session: bool,
    positional: Vec<String>,
    flags: BTreeMap<String, String>,
}

fn parse_args(argv: &[String]) -> Args {
    let mut args = Args {
        json: false,
        as_handle: None,
        session: None,
        new_session: false,
        positional: Vec::new(),
        flags: BTreeMap::new(),
    };
    let mut index = 0;
    while index < argv.len() {
        let arg = &argv[index];
        match arg.as_str() {
            "--json" => args.json = true,
            "--as" => {
                index += 1;
                args.as_handle = argv.get(index).cloned();
            }
            "--session" => {
                index += 1;
                args.session = argv.get(index).cloned();
            }
            "--new-session" => args.new_session = true,
            "--open" | "--all" | "--raw" => {
                args.flags.insert(arg.trim_start_matches('-').to_string(), "1".into());
            }
            value if value.starts_with("--") => {
                index += 1;
                let value = argv.get(index).cloned().unwrap_or_default();
                args.flags.insert(arg.trim_start_matches('-').to_string(), value);
            }
            value => args.positional.push(value.to_string()),
        }
        index += 1;
    }
    args
}

fn flag(args: &Args, key: &str) -> Option<String> {
    args.flags.get(key).cloned()
}

fn main() {
    let argv: Vec<String> = std::env::args().skip(1).collect();
    if argv.is_empty() || argv[0] == "--help" || argv[0] == "-h" {
        println!("{}", usage());
        return;
    }
    if argv[0] == "serve" {
        if let Err(error) = serve() {
            eprintln!("artifactd: {}", error);
            std::process::exit(1);
        }
        return;
    }
    let mut leading_json = false;
    let mut command_index = None;
    for (index, value) in argv.iter().enumerate() {
        if value == "--json" {
            leading_json = true;
            continue;
        }
        if !value.starts_with('-') {
            command_index = Some(index);
            break;
        }
    }
    let command_index = match command_index {
        Some(index) => index,
        None => {
            println!("{}", usage());
            return;
        }
    };
    let mut args = parse_args(&argv[command_index + 1..]);
    args.json = args.json || leading_json;
    let handle = match &args.as_handle {
        Some(value) => norm_handle(value).unwrap_or_else(|message| {
            eprintln!("artifact: {}", message);
            std::process::exit(1);
        }),
        None => guess_handle(),
    };
    let command = argv[command_index].as_str();
    let emit = |value: Value, human: String| {
        if args.json {
            println!("{}", serde_json::to_string_pretty(&value).unwrap_or_default());
        } else {
            println!("{}", human);
        }
    };

    match command {
        "whoami" => emit(
            json!({
                "handle": handle,
                "agent": guess_agent(),
                "model": guess_model(),
                "session": guess_session(),
                "url": base_url(),
            }),
            format!(
                "handle:  {}\nagent:   {}\nmodel:   {}\nsession: {}\nserver:  {}",
                handle,
                guess_agent(),
                if guess_model().is_empty() { "-".to_string() } else { guess_model() },
                guess_session().unwrap_or_else(|| "(generated per artifact on first use)".into()),
                base_url()
            ),
        ),
        "publish" => {
            let file = args.positional.first().cloned().unwrap_or_default();
            let html = match std::fs::read_to_string(&file) {
                Ok(text) => text,
                Err(error) => {
                    eprintln!("artifact: cannot read {} ({})", file, error);
                    std::process::exit(1);
                }
            };
            ensure_server();
            let slug = flag(&args, "slug").unwrap_or_default();
            let session = if slug.is_empty() {
                guess_session().unwrap_or_else(|| format!("s-{}", random_hex(4)))
            } else {
                session_for(&slug, &handle, args.new_session)
            };
            let mut body = json!({
                "html": html,
                "as": handle,
                "session": session,
                "cwd": std::env::current_dir().map(|p| p.to_string_lossy().to_string()).unwrap_or_default(),
            });
            if let Some(title) = flag(&args, "title") {
                body["title"] = json!(title);
            }
            if let Some(slug) = flag(&args, "slug") {
                body["slug"] = json!(slug);
            }
            if let Some(note) = flag(&args, "note") {
                body["note"] = json!(note);
            }
            let value = call("POST", "/api/artifacts", Some(&body.to_string()), Duration::from_secs(90));
            emit(
                value.clone(),
                format!(
                    "published v{} · {} · as {}\nreview: {}\nraw:    {}\nopen it, comment on elements, then run:\n  artifactd inbox {} --wait 900 --json",
                    int_of(&value, "version"),
                    str_of(&value, "slug"),
                    handle,
                    str_of(&value, "url"),
                    str_of(&value, "raw"),
                    str_of(&value, "slug")
                ),
            );
        }
        "list" | "map" => {
            ensure_server();
            let value = call("GET", "/api/artifacts", None, Duration::from_secs(30));
            let human = value
                .get("artifacts")
                .and_then(|v| v.as_array())
                .map(|items| {
                    items
                        .iter()
                        .map(|item| {
                            if command == "list" {
                                format!(
                                    "{:<30} v{:<3} {} open  {}  {}",
                                    str_of(item, "slug"),
                                    int_of(item, "currentVersion"),
                                    int_of(item, "openComments"),
                                    item.get("handles")
                                        .and_then(|v| v.as_array())
                                        .map(|h| h
                                            .iter()
                                            .filter_map(|v| v.as_str())
                                            .collect::<Vec<_>>()
                                            .join(" "))
                                        .filter(|s| !s.is_empty())
                                        .unwrap_or_else(|| "-".into()),
                                    str_of(item, "title")
                                )
                            } else {
                                let mut lines = vec![format!(
                                    "{}  v{}  {} open  {}",
                                    str_of(item, "slug"),
                                    int_of(item, "currentVersion"),
                                    int_of(item, "openComments"),
                                    str_of(item, "title")
                                )];
                                if let Some(sessions) = item.get("sessions").and_then(|v| v.as_array()) {
                                    for session in sessions {
                                        let is_origin = item
                                            .get("origin")
                                            .and_then(|o| o.get("session"))
                                            .and_then(|v| v.as_str())
                                            == Some(str_of(session, "id").as_str());
                                        lines.push(format!(
                                            "{:<18} {:<14} {:<7}{}attached {}",
                                            str_of(session, "handle"),
                                            str_of(session, "id"),
                                            if session.get("live").and_then(|v| v.as_bool()).unwrap_or(false) {
                                                "active"
                                            } else {
                                                "idle"
                                            },
                                            if is_origin { "(origin) " } else { "" },
                                            str_of(session, "attachedAt")
                                        ));
                                    }
                                }
                                lines.join("\n")
                            }
                        })
                        .collect::<Vec<_>>()
                        .join(if command == "list" { "\n" } else { "\n\n" })
                })
                .unwrap_or_else(|| "no artifacts yet".into());
            emit(value, human);
        }
        "show" => {
            ensure_server();
            let slug = args.positional.first().cloned().unwrap_or_default();
            let value = call("GET", &format!("/api/artifacts/{}", slug), None, Duration::from_secs(30));
            let versions = value
                .get("versions")
                .and_then(|v| v.as_array())
                .map(|list| {
                    list.iter()
                        .map(|v| {
                            format!(
                                "  v{}  {}  {} bytes  {}{}",
                                int_of(v, "n"),
                                str_of(v, "created"),
                                int_of(v, "bytes"),
                                str_of(v, "sha256").chars().take(12).collect::<String>(),
                                match v.get("by").and_then(|b| b.as_str()) {
                                    Some(by) => format!("  by {}", by),
                                    None => String::new(),
                                }
                            )
                        })
                        .collect::<Vec<_>>()
                        .join("\n")
                })
                .unwrap_or_default();
            let human = format!(
                "{}  ({})  v{}\nreview: {}\nsessions: {}\nversions:\n{}\ncomments: {}",
                str_of(&value, "title"),
                str_of(&value, "slug"),
                int_of(&value, "currentVersion"),
                str_of(&value, "url"),
                value
                    .get("handles")
                    .and_then(|v| v.as_array())
                    .map(|h| h.iter().filter_map(|v| v.as_str()).collect::<Vec<_>>().join(", "))
                    .filter(|s| !s.is_empty())
                    .unwrap_or_else(|| "none".into()),
                versions,
                value
                    .get("comments")
                    .and_then(|v| v.as_array())
                    .map(|c| c.len())
                    .unwrap_or(0)
            );
            emit(value, human);
        }
        "attach" | "detach" => {
            ensure_server();
            let slug = args.positional.first().cloned().unwrap_or_default();
            if command == "attach" {
                let session = args.session.clone().unwrap_or_else(|| session_for(&slug, &handle, args.new_session));
                let body = json!({
                    "as": handle,
                    "session": session,
                    "cwd": std::env::current_dir().map(|p| p.to_string_lossy().to_string()).unwrap_or_default(),
                });
                let value = call(
                    "POST",
                    &format!("/api/artifacts/{}/sessions", slug),
                    Some(&body.to_string()),
                    Duration::from_secs(30),
                );
                emit(
                    value.clone(),
                    format!(
                        "attached {} as {}\nreview: {}",
                        handle,
                        value.get("session").map(|s| str_of(s, "id")).unwrap_or_default(),
                        str_of(&value, "url")
                    ),
                );
            } else {
                let sessions = call(
                    "GET",
                    &format!("/api/artifacts/{}/sessions", slug),
                    None,
                    Duration::from_secs(30),
                );
                let mut target = args.session.clone();
                if target.is_none() {
                    if let Some(list) = sessions.get("sessions").and_then(|v| v.as_array()) {
                        let mine: Vec<&Value> = list
                            .iter()
                            .filter(|s| str_of(s, "handle") == handle)
                            .collect();
                        let live = mine
                            .iter()
                            .find(|s| s.get("live").and_then(|v| v.as_bool()).unwrap_or(false));
                        target = live.or(mine.first()).map(|s| str_of(s, "id"));
                    }
                }
                let target = match target {
                    Some(id) => id,
                    None => {
                        eprintln!("artifact: no session for {} on {}", handle, slug);
                        std::process::exit(1);
                    }
                };
                let value = call(
                    "DELETE",
                    &format!("/api/artifacts/{}/sessions/{}", slug, target),
                    None,
                    Duration::from_secs(30),
                );
                emit(value, format!("detached {}", target));
            }
        }
        "sessions" => {
            ensure_server();
            let slug = args.positional.first().cloned().unwrap_or_default();
            let value = call(
                "GET",
                &format!("/api/artifacts/{}/sessions", slug),
                None,
                Duration::from_secs(30),
            );
            let human = value
                .get("sessions")
                .and_then(|v| v.as_array())
                .map(|list| {
                    if list.is_empty() {
                        "no sessions attached".to_string()
                    } else {
                        list.iter()
                            .map(|session| {
                                format!(
                                    "{:<18} {:<14} {:<7} attached {}",
                                    str_of(session, "handle"),
                                    str_of(session, "id"),
                                    if session.get("live").and_then(|v| v.as_bool()).unwrap_or(false) {
                                        "active"
                                    } else {
                                        "idle"
                                    },
                                    str_of(session, "attachedAt")
                                )
                            })
                            .collect::<Vec<_>>()
                            .join("\n")
                    }
                })
                .unwrap_or_else(|| "no sessions attached".into());
            emit(value, human);
        }
        "inbox" | "wait" => {
            ensure_server();
            let slug = args.positional.first().cloned().unwrap_or_default();
            let session = args.session.clone().unwrap_or_else(|| session_for(&slug, &handle, false));
            let total = if command == "inbox" {
                flag(&args, "wait").and_then(|v| v.parse::<u64>().ok()).unwrap_or(0)
            } else {
                flag(&args, "timeout").and_then(|v| v.parse::<u64>().ok()).unwrap_or(900)
            };
            let include_all = args.flags.contains_key("all");
            let deadline = now_secs() + total;
            let mut last = json!({ "slug": slug, "handle": handle, "comments": [] });
            loop {
                let remaining = deadline.saturating_sub(now_secs());
                let chunk = if total == 0 { 0 } else { remaining.min(30).max(1) };
                let mut body = json!({ "as": handle, "session": session });
                if include_all {
                    body["all"] = json!(true);
                }
                if chunk > 0 {
                    body["wait"] = json!(chunk);
                }
                let value = match http::request(
                    &base_url(),
                    "POST",
                    &format!("/api/artifacts/{}/inbox", slug),
                    Some(&body.to_string()),
                    Duration::from_secs(chunk + 30),
                ) {
                    Ok(response) if response.status == 404 => {
                        json!({ "slug": slug, "handle": handle, "comments": [] })
                    }
                    Ok(response) => serde_json::from_str(&response.body).unwrap_or(json!({})),
                    Err(error) => {
                        eprintln!("artifact: cannot reach {} ({})", base_url(), error);
                        std::process::exit(1);
                    }
                };
                let comments = value.get("comments").and_then(|v| v.as_array()).cloned().unwrap_or_default();
                last = value.clone();
                if !comments.is_empty() || total == 0 || now_secs() >= deadline {
                    break;
                }
            }
            let comments = last.get("comments").and_then(|v| v.as_array()).cloned().unwrap_or_default();
            let human = if comments.is_empty() {
                format!("no new comments for {}", handle)
            } else {
                comments
                    .iter()
                    .enumerate()
                    .map(|(index, comment)| fmt_comment(comment, Some(index + 1)))
                    .collect::<Vec<_>>()
                    .join("\n\n")
            };
            emit(last, human);
        }
        "comments" => {
            ensure_server();
            let slug = args.positional.first().cloned().unwrap_or_default();
            let mut path = format!("/api/artifacts/{}/comments", slug);
            let mut query = Vec::new();
            if args.flags.contains_key("open") {
                query.push("open=1".to_string());
            }
            if let Some(since) = flag(&args, "since") {
                query.push(format!("since={}", since));
            }
            if !query.is_empty() {
                path.push('?');
                path.push_str(&query.join("&"));
            }
            let value = call("GET", &path, None, Duration::from_secs(30));
            let human = value
                .get("comments")
                .and_then(|v| v.as_array())
                .map(|list| {
                    if list.is_empty() {
                        "no comments".to_string()
                    } else {
                        list.iter()
                            .enumerate()
                            .map(|(index, comment)| fmt_comment(comment, Some(index + 1)))
                            .collect::<Vec<_>>()
                            .join("\n\n")
                    }
                })
                .unwrap_or_else(|| "no comments".into());
            emit(value, human);
        }
        "reply" => {
            ensure_server();
            let id = args.positional.first().cloned().unwrap_or_default();
            let body_text = args.positional.get(1).cloned().unwrap_or_default();
            let body = json!({ "body": body_text, "author": handle });
            let value = call(
                "POST",
                &format!("/api/comments/{}/reply", id),
                Some(&body.to_string()),
                Duration::from_secs(30),
            );
            emit(value, format!("replied to {} as {}", id, handle));
        }
        "resolve" | "reopen" => {
            ensure_server();
            let id = args.positional.first().cloned().unwrap_or_default();
            let status = if command == "resolve" { "resolved" } else { "open" };
            let body = json!({ "status": status, "author": handle });
            let value = call(
                "POST",
                &format!("/api/comments/{}/status", id),
                Some(&body.to_string()),
                Duration::from_secs(30),
            );
            emit(value, format!("{}: {}", id, status));
        }
        "release" => {
            ensure_server();
            let id = args.positional.first().cloned().unwrap_or_default();
            let body = json!({ "author": handle });
            let value = call(
                "POST",
                &format!("/api/comments/{}/release", id),
                Some(&body.to_string()),
                Duration::from_secs(30),
            );
            emit(value, format!("released {} from {}", id, handle));
        }
        "open" => {
            ensure_server();
            let slug = args.positional.first().cloned().unwrap_or_default();
            let value = call("GET", &format!("/api/artifacts/{}", slug), None, Duration::from_secs(30));
            let url = if args.flags.contains_key("raw") {
                format!("{}/raw?v={}", str_of(&value, "rawBase"), int_of(&value, "currentVersion"))
            } else {
                str_of(&value, "url")
            };
            let opener = if cfg!(target_os = "macos") { "open" } else { "xdg-open" };
            let _ = std::process::Command::new(opener).arg(&url).spawn();
            emit(json!({ "url": url.clone() }), url);
        }
        "rm" => {
            ensure_server();
            let slug = args.positional.first().cloned().unwrap_or_default();
            let value = call("DELETE", &format!("/api/artifacts/{}", slug), None, Duration::from_secs(30));
            emit(value, format!("deleted {}", slug));
        }
        other => {
            eprintln!("artifact: unknown command {}\n{}", other, usage());
            std::process::exit(2);
        }
    }
}
