//! A small HTTP/1.1 server and client, just enough for this service.
//!
//! One thread per connection, `Connection: close` on every response, bodies
//! framed by Content-Length. Long polls hold their thread, which is exactly what
//! this workload wants: a handful of local connections.

use crate::store::{Request, Response};
use std::collections::BTreeMap;
use std::io::{BufRead, BufReader, Read, Write};
use std::net::{TcpListener, TcpStream};
use std::sync::Arc;
use std::time::Duration;

pub const MAX_HEADER_BYTES: usize = 64 * 1024;

fn percent_decode(value: &str) -> String {
    let bytes = value.as_bytes();
    let mut out = Vec::with_capacity(bytes.len());
    let mut index = 0;
    while index < bytes.len() {
        match bytes[index] {
            b'%' if index + 2 < bytes.len() => {
                let hex = &value[index + 1..index + 3];
                match u8::from_str_radix(hex, 16) {
                    Ok(byte) => {
                        out.push(byte);
                        index += 3;
                    }
                    Err(_) => {
                        out.push(b'%');
                        index += 1;
                    }
                }
            }
            b'+' => {
                out.push(b' ');
                index += 1;
            }
            byte => {
                out.push(byte);
                index += 1;
            }
        }
    }
    String::from_utf8_lossy(&out).to_string()
}

pub fn parse_query(raw: &str) -> BTreeMap<String, String> {
    let mut map = BTreeMap::new();
    for pair in raw.split('&') {
        if pair.is_empty() {
            continue;
        }
        let mut parts = pair.splitn(2, '=');
        let key = percent_decode(parts.next().unwrap_or(""));
        let value = percent_decode(parts.next().unwrap_or(""));
        map.insert(key, value);
    }
    map
}

fn read_line(reader: &mut impl BufRead) -> Option<String> {
    let mut line = String::new();
    match reader.read_line(&mut line) {
        Ok(0) | Err(_) => None,
        Ok(_) => Some(line.trim_end_matches(['\r', '\n']).to_string()),
    }
}

pub enum ReadOutcome {
    Request(Request),
    /// Answer the client and close: the request was malformed or too large.
    Reject(Response),
    /// Nothing to say, just close.
    Drop,
}

pub fn read_request(stream: &mut TcpStream) -> ReadOutcome {
    let Ok(_) = stream.set_read_timeout(Some(Duration::from_secs(600))) else {
        return ReadOutcome::Drop;
    };
    let clone = match stream.try_clone() {
        Ok(clone) => clone,
        Err(_) => return ReadOutcome::Drop,
    };
    let mut reader = BufReader::new(clone);
    let request_line = match read_line(&mut reader) {
        Some(line) => line,
        None => return ReadOutcome::Drop,
    };
    let mut parts = request_line.split_whitespace();
    let method = match parts.next() {
        Some(value) => value.to_string(),
        None => return ReadOutcome::Drop,
    };
    let target = match parts.next() {
        Some(value) => value.to_string(),
        None => {
            return ReadOutcome::Reject(Response::error(
                400,
                "invalid_request",
                Some("malformed request line"),
            ))
        }
    };

    let mut headers: BTreeMap<String, String> = BTreeMap::new();
    let mut header_bytes = 0usize;
    loop {
        let line = match read_line(&mut reader) {
            Some(line) => line,
            None => return ReadOutcome::Drop,
        };
        if line.is_empty() {
            break;
        }
        header_bytes += line.len();
        if header_bytes > MAX_HEADER_BYTES {
            return ReadOutcome::Reject(Response::error(
                431,
                "request_header_fields_too_large",
                None,
            ));
        }
        if let Some((key, value)) = line.split_once(':') {
            headers.insert(key.trim().to_lowercase(), value.trim().to_string());
        }
    }

    // A content-length is attacker controlled: never allocate from it, and never
    // block reading a body we have already decided to refuse.
    let limit = crate::store::max_body();
    let length = match headers.get("content-length") {
        None => 0usize,
        Some(raw) => match raw.parse::<usize>() {
            Ok(value) if value <= limit => value,
            Ok(_) => {
                return ReadOutcome::Reject(Response::error(
                    400,
                    "invalid_request",
                    Some("body too large"),
                ))
            }
            Err(_) => {
                return ReadOutcome::Reject(Response::error(
                    400,
                    "invalid_request",
                    Some("content-length is not a number"),
                ))
            }
        },
    };
    let mut body = Vec::new();
    if length > 0 {
        body.resize(length, 0);
        if reader.read_exact(&mut body).is_err() {
            return ReadOutcome::Drop;
        }
    }

    let (path, query) = match target.split_once('?') {
        Some((path, query)) => (path.to_string(), parse_query(query)),
        None => (target, BTreeMap::new()),
    };
    ReadOutcome::Request(Request { method, path, query, headers, body })
}

pub fn write_response(stream: &mut TcpStream, response: Response) {
    let reason = match response.status {
        200 => "OK",
        201 => "Created",
        400 => "Bad Request",
        501 => "Not Implemented",
        404 => "Not Found",
        405 => "Method Not Allowed",
        413 => "Payload Too Large",
        431 => "Request Header Fields Too Large",
        500 => "Internal Server Error",
        _ => "OK",
    };
    let mut head = format!(
        "HTTP/1.1 {} {}\r\ncontent-type: {}\r\ncontent-length: {}\r\ncache-control: no-store\r\nconnection: close\r\n",
        response.status,
        reason,
        response.content_type,
        response.body.len()
    );
    for (key, value) in &response.headers {
        head.push_str(&format!("{}: {}\r\n", key, value));
    }
    head.push_str("\r\n");
    let _ = stream.write_all(head.as_bytes());
    let _ = stream.write_all(&response.body);
    let _ = stream.flush();
}

pub fn serve<F>(listener: TcpListener, handler: Arc<F>)
where
    F: Fn(&Request) -> Response + Send + Sync + 'static,
{
    for stream in listener.incoming() {
        let Ok(mut stream) = stream else { continue };
        let handler = Arc::clone(&handler);
        std::thread::spawn(move || match read_request(&mut stream) {
            ReadOutcome::Request(request) => {
                let response = handler(&request);
                write_response(&mut stream, response);
            }
            ReadOutcome::Reject(response) => write_response(&mut stream, response),
            ReadOutcome::Drop => {}
        });
    }
}

// -------------------------------------------------------------------- client

pub struct ClientResponse {
    pub status: u16,
    pub headers: BTreeMap<String, String>,
    pub body: String,
}

pub fn request(
    base: &str,
    method: &str,
    path: &str,
    body: Option<&str>,
    timeout: Duration,
) -> std::io::Result<ClientResponse> {
    let address = base.trim_start_matches("http://").trim_end_matches('/');
    let mut stream = TcpStream::connect(address)?;
    stream.set_read_timeout(Some(timeout))?;
    stream.set_write_timeout(Some(timeout))?;
    let payload = body.unwrap_or("");
    let mut head = format!(
        "{} {} HTTP/1.1\r\nhost: {}\r\nconnection: close\r\nx-artifacts-client: 1\r\n",
        method, path, address
    );
    if body.is_some() {
        head.push_str("content-type: application/json\r\n");
    }
    head.push_str(&format!("content-length: {}\r\n\r\n", payload.len()));
    stream.write_all(head.as_bytes())?;
    stream.write_all(payload.as_bytes())?;
    stream.flush()?;

    let mut raw = Vec::new();
    stream.read_to_end(&mut raw)?;
    let text = String::from_utf8_lossy(&raw).to_string();
    let (head_text, body_text) = match text.split_once("\r\n\r\n") {
        Some((head, body)) => (head, body),
        None => (text.as_str(), ""),
    };
    let mut lines = head_text.lines();
    let status_line = lines.next().unwrap_or("");
    let status = status_line
        .split_whitespace()
        .nth(1)
        .and_then(|value| value.parse::<u16>().ok())
        .unwrap_or(0);
    let mut headers = BTreeMap::new();
    for line in lines {
        if let Some((key, value)) = line.split_once(':') {
            headers.insert(key.trim().to_lowercase(), value.trim().to_string());
        }
    }
    Ok(ClientResponse { status, headers, body: body_text.to_string() })
}
