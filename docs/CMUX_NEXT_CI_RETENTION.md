# cmux-next CI retention

`glaeda-disk` recognizes only these cmux-next state roots under
`~/Library/Caches/cmux-next-ci`:

- `cmux-scheme/<runner>`;
- `dev-daily-release/<hex fingerprint>/Release/<hex Xcode hash>`.

The retention family is an allowlist. It can reclaim generated children named
`Build`, `Index.noindex`, `Logs`, `Products`, `Intermediates.noindex`,
`DerivedSources`, or `XCBuildData` after 72 hours of observed inactivity. It
keeps `SourcePackages`, `ModuleCache.noindex`, `SDKStatCaches.noindex`,
`SDKExplicitPrecompiledModules`, `info.plist`, metadata markers, unknown
children, and every directory outside the two validated layouts.

Both report and apply paths fail closed when a validated state root is named by
a process, holds a socket, has a `.cmux-active`, `.build.active`, `.lock`,
`lease.json`, or `reservation.json` marker, or contains an unknown direct
child. Apply re-collects process evidence immediately before each rename. The
family never deletes a whole runner root or source checkout. Use the normal
`glaeda-disk` report first; `--apply` writes the standard JSONL receipt and
rechecks every candidate.

The ordinary `--dedupe` pass visits only reusable package/module/SDK inputs. Build
output is write-heavy, so it is excluded from the periodic WatchPaths agent. An
operator may opt into the narrower `--dedupe --dedupe-builds` pass after the roots
are inactive. It scans each validated top-level `Products` directory, plus the sealed
`Build/Products` leaf used by current Xcode state roots; the rest of the write-heavy
`Build` tree remains excluded. It waits at least 15 minutes after the last file mtime
and re-collects process evidence before every APFS clone. A process, socket, lease/lock
marker, unknown state child, or unavailable `lsof`/`ps` evidence refuses that clone.
APFS copy-on-write keeps later compiler writes private to the path that performs them;
the command never hardlinks build outputs.
