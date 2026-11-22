//! Request counters over time windows, kept across restarts, and the JSON
//! that `GET /stats` serves.
//!
//! Why: the fleet store was down ~21 h (2026-11-20/21) and
//! nothing said so. Its counters lived only in the process (`stats.json`,
//! reset at every start) and nothing on the network could read them, so a
//! dead store looked like ordinary compiles on the nodes. Now the store keeps
//! per-minute buckets of hits, misses, puts and errors for 24 h in
//! `stats-history.txt` beside its objects, sums them into 1 h and 24 h
//! windows, and serves them on its own port. A scraper reads `/stats`: no
//! answer is a down store, a low 1 h hit rate with enough requests is a cold
//! or broken one.
//!
//! The process counters (`Stats`) stay the source: every sample turns their
//! growth since the last sample into the current minute's bucket, so the hot
//! paths change nothing.

use std::collections::{HashMap, VecDeque};
use std::net::IpAddr;
use std::path::{Path, PathBuf};
use std::sync::Mutex;
use std::sync::atomic::{AtomicU64, Ordering::Relaxed};

/// The windowed counters, in bucket order.
pub const KEYS: [&str; 9] = [
    "kv_hits",
    "kv_misses",
    "cas_hits",
    "cas_misses",
    "puts",
    "errors",
    "write_refused",
    "bytes_in",
    "bytes_out",
];
pub const N: usize = KEYS.len();
pub const KV_HITS: usize = 0;
pub const KV_MISSES: usize = 1;
pub const CAS_HITS: usize = 2;
pub const CAS_MISSES: usize = 3;
pub const PUTS: usize = 4;
pub const ERRORS: usize = 5;
pub const WRITE_REFUSED: usize = 6;
pub const BYTES_IN: usize = 7;
pub const BYTES_OUT: usize = 8;

/// 24 h of minute buckets.
pub const KEEP_MIN: u64 = 24 * 60;
pub const HISTORY_FILE: &str = "stats-history.txt";
const HEADER: &str = "fleet-cas-history v1";

pub type Counts = [u64; N];

#[derive(Debug)]
pub struct History {
    /// (unix minute, counts in that minute), oldest first, within KEEP_MIN.
    buckets: VecDeque<(u64, Counts)>,
    /// The process counters at the last sample.
    last: Counts,
    /// Every count since the history file was first written.
    pub lifetime: Counts,
    /// When the history file was first written (unix ms), and how many
    /// processes have started on it.
    pub first_started_ms: u64,
    pub starts: u64,
    path: Option<PathBuf>,
}

fn add(a: &mut Counts, b: &Counts) {
    for (x, y) in a.iter_mut().zip(b) {
        *x = x.saturating_add(*y);
    }
}

impl History {
    /// A history with nothing in it, kept in memory only.
    pub fn new(now_ms: u64) -> Self {
        History {
            buckets: VecDeque::new(),
            last: [0; N],
            lifetime: [0; N],
            first_started_ms: now_ms,
            starts: 1,
            path: None,
        }
    }

    /// Load `dir/stats-history.txt` for a process that just started (its
    /// counters are all 0). A missing or unreadable file starts a new
    /// history; buckets older than KEEP_MIN are dropped.
    pub fn load(dir: &Path, now_ms: u64) -> Self {
        let path = dir.join(HISTORY_FILE);
        let mut h = match std::fs::read_to_string(&path)
            .ok()
            .and_then(|text| Self::parse(&text))
        {
            // One more start on a kept history.
            Some(h) => History {
                starts: h.starts + 1,
                ..h
            },
            None => Self::new(now_ms),
        };
        h.path = Some(path);
        h.prune(now_ms / 60_000);
        h
    }

    fn parse(text: &str) -> Option<Self> {
        let mut lines = text.lines();
        if lines.next()? != HEADER {
            return None;
        }
        let mut h = Self::new(0);
        h.starts = 0;
        // Column i of the file is counter cols[i] here (None: a key this
        // build does not keep).
        let mut cols: Vec<Option<usize>> = Vec::new();
        let row = |cols: &[Option<usize>], fields: std::str::SplitWhitespace| -> Option<Counts> {
            let mut c = [0; N];
            for (i, f) in fields.enumerate() {
                let v: u64 = f.parse().ok()?;
                if let Some(Some(k)) = cols.get(i) {
                    c[*k] = v;
                }
            }
            Some(c)
        };
        for line in lines {
            let mut f = line.split_whitespace();
            match f.next() {
                Some("started") => {
                    h.first_started_ms = f.next()?.parse().ok()?;
                    h.starts = f.next()?.parse().ok()?;
                }
                Some("keys") => cols = f.map(|k| KEYS.iter().position(|x| *x == k)).collect(),
                Some("lifetime") => h.lifetime = row(&cols, f)?,
                Some("m") => {
                    let minute: u64 = f.next()?.parse().ok()?;
                    let c = row(&cols, f)?;
                    // Keep the order the file was written in; ignore a row
                    // that goes back in time.
                    if h.buckets.back().is_none_or(|(m, _)| *m < minute) {
                        h.buckets.push_back((minute, c));
                    }
                }
                _ => {}
            }
        }
        Some(h)
    }

    pub fn render(&self) -> String {
        let mut out = format!(
            "{HEADER}\nstarted {} {}\nkeys {}\n",
            self.first_started_ms,
            self.starts,
            KEYS.join(" ")
        );
        let nums = |c: &Counts| c.iter().map(u64::to_string).collect::<Vec<_>>().join(" ");
        out.push_str(&format!("lifetime {}\n", nums(&self.lifetime)));
        for (m, c) in &self.buckets {
            out.push_str(&format!("m {m} {}\n", nums(c)));
        }
        out
    }

    /// Write the history file atomically (no-op for an in-memory history).
    pub fn save(&self) -> std::io::Result<()> {
        let Some(path) = &self.path else {
            return Ok(());
        };
        let tmp = path.with_extension(format!("tmp{}", std::process::id()));
        std::fs::write(&tmp, self.render())?;
        std::fs::rename(&tmp, path)
    }

    fn prune(&mut self, now_min: u64) {
        while self
            .buckets
            .front()
            .is_some_and(|(m, _)| m + KEEP_MIN <= now_min)
        {
            self.buckets.pop_front();
        }
    }

    /// Fold the process counters' growth since the last sample into the
    /// current minute.
    pub fn sample(&mut self, current: &Counts, now_ms: u64) {
        let mut delta = [0; N];
        for i in 0..N {
            delta[i] = current[i].saturating_sub(self.last[i]);
        }
        self.last = *current;
        let minute = now_ms / 60_000;
        self.prune(minute);
        if delta.iter().all(|d| *d == 0) {
            return;
        }
        add(&mut self.lifetime, &delta);
        match self.buckets.back_mut() {
            Some((m, c)) if *m >= minute => add(c, &delta),
            _ => self.buckets.push_back((minute, delta)),
        }
    }

    /// Counts in the last `minutes` minutes (the current minute included),
    /// with the growth not sampled yet.
    pub fn window(&self, current: &Counts, now_ms: u64, minutes: u64) -> Counts {
        let now_min = now_ms / 60_000;
        let mut out = [0; N];
        for (m, c) in self.buckets.iter().rev() {
            if m + minutes <= now_min {
                break;
            }
            add(&mut out, c);
        }
        for i in 0..N {
            out[i] = out[i].saturating_add(current[i].saturating_sub(self.last[i]));
        }
        out
    }

    /// Like `window`, for the lifetime counts.
    pub fn lifetime_with(&self, current: &Counts) -> Counts {
        let mut out = self.lifetime;
        for i in 0..N {
            out[i] = out[i].saturating_add(current[i].saturating_sub(self.last[i]));
        }
        out
    }
}

fn rate(hits: u64, misses: u64) -> String {
    match hits + misses {
        0 => "null".into(),
        n => format!("{:.4}", hits as f64 / n as f64),
    }
}

/// One window as a JSON object: the counts, `requests` (lookups: index and
/// object gets), `hit_rate` over all lookups and `kv_hit_rate` over index
/// lookups (a compile replayed or not). Rates are null with no lookups.
pub fn counts_json(c: &Counts) -> String {
    let mut fields: Vec<String> = KEYS
        .iter()
        .zip(c)
        .map(|(k, v)| format!("\"{k}\":{v}"))
        .collect();
    let hits = c[KV_HITS] + c[CAS_HITS];
    let misses = c[KV_MISSES] + c[CAS_MISSES];
    fields.push(format!("\"requests\":{}", hits + misses));
    fields.push(format!("\"hits\":{hits}"));
    fields.push(format!("\"misses\":{misses}"));
    fields.push(format!("\"hit_rate\":{}", rate(hits, misses)));
    fields.push(format!(
        "\"kv_hit_rate\":{}",
        rate(c[KV_HITS], c[KV_MISSES])
    ));
    format!("{{{}}}", fields.join(","))
}

/// The windows `/stats` reports, as (name, minutes).
pub const WINDOWS: [(&str, u64); 4] = [("5m", 5), ("1h", 60), ("6h", 6 * 60), ("24h", KEEP_MIN)];

pub fn windows_json(h: &History, current: &Counts, now_ms: u64) -> String {
    let body: Vec<String> = WINDOWS
        .iter()
        .map(|(name, minutes)| {
            format!(
                "\"{name}\":{}",
                counts_json(&h.window(current, now_ms, *minutes))
            )
        })
        .collect();
    format!("{{{}}}", body.join(","))
}

/// The most clients `Clients` keeps; a new one past it drops the least
/// recently seen.
pub const MAX_CLIENTS: usize = 256;

/// The same counters per TCP peer address, kept in memory only (they start
/// with the process). Why: the fleet store's 1 h kv hit rate was 2% while
/// its writer put 20k entries an hour, and nothing said which node was
/// missing. Requests without a peer address (a unix socket) count under
/// `None`, served as "unknown".
///
/// A request costs one short mutex hold and a hash lookup; the windows are
/// folded from each client's counters by the same 500 ms sampler as the
/// store's own.
#[derive(Debug)]
pub struct Clients {
    map: Mutex<HashMap<Option<IpAddr>, Client>>,
    max: usize,
    /// Clients dropped to stay within `max`.
    dropped: AtomicU64,
}

#[derive(Debug)]
struct Client {
    counts: Counts,
    history: History,
    last_seen_ms: u64,
}

impl Clients {
    pub fn new(max: usize) -> Self {
        Clients {
            map: Mutex::new(HashMap::new()),
            max: max.max(1),
            dropped: AtomicU64::new(0),
        }
    }

    fn map(&self) -> std::sync::MutexGuard<'_, HashMap<Option<IpAddr>, Client>> {
        self.map.lock().unwrap_or_else(|e| e.into_inner())
    }

    /// Add each `(counter, n)` (a `KEYS` index) to `peer`'s counts, now.
    pub fn add(&self, peer: Option<IpAddr>, adds: &[(usize, u64)], now_ms: u64) {
        // One address per host: an IPv4 peer on a dual-stack socket arrives
        // as ::ffff:a.b.c.d.
        let peer = peer.map(|ip| ip.to_canonical());
        let mut map = self.map();
        if !map.contains_key(&peer) && map.len() >= self.max {
            let oldest = map
                .iter()
                .min_by_key(|(_, c)| c.last_seen_ms)
                .map(|(k, _)| *k);
            if let Some(k) = oldest {
                map.remove(&k);
                self.dropped.fetch_add(1, Relaxed);
            }
        }
        let c = map.entry(peer).or_insert_with(|| Client {
            counts: [0; N],
            history: History::new(now_ms),
            last_seen_ms: now_ms,
        });
        for (k, n) in adds {
            c.counts[*k] = c.counts[*k].saturating_add(*n);
        }
        c.last_seen_ms = c.last_seen_ms.max(now_ms);
    }

    /// Fold every client's growth since the last sample into its minute.
    pub fn sample(&self, now_ms: u64) {
        for c in self.map().values_mut() {
            let current = c.counts;
            c.history.sample(&current, now_ms);
        }
    }

    pub fn dropped(&self) -> u64 {
        self.dropped.load(Relaxed)
    }

    /// `{"IP":{"last_seen_ms":..,"5m":{..},"1h":{..},"6h":{..},"24h":{..},
    /// "lifetime":{..}},...}`, sorted by address, "unknown" last.
    pub fn json(&self, now_ms: u64) -> String {
        let map = self.map();
        let mut rows: Vec<(String, &Client)> = map
            .iter()
            .map(|(k, c)| (k.map_or("unknown".into(), |ip| ip.to_string()), c))
            .collect();
        rows.sort_by(|(a, _), (b, _)| (a == "unknown", a).cmp(&(b == "unknown", b)));
        let body: Vec<String> = rows
            .iter()
            .map(|(k, c)| {
                let mut fields = vec![format!("\"last_seen_ms\":{}", c.last_seen_ms)];
                for (name, minutes) in WINDOWS {
                    fields.push(format!(
                        "\"{name}\":{}",
                        counts_json(&c.history.window(&c.counts, now_ms, minutes))
                    ));
                }
                fields.push(format!(
                    "\"lifetime\":{}",
                    counts_json(&c.history.lifetime_with(&c.counts))
                ));
                format!("{}:{{{}}}", quote(k), fields.join(","))
            })
            .collect();
        format!("{{{}}}", body.join(","))
    }
}

/// A JSON string literal.
pub fn quote(s: &str) -> String {
    let mut out = String::from("\"");
    for ch in s.chars() {
        match ch {
            '"' => out.push_str("\\\""),
            '\\' => out.push_str("\\\\"),
            c if (c as u32) < 0x20 => out.push_str(&format!("\\u{:04x}", c as u32)),
            c => out.push(c),
        }
    }
    out.push('"');
    out
}

#[cfg(test)]
mod tests {
    use super::*;

    const MIN: u64 = 60_000;

    fn counts(kv_hits: u64, kv_misses: u64, puts: u64, errors: u64) -> Counts {
        let mut c = [0; N];
        c[0] = kv_hits;
        c[1] = kv_misses;
        c[4] = puts;
        c[5] = errors;
        c
    }

    #[test]
    fn samples_fold_into_minutes_and_windows_sum_them() {
        let t0 = 1_000 * MIN;
        let mut h = History::new(t0);
        h.sample(&counts(3, 1, 0, 0), t0);
        h.sample(&counts(5, 1, 2, 0), t0 + 10_000); // same minute
        h.sample(&counts(5, 4, 2, 1), t0 + 2 * MIN);
        assert_eq!(h.buckets.len(), 2);
        let now = t0 + 2 * MIN + 1;
        assert_eq!(h.window(&counts(5, 4, 2, 1), now, 1), counts(0, 3, 0, 1));
        assert_eq!(h.window(&counts(5, 4, 2, 1), now, 60), counts(5, 4, 2, 1));
        // Not sampled yet still counts.
        assert_eq!(h.window(&counts(6, 4, 2, 1), now, 60), counts(6, 4, 2, 1));
        // An hour later the first minutes are out of the 1 h window, in the 24 h one.
        let later = t0 + 61 * MIN;
        assert_eq!(h.window(&counts(5, 4, 2, 1), later, 60), counts(0, 3, 0, 1));
        assert_eq!(
            h.window(&counts(5, 4, 2, 1), later, KEEP_MIN),
            counts(5, 4, 2, 1)
        );
        // Past 24 h the buckets are dropped at the next sample; lifetime stays.
        h.sample(&counts(5, 4, 2, 1), t0 + (KEEP_MIN + 3) * MIN);
        assert!(h.buckets.is_empty());
        assert_eq!(h.lifetime, counts(5, 4, 2, 1));
    }

    #[test]
    fn history_survives_a_restart() {
        let dir = std::env::temp_dir().join(format!("fleet-cas-window-{}", std::process::id()));
        let _ = std::fs::remove_dir_all(&dir);
        std::fs::create_dir_all(&dir).unwrap();
        let t0 = 2_000 * MIN;
        let mut h = History::load(&dir, t0);
        assert_eq!(h.starts, 1);
        h.sample(&counts(7, 3, 1, 0), t0 + 5_000);
        h.save().unwrap();

        // The new process's counters start at 0 again.
        let t1 = t0 + 10 * MIN;
        let mut h2 = History::load(&dir, t1);
        assert_eq!(h2.starts, 2);
        assert_eq!(h2.first_started_ms, t0);
        assert_eq!(h2.lifetime, counts(7, 3, 1, 0));
        assert_eq!(h2.window(&[0; N], t1, 60), counts(7, 3, 1, 0));
        h2.sample(&counts(1, 0, 0, 2), t1);
        assert_eq!(h2.window(&counts(1, 0, 0, 2), t1, 60), counts(8, 3, 1, 2));
        assert_eq!(h2.lifetime, counts(8, 3, 1, 2));
        let _ = std::fs::remove_dir_all(&dir);
    }

    #[test]
    fn a_damaged_or_foreign_history_starts_over() {
        assert!(History::parse("something else\n").is_none());
        assert!(History::parse(&format!("{HEADER}\nkeys kv_hits\nm x 1\n")).is_none());
        // Unknown keys are skipped, known ones land by name.
        let h = History::parse(&format!(
            "{HEADER}\nstarted 5 2\nkeys future kv_misses\nlifetime 9 4\nm 10 1 2\n"
        ))
        .unwrap();
        assert_eq!(h.lifetime[KV_MISSES], 4);
        assert_eq!(h.buckets[0].1[KV_MISSES], 2);
        assert_eq!(h.starts, 2);
    }

    #[test]
    fn six_hour_window_reads_kept_buckets_across_a_restart() {
        let dir = std::env::temp_dir().join(format!("fleet-cas-window6h-{}", std::process::id()));
        let _ = std::fs::remove_dir_all(&dir);
        std::fs::create_dir_all(&dir).unwrap();
        let t0 = 3_000 * MIN;
        let mut h = History::load(&dir, t0);
        h.sample(&counts(4, 6, 0, 0), t0);
        h.save().unwrap();
        // 3 h later, in a new process: out of the 1 h window, in the 6 h one.
        let t1 = t0 + 180 * MIN;
        let h2 = History::load(&dir, t1);
        let j = windows_json(&h2, &[0; N], t1);
        let six = &j[j.find("\"6h\":").unwrap()..j.find("\"24h\":").unwrap()];
        assert!(six.contains("\"kv_hits\":4"), "{j}");
        assert!(six.contains("\"kv_hit_rate\":0.4000"), "{j}");
        let hour = &j[j.find("\"1h\":").unwrap()..j.find("\"6h\":").unwrap()];
        assert!(hour.contains("\"requests\":0"), "{j}");
        // 7 h later the 6 h window is empty, the 24 h one is not.
        let t2 = t0 + 7 * 60 * MIN;
        assert_eq!(h2.window(&[0; N], t2, 6 * 60), [0; N]);
        assert_eq!(h2.window(&[0; N], t2, KEEP_MIN), counts(4, 6, 0, 0));
        let _ = std::fs::remove_dir_all(&dir);
    }

    #[test]
    fn clients_count_per_peer_window_and_drop_the_least_recent() {
        let a: IpAddr = "192.0.2.10".parse().unwrap();
        let b: IpAddr = "192.0.2.20".parse().unwrap();
        let mapped: IpAddr = "::ffff:192.0.2.10".parse().unwrap();
        let t0 = 4_000 * MIN;
        let cl = Clients::new(2);
        cl.add(Some(a), &[(KV_HITS, 1)], t0);
        cl.add(Some(mapped), &[(KV_MISSES, 3)], t0 + 1);
        cl.add(Some(b), &[(PUTS, 5), (BYTES_IN, 100)], t0 + 2);
        cl.sample(t0 + 3);
        let j = cl.json(t0 + 4);
        let ja = &j[j.find("\"192.0.2.10\"").unwrap()..j.find("\"192.0.2.20\"").unwrap()];
        assert!(ja.contains("\"last_seen_ms\":"), "{j}");
        assert!(ja.contains("\"kv_hits\":1,\"kv_misses\":3"), "{j}");
        assert!(ja.contains("\"kv_hit_rate\":0.2500"), "{j}");
        for w in ["\"5m\":", "\"1h\":", "\"6h\":", "\"24h\":", "\"lifetime\":"] {
            assert!(ja.contains(w), "{w} in {j}");
        }
        assert!(j.contains("\"puts\":5"), "{j}");
        // An hour on, the counts leave the 1 h window but stay in 6 h.
        let later = t0 + 90 * MIN;
        let j = cl.json(later);
        let ja = &j[j.find("\"192.0.2.10\"").unwrap()..j.find("\"192.0.2.20\"").unwrap()];
        let hour = &ja[ja.find("\"1h\":").unwrap()..ja.find("\"6h\":").unwrap()];
        assert!(hour.contains("\"requests\":0"), "{j}");
        let six = &ja[ja.find("\"6h\":").unwrap()..ja.find("\"24h\":").unwrap()];
        assert!(six.contains("\"requests\":4"), "{j}");
        // A third client drops the least recently seen (a, seen at t0+1).
        cl.add(None, &[(CAS_MISSES, 1)], later);
        let j = cl.json(later);
        assert!(!j.contains("192.0.2.10"), "{j}");
        assert!(j.contains("\"192.0.2.20\""), "{j}");
        assert!(j.contains("\"unknown\":{"), "{j}");
        assert_eq!(cl.dropped(), 1);
    }

    #[test]
    fn window_json_has_rates_and_null_without_lookups() {
        let j = counts_json(&counts(3, 1, 0, 0));
        assert!(j.contains("\"requests\":4"), "{j}");
        assert!(j.contains("\"kv_hit_rate\":0.7500"), "{j}");
        assert!(j.contains("\"hit_rate\":0.7500"), "{j}");
        let j = counts_json(&[0; N]);
        assert!(j.contains("\"hit_rate\":null"), "{j}");
        assert_eq!(quote("a\"b\\\n"), "\"a\\\"b\\\\\\u000a\"");
    }
}
