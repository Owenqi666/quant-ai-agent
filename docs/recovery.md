# Backup, restore, and upgrade

Application code and user state are separate. The launcher's `--home` selects a workspace containing the database and owned input/result files. Keep that workspace private; it contains research material and local paths.

The current maintenance implementation supports schema 1-29 and rejects future schema versions. This is an implementation compatibility range, not evidence that every historical upgrade combination has been tested. Restore retains the saved schema; normal workspace initialization performs the ordered migrations.

## Offline maintenance

Stop the launcher with `Ctrl+C` and wait for the API, worker, and tool children to exit. Stop separately launched processes too. Backup requires an exclusive workspace lease and fails while an execution process still holds the lease. Do not remove the persistent sibling workspace lock file manually.

Run these commands from the source checkout. Each backup and restore target must **not already exist**, including as an empty directory; its parent must exist.

```sh
mkdir -p ./var/backups
.venv/bin/python -B -m paper_alpha.server.backup backup \
  --home ./var/demo --out ./var/backups/demo-01
.venv/bin/python -B -m paper_alpha.server.backup verify \
  --backup ./var/backups/demo-01
.venv/bin/python -B -m paper_alpha.server.backup restore \
  --backup ./var/backups/demo-01 --home ./var/demo-restored-01
.venv/bin/python -B -m paper_alpha.server.launcher \
  --home ./var/demo-restored-01 --port 8766
```

Open [the restored workbench](http://127.0.0.1:8766/) and inspect the original sources, revisions, experiments, result verification, reviews, and reports. Use a new target name for every subsequent backup or restore.

## What is preserved

The backup uses SQLite's backup API to capture committed state consistently, including WAL commits. It copies owned research assets and saves a manifest of file sizes and SHA256 digests. WAL/SHM files and active runtime locks are not copied as database state.

Material drafts and immutable material versions, selected-source bindings, definition-repair records, research revisions, attempts, snapshots, computed results, events, frozen reports, reviews, regression cases, and approved checks remain historical records. Required database-owned provider history is retained and verified. Restore rebases supported database path columns; saved engine snapshots and result/report content are not regenerated.

Browser drafts and pending requests are local browser state and are not included. Native model accounts, credentials, external authorization files, and current host execution access are managed separately. Restore pauses provider dispatch and changes recovery identity; it does not revive old authority or reset deadlines and call budgets. Reading restored history does not call a model. A restored automated material version retains its unresolved scientific content and recorded approval status; recovery does not convert mechanical verification into human approval.

## Failure behavior

- Existing targets, overlapping source/target directories, symlinks, hard links, special files, path traversal, unlisted files, and mismatched digests are rejected.
- A backup without its complete manifest cannot be restored. An unfinished restore marked `.restore-incomplete` cannot be started as a workspace.
- A normal caught failure cleans up the newly created target. After forced termination, retain the incomplete directory for diagnosis and retry with a new target.
- Worker recovery records lost execution ownership as interruption requiring explicit action. It does not overwrite the previous attempt or automatically repeat unknown native execution.

The manifest detects corruption and accidental changes; it is not encryption or a source signature. Backups remain private and should come from a trusted location. Do not publish them as sample project artifacts.

## Upgrade and rollback

Before upgrading, stop the old application, create and verify a backup, and retain the old software with its matching backup. Restore into a new workspace and start the new source version there to inspect migration and historical readback before deciding to use it.

Do not point older software at an already upgraded database. Roll back with the retained old software and a separately restored pre-upgrade backup. Keep original workspaces and historical result files intact.
