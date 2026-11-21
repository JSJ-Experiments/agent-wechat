# Native migration acceptance for legacy recorder users

This directory is deliberately **read-only** against the native daemon and the old recorder. The canary writes its own database, not the existing archive. It is not a drop-in replacement for either recorder. Do not repoint the live forwarder or run the old and native clients against the same profile.

On an isolated daemon with a representative supported account:

```sh
python3 native_shadow.py --server http://127.0.0.1:16174 --token-file /private/token --account ACCOUNT \
  compare THREAD --legacy /private/path/to/legacy-recorder.sqlite --limit 50

python3 native_shadow.py --server http://127.0.0.1:16174 --token-file /private/token --account ACCOUNT \
  download NATIVE_MESSAGE_ID ./new-file.jpg --variant original

python3 native_shadow.py --server http://127.0.0.1:16174 --token-file /private/token --account ACCOUNT \
  skipped
```

The comparison analyzes string server IDs, numeric types and subtypes, text, and visible unknown records from a bounded native page without printing private contents. The full native JSON is kept by the normalization function, not silently coerced into a type the client understands. The separate `skipped` check accounts for events that didn't become normal messages, counting their command and message type where available. Run it again with `--after NEXT_CURSOR` to verify a subsequent interval; a stale cursor is an explicit failure. Zero skipped controls in one sample doesn't establish that none were lost. It does **not** make upstream's sealed raw receipts readable or prove raw receipt parity with the legacy recorder.

The download command reads only already-cached verified bytes. It checks the actual variant, length, and SHA-256 before publishing a new file, and never overwrites an existing file or substitutes a thumbnail. Acquisition of pending media, automatic forwarder continuity, and live profile eligibility must pass separate tests before retiring legacy services.

To test native recorder continuity without Discord or the desktop, run the following against an isolated native account before sending a new sample to that account. Run it again after the sample arrives, and after restarting the canary process:

```sh
python3 native_record_batch.py --server http://127.0.0.1:16174 --token-file /private/token \
  --account ACCOUNT --archive /private/native-canary.sqlite
```

The first run records the current event cursor without replaying history. Later runs read committed messages through the native API, keep their full projected JSON and numeric type information, and record skipped-control labels separately. The SQLite cursor advances only once that page is saved. A delayed message read or expired event cursor is an error, not an instruction to skip to the newest event. Use a fresh canary database for each source. This test path does not yet deliver to Discord, automatically save attachments, or mirror message recalls; use the separate migration checks before switching production.

The fork's ARM64 Blacksmith workflow builds the native daemon and local upstream simulator, exercises CLI and profile projection, verifies an original photo and an ordinary incoming file against known synthetic bytes, and runs the unsupported-message regression. It keeps Cargo registry and target directories on separate persistent Blacksmith disks keyed to the native test environment. It does not use live profile material.

A production cutover remains contingent on representative same-history comparisons, real new inbound and unknown events, original-quality images/files/voice/video, exact cursor behaviour through restart, and a rollback plan with an untouched legacy archive. Official-account discovery is a separate current gap: the native API exposes link cards, not an Official Accounts feed or authenticated article viewer.
