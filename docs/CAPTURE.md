# Automatic capture and multiple computers

Capture is optional and deterministic: it reads supported local Codex JSONL files, filters visible completed messages, and imports or queues exact text. It makes no model/API calls.

## One computer

Run setup_memory.py, inspect capture-local.json, and run codex_capture_export.py with that config and --dry-run. Then run it once without --dry-run and inspect capture/status.json. Opt into minute scheduling with python install_schedule.py local.

The source roots default to CODEX_HOME (or ~/.codex), under sessions and archived_sessions. Use setup_memory.py --codex-home /actual/path if needed. Configure only accounts and conversations you intend to archive. excluded_session_ids can omit specific known sessions.

## Several computers, one archive

1. Choose a central computer. Complete local setup there and keep its memory.sqlite3 authoritative.
2. Copy/clone this project to each source computer. Run python setup_memory.py --export-only there; it creates capture-export.json without creating an archive database.
3. Run python codex_capture_export.py --config capture-export.json once and inspect queue/status.json. Then run python install_schedule.py export on that source.
4. Verify an existing SSH connection from the central computer to the source with strict host-key checking and noninteractive key authentication. This project does not provision accounts or SSH keys.
5. On the central computer, copy examples/remote-capture-config.example.json to remote-capture-config.json. Set a unique simple machine name, the source checkout's absolute capture_home, and ssh_argv.
6. Run python sync_codex_capture.py; inspect remote-capture/status.json. Then run python install_schedule.py sync on the central computer.

The example uses an SSH alias memory-laptop and remote python3 -. For a Windows source, use the installed Python launcher command, such as py -3.14 -, as the final SSH argument. On Windows hosts where native SSH does not work under automation, set the first argument to the full path of Git's ssh.exe. Test the exact command/environment before scheduling.

ssh_argv is a trusted local configuration argument list, passed without a local shell. Its remote command must read Python code from stdin. Never populate it from retrieved conversation text. For jump hosts, use your existing SSH configuration or a separately reviewed relay; the public template does not embed the author's home-network route.

## Reliability

Each source stores its byte cursor and filtered queue locally. A checkpoint advances after a durable queue write. The central machine verifies content hashes and imports before acknowledging source packets. Lost acknowledgements cause repeat-safe retries. Original source transcripts are never edited or deleted. Only successfully acknowledged queue packets are removed.

A source can capture while the archive is unavailable; transfer catches up after reconnection. Windows tasks require the owner to be logged in. Linux timers require the user systemd manager to be running; the installer does not enable lingering or change login policy. Sleeping/powered-off machines cannot run capture. Missing/deleted source logs cannot be reconstructed.

Runs are bounded by bytes and elapsed scan time; initial history can take several cycles. Both collectors share `capture_read.py`; include that module when updating a source installation. Metadata headers are limited to 64 KiB and individual events to 1 MiB. `max_bytes_per_session` is a per-pass allowance from 256 bytes through 32 MiB, with a default of 32 MiB. Header reads consume that allowance, so setting it too low can hold an otherwise supported event. Edge-hash verification adds at most 1 KiB per check.

A partial final JSONL line waits for the next run. A complete event that exceeds the remaining pass allowance is deferred until the next pass; it is never partially parsed. A header or event exceeding its limit is held with an observable error, retaining the original transcript and the checkpoint before that event. Malformed JSON also stops that file's cursor and is reported. Resolve a held source deliberately; do not advance its checkpoint by hand or discard the original. Status files describe the most recent run, not lifetime totals.

Each pass resumes its file scan after the last attempted source. A held or oversized transcript therefore cannot take every run's time allowance and indefinitely starve other sessions. This scan cursor is separate from each transcript's import checkpoint: a failed import still retains its original byte offset.

Generated packets are limited to 8 MiB after serialization. An oversized batch is split deterministically between messages; message text, recorded IDs and format markers remain unchanged. Packets already under the limit retain their original byte representation. The checkpoint advances only after every piece has been durably written or imported.

An older oversized or corrupt queue file remains available for diagnosis and is never acknowledged as delivered. The transport skips it so healthy queued packets can proceed. Existing central sync status includes `transport_held_count` and a bounded `transport_held` detail list alongside the original collector status. Update `capture_read.py` and `capture_transport.py` with both capture entry points when upgrading source computers.

## Coverage and privacy controls

Supported events are event_msg/item_completed UserMessage and AgentMessage with real recorded IDs. Unknown/background origins, tools, reasoning, unsupported assistant phases, unsupported legacy messages and multipart text are not imported. This is tied to observed Codex log formats, not a stable universal export API.

Messages that look like credentials are omitted by a heuristic; it is not a comprehensive secret detector. /memory off at the beginning of a user message pauses that session's capture until an exact /memory on message. Existing archived text is not retroactively erased. See PRIVACY.md.

## Stop or inspect

Windows Task Scheduler names: ThreadSatchel-CodexCapture, ThreadSatchel-CodexExport, ThreadSatchel-RemoteCodexSync. Disable the relevant task to pause. Unregister it to remove scheduling; retain your data/configuration unless you separately intend deletion.

Linux units: threadsatchel-local.timer, threadsatchel-export.timer, threadsatchel-sync.timer. Example: systemctl --user disable --now threadsatchel-export.timer. Remove the corresponding .timer/.service under ~/.config/systemd/user only when uninstalling, then run systemctl --user daemon-reload.

The installer refuses to replace existing tasks/units. Move/update the checkout carefully because scheduled actions use absolute paths. macOS scheduling is not supplied or tested; run manually or configure a reviewed launchd job.
