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

## Scheduling and quiet operation

New remote-sync schedules run every **five minutes**. Local capture and source exports still run every minute; the optional inbox sweep still runs every two minutes. Windows also syncs at sign-in, and Linux starts its user timer shortly after boot when the user manager is available.

On Windows, the central sync process starts its configured SSH command without opening a console window. It still captures errors and enforces the command's timeout. This applies to the command launched directly by ThreadSatchel; a separately maintained relay that launches another SSH process must suppress that process's window too.

When a source fails, only that source waits before another attempt: **10, 20, 40, then at most 60 minutes** after consecutive failures. Other sources continue on the five-minute schedule. Retry state is saved in `remote-capture/retry-state.json`, survives separate runs, and clears for a source after a successful sync. Actual retries occur at the next scheduled run after the waiting period. A manual invocation also respects a pending retry delay.

Inspect `remote-capture/status.json` for each source's `sync_state` (`succeeded`, `failed`, or `waiting_to_retry`), `consecutive_failures`, and `next_retry_at` when present. Deferred sources retain their last error in the report with `deferred: true`; the command continues to exit nonzero while any source is failed or deferred. This preserves visibility of an unavailable source while avoiding repeated connections. Queued packets remain available for a later retry.

### Update an existing one-minute sync schedule

Updating the source files takes effect on the next run, but it does not change an already installed schedule. The installer still refuses to overwrite existing tasks or timers.

For the standard Windows task, run the following as its owning Windows user. This changes only the periodic interval and retains the existing sign-in trigger, account, action, and task settings:

```powershell
$task = Get-ScheduledTask -TaskName 'ThreadSatchel-RemoteCodexSync'
$periodic = @($task.Triggers | Where-Object { $_.CimClass.CimClassName -eq 'MSFT_TaskTimeTrigger' })
if ($periodic.Count -ne 1) { throw 'Expected one periodic trigger; inspect this task manually.' }
$periodic[0].Repetition.Interval = 'PT5M'
Set-ScheduledTask -TaskName $task.TaskName -TaskPath $task.TaskPath -Trigger $task.Triggers
```

For an existing Linux user timer, run `systemctl --user edit threadsatchel-sync.timer` and add this override:

```ini
[Timer]
OnUnitInactiveSec=
OnUnitInactiveSec=300
```

Then run `systemctl --user daemon-reload` followed by `systemctl --user restart threadsatchel-sync.timer`. This changes the existing sync timer without changing source capture/export timers.

## Reliability

Each source stores its byte cursor and filtered queue locally. A checkpoint advances after a durable queue write. The central machine verifies content hashes and imports before acknowledging source packets. Lost acknowledgements cause repeat-safe retries. Original source transcripts are never edited or deleted. Only successfully acknowledged queue packets are removed.

A source can capture while the archive is unavailable; transfer catches up after reconnection. Windows tasks require the owner to be logged in. Linux timers require the user systemd manager to be running; the installer does not enable lingering or change login policy. Sleeping/powered-off machines cannot run capture. Missing/deleted source logs cannot be reconstructed.

Runs are bounded by bytes and elapsed scan time; initial history can take several cycles. A partial final JSONL line waits for the next run. Malformed JSON stops that file's cursor and is reported. Status files describe the most recent run, not lifetime totals.

## Coverage and privacy controls

Supported events are event_msg/item_completed UserMessage and AgentMessage with real recorded IDs. Unknown/background origins, tools, reasoning, unsupported assistant phases, unsupported legacy messages and multipart text are not imported. This is tied to observed Codex log formats, not a stable universal export API.

Messages that look like credentials are omitted by a heuristic; it is not a comprehensive secret detector. /memory off at the beginning of a user message pauses that session's capture until an exact /memory on message. Existing archived text is not retroactively erased. See PRIVACY.md.

## Stop or inspect

Windows Task Scheduler names: ThreadSatchel-CodexCapture, ThreadSatchel-CodexExport, ThreadSatchel-RemoteCodexSync. Disable the relevant task to pause. Unregister it to remove scheduling; retain your data/configuration unless you separately intend deletion.

Linux units: threadsatchel-local.timer, threadsatchel-export.timer, threadsatchel-sync.timer. Example: systemctl --user disable --now threadsatchel-export.timer. Remove the corresponding .timer/.service under ~/.config/systemd/user only when uninstalling, then run systemctl --user daemon-reload.

The installer refuses to replace existing tasks/units. Move/update the checkout carefully because scheduled actions use absolute paths. macOS scheduling is not supplied or tested; run manually or configure a reviewed launchd job.
