# Bitwarden

Commons can use items from a Bitwarden vault as vault credentials, and back its own vault up to a Bitwarden folder,
through the `bw` CLI (standard library only; install `bw` and run `bw login` once on the hub, as the user Commons
runs as).

> Tested against a fake `bw` and checked against the CLI source for 2026.11.1. It has **not** been run against a live
> Bitwarden account: try it on a throwaway account first.

## Linking an item: the only way to read Bitwarden

```
commons vault link plex --ref bw://<item-uuid>/password --service plex \
    --base-url http://plex.lan:32400 --auth header:X-Plex-Token
```

Takes the options of `commons vault set` and no value on stdin. `<item-uuid>` is the item's id (a UUID: `bw get item`
treats anything else as a name search, so it is refused) and `<field>` is `password` (the default), `username`, `totp`,
`notes`, `uri` (the first one) or a custom field's name (percent-encode spaces). The credential stores nothing:
`use_credential` reads the item when it is used, and everything else (base URLs, judges, approvals, scrubbing, audit)
is unchanged. The link is checked at link time: the item must be readable then.

There is **no `bw://` secret-broker scheme**. An agent can register any reference and lease it, which would hand it the
value and skip the judges; a linked credential is reachable only through `use_credential`. Only the owner CLI creates
a link: no agent tool or dashboard route can set or change one, a deposit or generate onto the name is refused,
`vault_reveal` refuses it, a `commons://NAME` lease refuses a linked name, and `commons vault set` refuses it until you
`rm` it. Re-linking a name to a different item, or with other base URLs or auth, deletes that credential's grants and voids any request still waiting for your approval (a later yes does nothing).

| Variable | Purpose |
|---|---|
| `COMMONS_BW_FOLDER` | **Required.** The one Bitwarden folder (by name) Commons may read. `*` allows the whole vault: not recommended. |
| `BW_SESSION` | A session key from `bw unlock --raw`. |
| `COMMONS_BW_PASSWORD_FILE` | Instead of `BW_SESSION`: a file with the master password. Commons runs `bw unlock` itself, keeps the session in memory and unlocks again once if it expires. |
| `COMMONS_BW_BIN` | The `bw` binary (default `bw` on PATH). |
| `COMMONS_BW_SYNC_S` | Seconds between `bw sync` runs (default 300, 0 = never). After a failed sync Commons waits a minute and serves from `bw`'s local copy if it has synced before, but for at most an hour after the last good sync, then reads are refused (an item moved out of the folder or deleted must not stay readable). With `0` (you chose never to sync) that cap does not apply. |

Why a folder: a personal API key plus master password opens the whole vault and Bitwarden has no per-item scoping. The
folder limits what a mistaken or malicious link could reach, and fails closed: with `COMMONS_BW_FOLDER` unset every
read is refused, and an item outside the folder is refused with the same message as one that does not exist. Put the
items you link in that folder and nothing else. The session key goes to `bw` only through its environment, never a
command line; ambient `BW_RESPONSE`, `BW_QUIET`, `BW_CLEANEXIT`, `BW_RAW` and `BW_PRETTY` are not passed on; error
messages never repeat what `bw` printed.

`commons vault link` and `commons vault export` run `bw` from the shell you run them in, not inside the hub: that
shell needs the same `BW_SESSION` or `COMMONS_BW_PASSWORD_FILE`, `COMMONS_BW_FOLDER` and `COMMONS_BW_BIN` as the hub,
and must run as the user that did `bw login` (`bw` keeps its login in that user's data directory).

## Backup: `commons vault export --to bitwarden`

```
commons vault export --to bitwarden [--folder "Commons backup"] [--dry-run]
```

**This copies decrypted secrets out of Commons.** The Bitwarden account then holds everything the vault key protects,
so it is as sensitive as that key: use a dedicated Bitwarden account (or at least a folder other than
`COMMONS_BW_FOLDER`; Commons refuses that folder so backups and linked items stay apart).

Each stored credential becomes a login item (name, username, password, base URLs as URIs, purpose as notes) with
custom fields `commons_name` (the key it is matched by: item names may repeat in Bitwarden), `commons_service`,
`commons_auth`, `commons_risk`, `commons_kind`, `commons_scope`; extra-header values go into a hidden field,
`commons_extra_headers`. Missing items are created, items whose content differs are edited in place, identical ones are
left alone, and nothing in Bitwarden is ever deleted. **The item's name and notes belong to Commons in a backup: each run overwrites its name, notes, username, password,
URIs and `commons_*` fields**, so edit those in Commons, not in Bitwarden; other fields and a TOTP you added are kept. An item with our `commons_name` that is not a
login is reported as failed and left untouched. Linked credentials are skipped (they live in Bitwarden already). A
value Bitwarden's limits would refuse (limits apply to the encrypted text, 30-50% longer: password or custom field over
3,000 bytes, notes or a URI over 7,000, username over 700) is skipped as `skipped_too_large`. A failed write is
recorded and the run continues; `COMMONS_BW_WRITE_PAUSE_S` (default 0.2) spaces the writes. The summary lists names per
outcome (created, updated, unchanged, skipped_linked, skipped_too_large, failed) and never a value; the exit code is 1
when anything failed or was too large. One audit row (`vault.export`) records the counts and folder. `--dry-run` reads
Bitwarden and writes nothing, not even the folder.
