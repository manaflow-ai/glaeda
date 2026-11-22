//! End to end: a fleet store counts lookups, in total and per client, serves
//! them on `GET /stats` on its gRPC port, and keeps its windows across a
//! restart.

use std::io::{Read, Write};
use std::path::PathBuf;
use std::process::{Child, Command};
use std::time::{Duration, Instant};

pub mod kv {
    tonic::include_proto!("compilation_cache_service.keyvalue.v1");
}

const BIN: &str = env!("CARGO_BIN_EXE_fleet-cas");

struct Kill(Option<Child>);
impl Drop for Kill {
    fn drop(&mut self) {
        if let Some(c) = &mut self.0 {
            let _ = c.kill();
            let _ = c.wait();
        }
    }
}

fn wait_for(what: &str, ok: impl Fn() -> bool) {
    let t = Instant::now();
    while !ok() {
        assert!(
            t.elapsed() < Duration::from_secs(10),
            "timed out waiting for {what}"
        );
        std::thread::sleep(Duration::from_millis(50));
    }
}

fn start(port: u16, root: &PathBuf) -> Kill {
    let child = Command::new(BIN)
        .args([
            &format!("tcp:127.0.0.1:{port}"),
            root.to_str().unwrap(),
            "--writers",
            "127.0.0.2",
        ])
        .spawn()
        .unwrap();
    wait_for("store", || {
        std::net::TcpStream::connect(("127.0.0.1", port)).is_ok()
    });
    Kill(Some(child))
}

/// Stop gracefully (SIGINT), so the store saves its history on the way out.
fn stop(mut k: Kill) {
    let mut c = k.0.take().unwrap();
    let ok = Command::new("kill")
        .args(["-INT", &c.id().to_string()])
        .status()
        .unwrap()
        .success();
    assert!(ok);
    let t = Instant::now();
    while c.try_wait().unwrap().is_none() {
        assert!(t.elapsed() < Duration::from_secs(10), "store did not stop");
        std::thread::sleep(Duration::from_millis(50));
    }
}

/// `GET /stats` over plain HTTP/1.1, as curl would.
fn stats(port: u16) -> String {
    let mut s = std::net::TcpStream::connect(("127.0.0.1", port)).unwrap();
    s.set_read_timeout(Some(Duration::from_secs(5))).unwrap();
    s.write_all(b"GET /stats HTTP/1.1\r\nHost: x\r\nConnection: close\r\n\r\n")
        .unwrap();
    let mut out = String::new();
    s.read_to_string(&mut out).unwrap();
    assert!(out.starts_with("HTTP/1.1 200"), "{out}");
    assert!(
        out.to_ascii_lowercase()
            .contains("content-type: application/json"),
        "{out}"
    );
    out.split_once("\r\n\r\n").unwrap().1.to_string()
}

/// The JSON object at `"key":` (after `from`), by brace matching.
fn object<'a>(json: &'a str, key: &str) -> &'a str {
    let start = json
        .find(&format!("\"{key}\":{{"))
        .unwrap_or_else(|| panic!("{key} in {json}"))
        + key.len()
        + 3;
    let mut depth = 0;
    for (i, c) in json[start..].char_indices() {
        match c {
            '{' => depth += 1,
            '}' => {
                depth -= 1;
                if depth == 0 {
                    return &json[start..start + i + 1];
                }
            }
            _ => {}
        }
    }
    panic!("unterminated {key}");
}

#[tokio::test(flavor = "multi_thread")]
async fn store_serves_stats_and_keeps_windows_across_restart() {
    let root: PathBuf =
        std::env::temp_dir().join(format!("fleet-cas-stats-e2e-{}", std::process::id()));
    let _ = std::fs::remove_dir_all(&root);
    let port = std::net::TcpListener::bind("127.0.0.1:0")
        .unwrap()
        .local_addr()
        .unwrap()
        .port();
    let url = format!("http://127.0.0.1:{port}");

    let store = start(port, &root);
    let body = stats(port);
    assert!(body.contains("\"schema\":\"fleet-cas-stats/v1\""), "{body}");
    assert!(body.contains("\"role\":\"store\""), "{body}");
    assert!(body.contains("\"writers\":[\"127.0.0.2\"]"), "{body}");
    assert!(body.contains("\"starts\":1"), "{body}");
    assert!(object(&body, "1h").contains("\"hit_rate\":null"), "{body}");

    // gRPC still works on the same port: three index misses, one refused put
    // (127.0.0.1 is not a writer).
    let mut client = kv::key_value_db_client::KeyValueDbClient::connect(url.clone())
        .await
        .unwrap();
    for k in 0..3u8 {
        let r = client
            .get_value(kv::GetValueRequest { key: vec![k] })
            .await
            .unwrap()
            .into_inner();
        assert!(r.contents.is_none());
    }
    let put = client
        .put_value(kv::PutValueRequest {
            key: vec![9],
            value: Some(kv::Value::default()),
        })
        .await;
    assert_eq!(put.unwrap_err().code(), tonic::Code::PermissionDenied);

    let body = stats(port);
    let hour = object(&body, "1h");
    assert!(hour.contains("\"kv_misses\":3"), "{body}");
    assert!(hour.contains("\"requests\":3"), "{body}");
    assert!(hour.contains("\"kv_hit_rate\":0.0000"), "{body}");
    assert!(hour.contains("\"errors\":1"), "{body}");
    assert!(hour.contains("\"write_refused\":1"), "{body}");
    assert!(
        object(&body, "counters").contains("\"kv_get_miss\":3"),
        "{body}"
    );
    assert!(object(&body, "6h").contains("\"kv_misses\":3"), "{body}");

    // The same counts, per client (TCP peer address).
    assert!(body.contains("\"clients_since_ms\":"), "{body}");
    assert!(body.contains("\"clients_max\":256"), "{body}");
    let me = object(object(&body, "clients"), "127.0.0.1");
    assert!(me.contains("\"last_seen_ms\":"), "{body}");
    let me_hour = object(me, "1h");
    assert!(me_hour.contains("\"kv_misses\":3"), "{body}");
    assert!(me_hour.contains("\"kv_hit_rate\":0.0000"), "{body}");
    assert!(me_hour.contains("\"write_refused\":1"), "{body}");
    assert!(me_hour.contains("\"errors\":1"), "{body}");
    assert!(object(me, "24h").contains("\"kv_misses\":3"), "{body}");

    stop(store);
    assert!(root.join("stats-history.txt").exists());

    // A new process: its own counters are 0, its windows are not.
    let store = start(port, &root);
    let body = stats(port);
    assert!(body.contains("\"starts\":2"), "{body}");
    assert!(
        object(&body, "counters").contains("\"kv_get_miss\":0"),
        "{body}"
    );
    assert!(object(&body, "1h").contains("\"kv_misses\":3"), "{body}");
    assert!(object(&body, "6h").contains("\"kv_misses\":3"), "{body}");
    assert!(object(&body, "24h").contains("\"kv_misses\":3"), "{body}");
    // Per-client counts start with the process.
    assert!(body.contains("\"clients\":{}"), "{body}");
    assert!(
        object(&body, "lifetime").contains("\"kv_misses\":3"),
        "{body}"
    );
    drop(store);
    let _ = std::fs::remove_dir_all(&root);
}
