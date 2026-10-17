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
