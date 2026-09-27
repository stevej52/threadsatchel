# Automatic inbox imports

The optional sweep imports completed files directly inside the archive's
`inbox/` directory every two minutes. It is a short scheduled command, not an
always-running watcher, AI agent, or network service. It calls the existing
`import_memory.import_file` function; all storage, identity and reconciliation
remain in that importer.

## Enable

Initialize the archive first using the normal setup instructions, then run:

```sh
python inbox_sweep.py
python install_schedule.py inbox
```

Windows installs `ThreadSatchel-InboxSweep` as the current user with limited
permissions, a sign-in trigger, a two-minute trigger, overlapping runs ignored,
and a four-minute execution limit. The account must remain signed in; locking
the desktop is fine. It does not install before-sign-in operation, enable
automatic Windows login, or require Codex to remain open. Existing capture and
remote-access tasks are unchanged.

Linux uses `threadsatchel-inbox.service` and `.timer` in the current user's
systemd configuration. That scheduler follows the existing capture installer;
the new inbox mode has been exercised on Windows. Other operating systems can
schedule `python inbox_sweep.py` using their own scheduler.

## Delivery contract

1. Write the complete file under `inbox/` using a temporary name such as
   `.incoming-<unique-id>.json.part`.
2. Finish all chunks and validate the complete file.
3. Rename it in the same directory to a new supported filename such as
   `claude-<unique-id>.json`. Do not overwrite an existing completed deposit.
4. The sweep waits until the file's modification time is at least 30 seconds
   old, then imports it on an eligible pass. Allow about two to three minutes
   under normal load; held files and a backlog can take longer.

Use the existing JSON packet format. The public importer accepts
`threadsatchel/1` and its `steve-memory/1` compatibility alias. Local installations
that still use only `steve-memory/1` can use this same sweep unchanged. TXT and
Markdown become notes unless already imported with explicit metadata. Use JSON
when you need summary/excerpt labels and source metadata.

Only `.json`, `.txt`, `.md`, and `.markdown` are automatic inputs, up to 8 MiB
each. Subdirectories, links/reparse points, executables, temporary names, and
ZIP files are ignored. Large files and ZIP archives remain manual imports.
Do not place credentials in memory files. The existing capture secret-pattern
filter holds likely credentials for review; it is a heuristic, not a guarantee
that every kind of secret will be detected.

The Windows reader denies other writes/renames/deletes while it verifies and
imports a file; files already open for writing are deferred. POSIX read locks
are advisory. Atomic finalization is required on every platform: the sweep
cannot infer whether a client intends to append more to an apparently finished
file.

## Success, retry and errors

Files stay in the inbox. Their unchanged original bytes, SHA-256 and input path
are stored in the existing archive. Successful matching import receipts are
skipped on later passes. A renamed/redelivered packet goes through the existing
deduplication rules. No separate memory database or new database schema is used.

Each run checks a roughly one-minute work budget between files and imports at
most 50 files. SQLite commits remain atomic. After a crash or timeout, the next
pass checks for a successful receipt before taking any further action. A
previously interrupted import with no receipt is held for review. Database
busy errors are retried on a later pass; invalid unchanged content is held until
the file changes or an operator explicitly retries it.

Read local `inbox-sweep/last-run.json` for the latest completed pass and per-file
results. `inbox-sweep/state.json` retains held-file status; `events.log` records
lifecycle events and counters, rotating at 256 KiB with three backups. These
files record filenames, hashes, times, counts and fixed error categories, not
conversation text, authentication values or raw exception messages. A stale
`running` result indicates a pass did not finish; check the scheduler.

After reviewing an error, run:

```sh
python inbox_sweep.py --retry-failed
```

Malformed input stays available for diagnosis. Write corrections as a new
completed deposit and preserve the original. A committed import with uncertain
matches or warnings is recorded as imported with those counters; inspect the
importer's provenance and uncertainty records as needed. No input file is
deleted or executed.

Before each batch that needs to import files, the sweep creates and verifies a
SQLite online backup in `backups/inbox-sweep/`. It retains the newest eight of
its own verified backups. It never prunes other backup directories, the archive,
or deposited files. Sweeps with only already-imported or held files do not create
database backups.

## Pause and resume

Windows:

```powershell
Disable-ScheduledTask -TaskName 'ThreadSatchel-InboxSweep'
# Resume later:
Enable-ScheduledTask -TaskName 'ThreadSatchel-InboxSweep'
Start-ScheduledTask -TaskName 'ThreadSatchel-InboxSweep'
```

Linux: `systemctl --user disable --now threadsatchel-inbox.timer`; resume with
`systemctl --user enable --now threadsatchel-inbox.timer`.

Disabling the schedule lets an active pass finish. For a persistent stop checked
between files, create `inbox-sweep/disabled.flag`; remove only that flag when
ready to resume. None of these controls stop the remote-access watchdog.
