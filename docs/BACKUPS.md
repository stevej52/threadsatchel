# Backups and recovery

## Daily backups

Create a dedicated backup directory on a mounted disk outside the installation. Run the following from the project directory, substituting the directory you created.

```sh
python scheduled_memory_backup.py --destination "<backup directory>" --keep 14
python install_schedule.py backup --destination "<backup directory>" --keep 14
```

The first command makes a backup immediately. The second installs a Windows Task Scheduler task or a Linux systemd user timer. Each invokes local Python under your account. The computer must be on and the selected directory accessible. On other platforms use the first command in your scheduler. Successful reruns on the same local date verify and reuse the existing ZIP. The default retention is the latest 14 verified backups of this installation.

Each ZIP contains an online SQLite snapshot, any recovery journal, available handoffs, inbox deposits, documentation, top-level source and supported configuration files. These files can contain private archive content. Model weights, environments and earlier backups are excluded. The job checks SQLite integrity and reads back every ZIP member to verify its SHA-256 before publishing the ZIP and its JSON receipt. Retention checks receipt ownership and the saved ZIP checksum before deletion. Keep the receipt alongside the ZIP. SQLite snapshots of the archive and recovery journal are taken separately, and a changed supporting file can require another run.

## Controlled restore

Keep the current inbox originals and the most recent `inbox-sweep/` directory, especially `recovery.sqlite3`. Stop the server and all writers, including capture, sync, any connector and the inbox sweep. Close all SQLite readers before replacing the database. For a daily ZIP, verify its hash against the adjacent receipt and extract the `memory.sqlite3` member into a separate directory. A completed hourly `backups/inbox-sweep/before-*.sqlite3` can be used directly.

1. Run `python inbox_recovery.py --prepare-restore "<snapshot file>"`. This checks the snapshot and records the affected inbox files before the restore. A recovery hold yields a nonzero exit code.
2. Preserve the original database and any associated `-wal` and `-shm` sidecars together. With all connections closed, install the selected standalone snapshot as `memory.sqlite3` in the installation. Keep the preserved sidecars separate from the restored database. Keep the newer recovery journal and original inbox deposits in place.
3. Run `python inbox_recovery.py --status`. Review the oldest held file and run `python inbox_recovery.py --replay-next` once. Each invocation checks SQLite integrity and verifies the original file hash and receipt. A failed check holds the queue for investigation. Repeat until the journal reports recovery is clear, then resume the writers.

An ordinary sweep also detects missing previously observed receipts and interrupted import passes. `--retry-failed` cannot release a recovery hold. The replay journal covers local inbox imports it observed. Restoring other writes may require their original source, capture queue, or a later backup.
