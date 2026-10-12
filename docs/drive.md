# Commons Drive (v0.7)

One shared, versioned file store for agents and you.

- Files live at `(project, path)`; `project=""` is the shared space. Projects are isolated and ACL-checked (read/write).
- Every write is a new version (`fs_history`, `fs_history(diff_from=, diff_to=)`, `fs_restore`); deletes are soft.
- Concurrency: `expected_version` (optimistic; conflict error on mismatch, `0` = must not exist) and `fs_lock` (TTL lock).
- Text is secret-scanned on write (redacted, or rejected with `on_secret="reject"`) and indexed as `document` memory
  items, so search, the KB and the graph see it. `fs_search` combines that with path matches.
- Reads are token-budgeted (`max_tokens`, continue with `offset_line`).
- Quotas: `COMMONS_DRIVE_QUOTA_MB` per project (default 1024), `COMMONS_DRIVE_MAX_FILE_MB` (default 25).
- Artifacts link to the subtask that produced them (`fs_write(subtask_id=...)`), shown in Task breakdown.
- MCP: `fs_list, fs_read, fs_write, fs_edit, fs_move, fs_delete, fs_history, fs_restore, fs_search, fs_lock`.
- CLI: `commons drive ls|cat|put|get|rm|mv|history|restore|search|mount|sync-git|sync-rclone|usage|gc`.
- Dashboard → **Drive**: browse, preview (text/markdown/code editable, images, PDF), upload, download, history/restore.

## Backends (`COMMONS_DRIVE_BACKEND`)
| backend | settings | status |
|---|---|---|
| `local` (default) | `COMMONS_DRIVE_DIR` (~/.commons/drive) | verified |
| `s3` — AWS S3, Cloudflare R2, MinIO, Backblaze B2 | `COMMONS_DRIVE_S3_ENDPOINT`, `_BUCKET`, `_REGION`, `_PREFIX`, keys via `COMMONS_DRIVE_S3_ACCESS_KEY_REF`/`_SECRET_KEY_REF` (or AWS env) | SigV4 verified against a mock S3; not run against a real bucket |
| `webdav` — Nextcloud/ownCloud | `COMMONS_DRIVE_WEBDAV_URL`, `_USER`, `_PASSWORD_REF` | verified against a mock |
| `hostfs` — a directory on the Commons host, incl. NFS/SMB mounts the OS mounted | `COMMONS_DRIVE_HOSTFS_ROOT` (must be inside `COMMONS_HOST_ROOTS`) | verified |
| `sftp` | `pip install paramiko`; `COMMONS_DRIVE_SFTP_HOST`, `_USER`, `_PASSWORD_REF` or `_KEY_FILE` | not verified (no server) |
| `smb` (SMB/CIFS via smbprotocol) | `pip install smbprotocol`; `COMMONS_DRIVE_SMB_ROOT=\\server\share\dir`, `_USER`, `_PASSWORD_REF` | not verified (no share) |
| `ftp` / FTPS | `COMMONS_DRIVE_FTP_HOST`, `_USER`, `_PASSWORD_REF`, `COMMONS_DRIVE_FTP_TLS=1` | verified against a fake ftplib only |

Blobs are content-addressed (SHA-256), so identical content is stored once; `commons drive gc` removes unreferenced local blobs.

## Host filesystem mounts
`COMMONS_HOST_ROOTS=/srv/shared:/mnt/nas` allowlists host directories. `commons drive mount docs /srv/shared/docs`
(read-only by default, `--rw` to allow writes) exposes it at `/mnt/docs`. Paths are resolved (symlinks included) and must stay
inside an allowlisted root; `..` is rejected. Host mounts are not versioned (they are the live files).

## Sync adapters
- Git: `commons drive sync-git REPO --direction pull|push --prefix /repo [--subdir docs]` (tracked files only; push commits). verified
- Google Drive / OneDrive / Dropbox / anything rclone supports: `commons drive sync-rclone gdrive:Commons --direction push|pull`
  (configure the remote with `rclone config`). Implemented via rclone; not verified here (no accounts).
