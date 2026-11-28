"""Bitwarden through its `bw` CLI: read one item's field for a linked vault credential, and back up the vault.

There is no Bitwarden secret-broker scheme: an agent can register any reference and lease it, so a broker scheme would
hand it the value. The only way to read Bitwarden is a credential the owner made with `commons vault link` (a ref
`bw://<item-uuid>/<field>`, field = password (default), username, totp, notes, uri (first one) or a custom field's
name), used through use_credential, where the judges, approvals and scrubbing apply.

Scope fails closed: COMMONS_BW_FOLDER names the one folder Commons may read (`*` = the whole vault, not recommended:
a personal API key plus master password opens everything and Bitwarden has no per-item scoping). An item outside it is
refused with the same words as one that does not exist. This limits what a mistaken or malicious link could reach.

The session key travels only in the child's environment (BW_SESSION), never on argv; item JSON for writes goes to
bw's stdin (base64, what `bw encode` makes), never on argv. Messages carry a short reason and never bw's output.
"""
from __future__ import annotations

import base64
import json
import os
import re
import subprocess
import threading
import time
import urllib.parse

from .store import CommonsError

TIMEOUT = 60  # the first decrypt of a big vault is slow
SYNC_TIMEOUT = 120
SYNC_RETRY_S = 60  # after a failed sync, don't try again for this long
MAX_STALE_S = 3600  # with syncs failing, bw's local copy is served for at most this long after the last good sync
UUID = r"[0-9a-fA-F]{8}-(?:[0-9a-fA-F]{4}-){3}[0-9a-fA-F]{12}"  # bw get item takes anything else as a NAME search
REF_RE = re.compile(rf"^bw://({UUID})(?:/([^/\s]{{1,200}}))?$")
# env vars bw reads that could make a failure look like a success (errors as JSON on stdout, exit 0)
STRIP_ENV = ("BW_SESSION", "BW_RESPONSE", "BW_QUIET", "BW_CLEANEXIT", "BW_RAW", "BW_PRETTY")
FIELDS = ("password", "username", "totp", "notes", "uri")
# Bitwarden's limits apply to the ENCRYPTED string (password and custom field 5000, username 1000, notes and uri 10000),
# which is 30-50% longer than the text: stay well under them
MAX_VALUE, MAX_USERNAME, MAX_NOTES, MAX_URI = 3000, 700, 7000, 7000
OFF = ("COMMONS_BW_FOLDER is not set, so Commons reads nothing from Bitwarden. Set it to the one Bitwarden folder "
       "Commons may read (its name), or * for the whole vault (not recommended)")
HIDDEN = "not found, or outside the folder Commons may read (COMMONS_BW_FOLDER)"


def parse_ref(ref: str) -> tuple[str, str]:
    """bw://<item-uuid>[/<field>] -> (item id, field). The field may be percent-encoded (custom names with spaces)."""
    m = REF_RE.match(ref or "")
    if not m:
        raise CommonsError("a Bitwarden ref is bw://<item-uuid>/<field>: password (default), username, totp, notes, uri "
                           "or a custom field's name")
    return m.group(1), urllib.parse.unquote(m.group(2) or "password")


class Bitwarden:
    """One `bw` CLI. Holds the unlocked session and the sync time in process memory only."""

    def __init__(self) -> None:
        self._session: str | None = None
        self._synced: float | None = None  # last good sync
        self._sync_failed: float | None = None
        self._folders: dict[str, str] = {}
        self._lock = threading.RLock()

    # ---- the CLI
    @staticmethod
    def _bin() -> str:
        return os.environ.get("COMMONS_BW_BIN") or "bw"

    def _run(self, args: list[str], stdin: str | None = None, session: bool = True, timeout: int = TIMEOUT) -> str:
        env = {k: v for k, v in os.environ.items() if k not in STRIP_ENV} | {"BW_NOINTERACTION": "true"}
        if session:
            key = self._session or os.environ.get("BW_SESSION")
            if not key:
                raise CommonsError("Bitwarden is locked and has no session: set BW_SESSION, or COMMONS_BW_PASSWORD_FILE "
                                   "(a file holding the master password; Commons then unlocks `bw` itself)")
            env["BW_SESSION"] = key
        try:
            p = subprocess.run([self._bin(), *args, "--nointeraction"], input=stdin, env=env, capture_output=True,
                               text=True, timeout=timeout)
        except FileNotFoundError:
            raise CommonsError("the Bitwarden CLI is not installed: install `bw` or set COMMONS_BW_BIN to it") from None
        except subprocess.TimeoutExpired:
            raise CommonsError(f"bw {args[0]} timed out") from None
        except OSError:
            raise CommonsError("the Bitwarden CLI could not be run (COMMONS_BW_BIN)") from None
        if p.returncode:
            err = (p.stderr or "").lower()
            if "locked" in err:
                raise _Locked("Bitwarden is locked (the session expired or is wrong)")
            if "not logged in" in err:
                raise CommonsError("Bitwarden is not logged in on this hub: run `bw login` once as the user Commons runs as")
            if "premium" in err:
                raise CommonsError(f"bw {args[0]} needs a Bitwarden Premium account")
            if "not found" in err:
                raise _Missing(f"bw {args[0]}: not found")
            raise CommonsError(f"bw {args[0]} failed (exit {p.returncode})")
        return p.stdout

    def _call(self, args: list[str], stdin: str | None = None, timeout: int = TIMEOUT) -> str:
        """Run with the session; when the vault is locked and a password file is set, unlock once and retry."""
        with self._lock:
            if not (self._session or os.environ.get("BW_SESSION")) and os.environ.get("COMMONS_BW_PASSWORD_FILE"):
                self._unlock()
            try:
                return self._run(args, stdin, timeout=timeout)
            except _Locked:
                if not os.environ.get("COMMONS_BW_PASSWORD_FILE"):
                    raise
                self._unlock()
                return self._run(args, stdin, timeout=timeout)

    def _unlock(self) -> None:
        f = os.environ.get("COMMONS_BW_PASSWORD_FILE", "")
        try:
            out = self._run(["unlock", "--passwordfile", f, "--raw"], session=False)
        except _Locked:  # cannot happen with a good password; keep the message free of bw's words
            raise CommonsError("bw unlock failed") from None
        key = out.strip()
        if not key:
            raise CommonsError("bw unlock returned no session key (is `bw` logged in? run `bw login` once)")
        self._session = key

    # ---- reads
    def sync(self, force: bool = False) -> None:
        """`bw sync` when the last one by this process is older than COMMONS_BW_SYNC_S (default 300; 0 = never)."""
        try:
            every = float(os.environ.get("COMMONS_BW_SYNC_S", "300"))
        except ValueError:
            every = 300.0
        with self._lock:
            due = every > 0 and (self._synced is None or time.monotonic() - self._synced >= every)
            if not (force or due):
                return
            if not force and self._sync_failed is not None and time.monotonic() - self._sync_failed < SYNC_RETRY_S:
                if self._synced is None:
                    raise CommonsError("bw sync failed a moment ago and Commons has not synced yet: try again shortly")
                self._fresh_enough()
                return  # serve from bw's local copy for now
            try:
                self._call(["sync"], timeout=SYNC_TIMEOUT)
            except CommonsError:
                self._sync_failed = time.monotonic()
                if force or self._synced is None:
                    raise
                self._fresh_enough()
                return  # synced before: a short while on bw's local copy is acceptable
            self._synced, self._sync_failed = time.monotonic(), None
            self._folders.clear()  # a folder may have been renamed or recreated: ask again

    def _fresh_enough(self) -> None:
        """With syncs failing, an item moved out of the folder or deleted would stay readable: stop after MAX_STALE_S.
        (COMMONS_BW_SYNC_S=0, the owner's choice never to sync, never gets here.)"""
        if self._synced is not None and time.monotonic() - self._synced > MAX_STALE_S:
            raise CommonsError("Bitwarden has not synced for over an hour (bw sync keeps failing): fix it, or reads stay refused")

    def _json(self, args: list[str], stdin: str | None = None):
        try:
            return json.loads(self._call(args, stdin) or "null")
        except ValueError:
            raise CommonsError(f"bw {args[0]} returned something that is not JSON") from None

    def folder_id(self, name: str, create: bool = False) -> str | None:
        """The id of the folder with exactly this name; None if there is none; an error when two share the name."""
        found = self._json(["list", "folders", "--search", name])
        if not isinstance(found, list):
            raise CommonsError("bw list folders returned something that is not a list")
        hits = [f["id"] for f in found if isinstance(f, dict) and f.get("name") == name and f.get("id")]
        if len(hits) > 1:
            raise CommonsError(f"two Bitwarden folders are named {name!r}: rename one")
        if hits or not create:
            return hits[0] if hits else None
        made = self._json(["create", "folder"], _enc({"name": name}))
        if not isinstance(made, dict) or not made.get("id"):
            raise CommonsError("bw create folder returned no id")
        return made["id"]

    def _allowed_folder(self) -> str | None:
        """None = the whole vault (`*`); else the one folder's id. Anything else is a refusal."""
        want = (os.environ.get("COMMONS_BW_FOLDER") or "").strip()
        if not want:
            raise CommonsError(OFF)
        if want == "*":
            return None
        with self._lock:
            fid = self._folders.get(want) or self.folder_id(want)
            if not fid:
                raise CommonsError(HIDDEN)
            self._folders[want] = fid
            return fid

    def read(self, ref: str) -> str:
        item_id, field = parse_ref(ref)
        if not (os.environ.get("COMMONS_BW_FOLDER") or "").strip():
            raise CommonsError(OFF)  # before bw runs at all
        self.sync()
        scope = self._allowed_folder()  # after the sync: a sync forgets the cached folder id
        try:
            item = self._json(["get", "item", item_id])
        except _Missing:
            raise CommonsError(HIDDEN) from None
        if not isinstance(item, dict) or str(item.get("id", "")).lower() != item_id.lower() or \
                (scope is not None and item.get("folderId") != scope):
            raise CommonsError(HIDDEN)
        login = item.get("login") or {}
        if field == "totp":
            try:
                value = self._call(["get", "totp", item_id]).strip()
            except _Missing:
                raise CommonsError(f"the item has no field {field!r}") from None
        elif field == "uri":
            value = ((login.get("uris") or [{}])[0] or {}).get("uri")
        elif field in ("password", "username"):
            value = login.get(field)
        elif field == "notes":
            value = item.get("notes")
        else:
            value = next((f.get("value") for f in item.get("fields") or [] if f.get("name") == field), None)
        if value is None or value == "":
            raise CommonsError(f"the item has no field {field!r}")
        return str(value)


class _Locked(CommonsError):
    pass


class _Missing(CommonsError):
    pass


def _enc(obj) -> str:
    return base64.b64encode(json.dumps(obj).encode()).decode()


_BW = Bitwarden()  # process-wide: the session and the last sync live in this process's memory only


def reset() -> None:
    """Forget the session, the sync time and cached folder ids (tests; also after changing the account)."""
    global _BW
    _BW = Bitwarden()


def resolve(ref: str) -> str:
    return _BW.read(ref)


# ------------------------------------------------------------------------------------------------ backup
def _login_item(cred: dict, value: str) -> dict:
    """The Bitwarden login item for one stored credential. Extra-header values can carry secrets, so they go in one
    hidden field, never in the text of commons_auth."""
    auth = json.loads(cred["auth"] or "{}")
    headers = auth.pop("extra_headers", None)
    f = [("commons_name", cred["name"], 0), ("commons_service", cred["service"], 0), ("commons_auth", json.dumps(auth), 0),
         ("commons_risk", cred["risk"], 0), ("commons_kind", cred["kind"], 0), ("commons_scope", cred["scope"], 0)]
    if headers:
        f.append(("commons_extra_headers", json.dumps(headers), 1))
    return {"type": 1, "favorite": False, "reprompt": 0, "name": cred["name"], "notes": cred["purpose"] or cred["description"] or None,
            "login": {"username": cred["username"] or None, "password": value,
                      "uris": [{"match": None, "uri": u} for u in json.loads(cred["base_urls"] or "[]")]},
            "fields": [{"name": n, "value": v, "type": t} for n, v, t in f if v]}


def _merge(old: dict, new: dict) -> tuple[dict, bool]:
    """Our content laid over the fetched item (an edit replaces the whole item): (merged, differs)."""
    merged = json.loads(json.dumps(old))
    merged["name"], merged["notes"] = new["name"], new["notes"]
    login = merged.setdefault("login", {}) or {}
    merged["login"] = login
    login.update({k: new["login"][k] for k in ("username", "password", "uris")})
    ours = {f["name"] for f in new["fields"]} | {"commons_extra_headers", "commons_scope", "commons_kind"}
    merged["fields"] = [f for f in old.get("fields") or [] if f.get("name") not in ours] + new["fields"]

    def key(i: dict) -> tuple:
        lg = i.get("login") or {}
        return (i.get("name"), i.get("notes") or None, lg.get("username") or None, lg.get("password"),
                [u.get("uri") for u in lg.get("uris") or []],
                sorted((f.get("name"), f.get("value"), f.get("type")) for f in i.get("fields") or []
                       if f.get("name") in ours))
    return merged, key(old) != key(merged)


def export(store, folder: str = "Commons backup", dry_run: bool = False, pause: float | None = None,
           client: Bitwarden | None = None) -> dict:
    """Copy every stored credential into a Bitwarden folder as a login item (see docs). Never deletes anything. The
    result lists credential names per outcome and never a value."""
    from . import vault as V
    bw = client or _BW
    folder = (folder or "").strip()
    guarded = (os.environ.get("COMMONS_BW_FOLDER") or "").strip()
    if not folder or folder == "*":
        raise CommonsError("give the Bitwarden folder to back up into (--folder NAME)")
    if guarded in ("*", folder):
        raise CommonsError("that folder is the one COMMONS_BW_FOLDER lets Commons read for linked credentials: keep the "
                           "backup and the linked items apart, so a link can never point at a copy. Back up into another "
                           "folder, ideally in a dedicated Bitwarden account")
    if pause is None:
        try:
            pause = float(os.environ.get("COMMONS_BW_WRITE_PAUSE_S", "0.2"))
        except ValueError:
            pause = 0.2
    out: dict[str, list] = {k: [] for k in ("created", "updated", "unchanged", "skipped_linked", "skipped_too_large", "failed")}
    errors: dict[str, str] = {}
    rows = [dict(r) for r in store.q("SELECT name, service, description, base_urls, auth, risk, kind, scope, purpose, username,"
                                     " secret, ref FROM vault_creds ORDER BY name")]
    bw.sync(force=True)
    fid = bw.folder_id(folder)
    existing: dict[str, list[dict]] = {}
    if fid:
        listed = bw._json(["list", "items", "--folderid", fid])
        if not isinstance(listed, list):
            raise CommonsError("bw list items returned something that is not a list")
        for it in listed:
            n = next((f.get("value") for f in (it.get("fields") or []) if isinstance(f, dict) and f.get("name") == "commons_name"),
                     None) if isinstance(it, dict) else None
            if n:
                existing.setdefault(n, []).append(it)
    wrote, folder_failed = False, False

    def fail(name: str, why: str) -> None:
        out["failed"].append(name)
        errors[name] = why

    def too_large(i: dict) -> bool:
        lg = i["login"]
        return any(len(str(v or "").encode()) > m for v, m in (
            [(lg["password"], MAX_VALUE), (lg["username"], MAX_USERNAME), (i["notes"], MAX_NOTES)]
            + [(f["value"], MAX_VALUE) for f in i["fields"]] + [(u["uri"], MAX_URI) for u in lg["uris"]]))

    for r in rows:
        name = r["name"]
        if r["ref"]:
            out["skipped_linked"].append(name)
            continue
        if not r["secret"]:
            continue
        try:
            item = _login_item(r, V._value(store, name))
            if too_large(item):
                out["skipped_too_large"].append(name)
                continue
            have = existing.get(name, [])
            if len(have) > 1:
                fail(name, "two items in the folder claim this commons_name: leave them, remove one in Bitwarden")
                continue
            if have and (have[0].get("type") != 1 or not re.fullmatch(UUID, str(have[0].get("id")))):
                fail(name, "the item in the folder with this commons_name is not a login: left untouched")
                continue
            if have:
                merged, differs = _merge(have[0], item)
                if not differs:
                    out["unchanged"].append(name)
                    continue
                if not dry_run:
                    if wrote and pause:
                        time.sleep(pause)
                    wrote = True
                    bw._call(["edit", "item", have[0]["id"]], stdin=_enc(merged))
                out["updated"].append(name)
            else:
                if not dry_run:
                    if folder_failed:
                        fail(name, "the backup folder could not be created")
                        continue
                    if fid is None:
                        folder_failed = True  # one attempt: a second could leave two folders of the same name
                        fid = bw.folder_id(folder, create=True)
                        folder_failed = False
                    if wrote and pause:
                        time.sleep(pause)
                    wrote = True
                    bw._call(["create", "item"], stdin=_enc(item | {"folderId": fid, "organizationId": None}))
                out["created"].append(name)
        except CommonsError as e:
            fail(name, str(e)[:160])
        except Exception as e:  # noqa: BLE001 - one credential's odd answer from bw never aborts the run; no detail: it could carry a value
            fail(name, f"unexpected error ({type(e).__name__})")
    counts = {k: len(v) for k, v in out.items()}
    if not dry_run:
        store.audit(None, "vault.export", folder, {"to": "bitwarden", "folder": folder, **counts})
    return {"to": "bitwarden", "folder": folder, "dry_run": dry_run, "counts": counts, **out,
            **({"errors": errors} if errors else {})}
