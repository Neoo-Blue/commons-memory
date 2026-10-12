"""Commons Drive: a shared, versioned file/artifact store for agents and the user.

Files live at (project, path). Content is stored once per SHA-256 in a pluggable blob backend; every write creates a
version (history, diff, restore). Writes use optimistic concurrency (`expected_version`) and optional locks. Text is
secret-scanned (redacted, or rejected with `on_secret="reject"`) and indexed into memory (kind `document`, so search,
the KB and the graph see it). Per-project ACLs (the store's), quotas and a max file size apply. Artifacts can be
linked to the jobs/subtasks that produced them. Sync adapters mirror a folder to/from git repos and rclone remotes.

Blob backends (COMMONS_DRIVE_BACKEND or Drive(backend=...)):
  local    ~/.commons/drive (default)                                                   verified
  s3       AWS S3 / Cloudflare R2 / MinIO / Backblaze B2 (SigV4 over urllib, path-style)  verified against a mock S3
  webdav   Nextcloud/ownCloud/any WebDAV (PUT/GET/DELETE/MKCOL)                           verified against a mock
  hostfs   a directory on the Commons host (allowlisted roots; NFS/SMB mounts work too)  verified
  sftp     SFTP via paramiko (optional dependency)                                      not verified (no server here)
  smb      SMB/CIFS via smbprotocol (optional dependency)                               not verified (no share here)
  ftp      FTP/FTPS via ftplib                                                          verified against a fake ftplib
Host mounts (`mount_host`) expose allowlisted host directories read-only or read-write as Drive folders.
"""
from __future__ import annotations

import datetime as _dt
import difflib
import hashlib
import hmac
import json
import mimetypes
import os
import posixpath
import re
import shutil
import subprocess
import tempfile
import threading
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

from .ids import new_id, now
from .redact import redact, scan
from .store import CommonsError, Store

SCHEMA = """
CREATE TABLE IF NOT EXISTS drive_files(id TEXT PRIMARY KEY, project TEXT NOT NULL DEFAULT '', path TEXT, size INTEGER,
  mime TEXT, sha TEXT, version INTEGER, tags TEXT, meta TEXT, created_by TEXT, updated_by TEXT, created_at REAL,
  updated_at REAL, deleted_at REAL, lock_by TEXT, lock_until REAL, item_id TEXT, UNIQUE(project, path));
CREATE TABLE IF NOT EXISTS drive_versions(file_id TEXT, version INTEGER, sha TEXT, size INTEGER, path TEXT, author TEXT,
  message TEXT, created_at REAL, PRIMARY KEY(file_id, version));
CREATE TABLE IF NOT EXISTS drive_links(file_id TEXT, ref TEXT, rel TEXT, created_at REAL, PRIMARY KEY(file_id, ref, rel));
CREATE TABLE IF NOT EXISTS drive_mounts(name TEXT PRIMARY KEY, root TEXT, read_only INTEGER, project TEXT, created_at REAL);
"""
TEXT_EXT = {".md", ".txt", ".py", ".js", ".ts", ".tsx", ".jsx", ".json", ".yaml", ".yml", ".toml", ".csv", ".html", ".css",
            ".sh", ".sql", ".go", ".rs", ".java", ".rb", ".c", ".h", ".cpp", ".ini", ".cfg", ".xml", ".mdx", ".tex", ".log"}


# ------------------------------------------------------------------ blob backends
class LocalBlobs:
    kind = "local"

    def __init__(self, root: str | None = None):
        self.root = Path(root or os.environ.get("COMMONS_DRIVE_DIR") or Path.home() / ".commons" / "drive")
        self.root.mkdir(parents=True, exist_ok=True)

    def _p(self, sha):
        return self.root / sha[:2] / sha

    def put(self, sha: str, data: bytes) -> None:
        p = self._p(sha)
        if not p.exists():
            p.parent.mkdir(exist_ok=True)
            tmp = p.with_name(f"{p.name}.{os.getpid()}.{threading.get_ident()}.tmp")
            tmp.write_bytes(data)
            os.replace(tmp, p)

    def get(self, sha: str) -> bytes:
        return self._p(sha).read_bytes()

    def delete(self, sha: str) -> None:
        self._p(sha).unlink(missing_ok=True)

    def exists(self, sha: str) -> bool:
        return self._p(sha).exists()


def _sigv4(method, url, region, ak, sk, body=b"", service="s3", headers=None):
    u = urllib.parse.urlparse(url)
    t = _dt.datetime.now(_dt.timezone.utc)
    amz, day = t.strftime("%Y%m%dT%H%M%SZ"), t.strftime("%Y%m%d")
    ph = hashlib.sha256(body).hexdigest()
    h = {"host": u.netloc, "x-amz-date": amz, "x-amz-content-sha256": ph} | {k.lower(): v for k, v in (headers or {}).items()}
    q = "&".join(f"{urllib.parse.quote(k, safe='-_.~')}={urllib.parse.quote(v, safe='-_.~')}"
                 for k, v in sorted(urllib.parse.parse_qsl(u.query, keep_blank_values=True)))
    signed = ";".join(sorted(h))
    creq = "\n".join([method, urllib.parse.quote(u.path or "/", safe="/-_.~"), q,
                      "".join(f"{k}:{h[k].strip()}\n" for k in sorted(h)), signed, ph])
    scope = f"{day}/{region}/{service}/aws4_request"
    sts = "\n".join(["AWS4-HMAC-SHA256", amz, scope, hashlib.sha256(creq.encode()).hexdigest()])
    k = ("AWS4" + sk).encode()
    for part in (day, region, service, "aws4_request"):
        k = hmac.new(k, part.encode(), hashlib.sha256).digest()
    sig = hmac.new(k, sts.encode(), hashlib.sha256).hexdigest()
    h["authorization"] = f"AWS4-HMAC-SHA256 Credential={ak}/{scope}, SignedHeaders={signed}, Signature={sig}"
    return h


class S3Blobs:
    """S3-compatible object storage (AWS S3, Cloudflare R2, MinIO, Backblaze B2) with path-style URLs."""
    kind = "s3"

    def __init__(self, endpoint: str, bucket: str, region: str = "auto", access_key: str = "", secret_key: str = "",
                 prefix: str = "commons-drive/"):
        self.endpoint, self.bucket, self.region = endpoint.rstrip("/"), bucket, region
        self.ak, self.sk, self.prefix = access_key, secret_key, prefix

    def _req(self, method, key, body=b"", headers=None):
        url = f"{self.endpoint}/{self.bucket}/{urllib.parse.quote(self.prefix + key)}"
        h = _sigv4(method, url, self.region, self.ak, self.sk, body, headers=headers)
        req = urllib.request.Request(url, data=body if method in ("PUT", "POST") else None, method=method, headers=h)
        with urllib.request.urlopen(req, timeout=60) as r:  # noqa: S310 - configured endpoint
            return r.status, r.read()

    def put(self, sha, data):
        if not self.exists(sha):
            self._req("PUT", sha, data)

    def get(self, sha):
        return self._req("GET", sha)[1]

    def delete(self, sha):
        try:
            self._req("DELETE", sha)
        except urllib.error.HTTPError as e:
            if e.code != 404:
                raise

    def exists(self, sha):
        try:
            return self._req("HEAD", sha)[0] == 200
        except urllib.error.HTTPError as e:
            if e.code == 404:
                return False
            raise


class WebDAVBlobs:
    kind = "webdav"

    def __init__(self, url: str, user: str = "", password: str = ""):
        import base64
        self.url = url.rstrip("/") + "/"
        self.auth = {"Authorization": "Basic " + base64.b64encode(f"{user}:{password}".encode()).decode()} if user else {}
        self._dirs: set[str] = set()

    def _r(self, method, path, body=None):
        req = urllib.request.Request(self.url + path, data=body, method=method, headers=self.auth)
        with urllib.request.urlopen(req, timeout=60) as r:  # noqa: S310
            return r.status, r.read()

    def put(self, sha, data):
        d = sha[:2]
        if d not in self._dirs:
            try:
                self._r("MKCOL", d + "/")
            except urllib.error.HTTPError as e:
                if e.code not in (405, 409):  # 405 = exists
                    raise
            self._dirs.add(d)
        self._r("PUT", f"{d}/{sha}", data)

    def get(self, sha):
        return self._r("GET", f"{sha[:2]}/{sha}")[1]

    def delete(self, sha):
        try:
            self._r("DELETE", f"{sha[:2]}/{sha}")
        except urllib.error.HTTPError:
            pass

    def exists(self, sha):
        try:
            return self._r("HEAD", f"{sha[:2]}/{sha}")[0] == 200
        except urllib.error.HTTPError:
            return False


class HostFSBlobs(LocalBlobs):
    """Blobs in an allowlisted host directory (also NFS/SMB mounts mounted by the OS)."""
    kind = "hostfs"

    def __init__(self, root: str):
        check_host_path(root)
        super().__init__(root)


class SFTPBlobs:
    kind = "sftp"

    def __init__(self, host: str, user: str, password: str = "", key_file: str = "", root: str = "commons-drive", port: int = 22):
        try:
            import paramiko  # type: ignore
        except ImportError as e:
            raise CommonsError("sftp backend needs `pip install paramiko`") from e
        t = paramiko.Transport((host, port))
        t.connect(username=user, password=password or None,
                  pkey=paramiko.RSAKey.from_private_key_file(key_file) if key_file else None)
        self.sftp, self.root = paramiko.SFTPClient.from_transport(t), root.rstrip("/")
        self._mk(self.root)

    def _mk(self, d):
        try:
            self.sftp.mkdir(d)
        except OSError:
            pass

    def put(self, sha, data):
        self._mk(f"{self.root}/{sha[:2]}")
        with self.sftp.open(f"{self.root}/{sha[:2]}/{sha}", "wb") as f:
            f.write(data)

    def get(self, sha):
        with self.sftp.open(f"{self.root}/{sha[:2]}/{sha}", "rb") as f:
            return f.read()

    def delete(self, sha):
        try:
            self.sftp.remove(f"{self.root}/{sha[:2]}/{sha}")
        except OSError:
            pass

    def exists(self, sha):
        try:
            self.sftp.stat(f"{self.root}/{sha[:2]}/{sha}")
            return True
        except OSError:
            return False


class SMBBlobs:
    kind = "smb"

    def __init__(self, unc_root: str, user: str = "", password: str = ""):
        try:
            import smbclient  # type: ignore  (smbprotocol)
        except ImportError as e:
            raise CommonsError("smb backend needs `pip install smbprotocol`") from e
        server = unc_root.strip("\\").split("\\")[0]
        if user:
            smbclient.register_session(server, username=user, password=password)
        self.c, self.root = smbclient, unc_root.rstrip("\\")
        self.c.makedirs(self.root, exist_ok=True)

    def _p(self, sha):
        return f"{self.root}\\{sha[:2]}\\{sha}"

    def put(self, sha, data):
        self.c.makedirs(f"{self.root}\\{sha[:2]}", exist_ok=True)
        with self.c.open_file(self._p(sha), mode="wb") as f:
            f.write(data)

    def get(self, sha):
        with self.c.open_file(self._p(sha), mode="rb") as f:
            return f.read()

    def delete(self, sha):
        try:
            self.c.remove(self._p(sha))
        except OSError:
            pass

    def exists(self, sha):
        try:
            self.c.stat(self._p(sha))
            return True
        except OSError:
            return False


class FTPBlobs:
    kind = "ftp"

    def __init__(self, host: str, user: str = "anonymous", password: str = "", root: str = "commons-drive", tls: bool = True):
        import ftplib
        self.ftp = (ftplib.FTP_TLS if tls else ftplib.FTP)(host, timeout=60)
        self.ftp.login(user, password)
        if tls:
            self.ftp.prot_p()
        self.root = root.rstrip("/")
        self._mk(self.root)

    def _mk(self, d):
        import ftplib
        try:
            self.ftp.mkd(d)
        except ftplib.error_perm:
            pass

    def put(self, sha, data):
        import io
        self._mk(f"{self.root}/{sha[:2]}")
        self.ftp.storbinary(f"STOR {self.root}/{sha[:2]}/{sha}", io.BytesIO(data))

    def get(self, sha):
        buf = bytearray()
        self.ftp.retrbinary(f"RETR {self.root}/{sha[:2]}/{sha}", buf.extend)
        return bytes(buf)

    def delete(self, sha):
        import ftplib
        try:
            self.ftp.delete(f"{self.root}/{sha[:2]}/{sha}")
        except ftplib.error_perm:
            pass

    def exists(self, sha):
        import ftplib
        try:
            return self.ftp.size(f"{self.root}/{sha[:2]}/{sha}") is not None
        except ftplib.error_perm:
            return False


def _ref_or_env(s: Store, name: str) -> str:
    """Credential from COMMONS_DRIVE_<NAME>_REF (secret reference) or plain env var (for local dev)."""
    ref = os.environ.get(f"COMMONS_DRIVE_{name}_REF")
    if ref:
        from .secrets import Broker
        return Broker(s).resolve_internal(ref, purpose="drive backend")
    return os.environ.get(f"COMMONS_DRIVE_{name}", "")


def backend_from_env(s: Store):
    k = os.environ.get("COMMONS_DRIVE_BACKEND", "local")
    e = os.environ.get
    if k == "local":
        return LocalBlobs()
    if k == "s3":
        return S3Blobs(e("COMMONS_DRIVE_S3_ENDPOINT", "https://s3.amazonaws.com"), e("COMMONS_DRIVE_S3_BUCKET", "commons"),
                       e("COMMONS_DRIVE_S3_REGION", "us-east-1"),
                       _ref_or_env(s, "S3_ACCESS_KEY") or e("AWS_ACCESS_KEY_ID", ""),
                       _ref_or_env(s, "S3_SECRET_KEY") or e("AWS_SECRET_ACCESS_KEY", ""), e("COMMONS_DRIVE_S3_PREFIX", "commons-drive/"))
    if k == "webdav":
        return WebDAVBlobs(e("COMMONS_DRIVE_WEBDAV_URL", ""), e("COMMONS_DRIVE_WEBDAV_USER", ""), _ref_or_env(s, "WEBDAV_PASSWORD"))
    if k == "hostfs":
        return HostFSBlobs(e("COMMONS_DRIVE_HOSTFS_ROOT", ""))
    if k == "sftp":
        return SFTPBlobs(e("COMMONS_DRIVE_SFTP_HOST", ""), e("COMMONS_DRIVE_SFTP_USER", ""), _ref_or_env(s, "SFTP_PASSWORD"),
                         e("COMMONS_DRIVE_SFTP_KEY_FILE", ""))
    if k == "smb":
        return SMBBlobs(e("COMMONS_DRIVE_SMB_ROOT", ""), e("COMMONS_DRIVE_SMB_USER", ""), _ref_or_env(s, "SMB_PASSWORD"))
    if k == "ftp":
        return FTPBlobs(e("COMMONS_DRIVE_FTP_HOST", ""), e("COMMONS_DRIVE_FTP_USER", "anonymous"), _ref_or_env(s, "FTP_PASSWORD"),
                        tls=e("COMMONS_DRIVE_FTP_TLS", "1") == "1")
    raise CommonsError(f"unknown COMMONS_DRIVE_BACKEND {k}")


def check_host_path(p: str, base: str | None = None) -> Path:
    """Host paths must be inside COMMONS_HOST_ROOTS (colon/comma-separated). Symlinks are resolved first. With
    `base` (a mount's root) the path must also stay inside that root: a symlink in one mount must not reach into
    another allowlisted root."""
    roots = [Path(x).expanduser().resolve() for x in re.split(r"[,:]", os.environ.get("COMMONS_HOST_ROOTS", "")) if x.strip()]
    if not roots:
        raise CommonsError("host filesystem access is off: set COMMONS_HOST_ROOTS to allowlisted directories")
    rp = Path(p).expanduser().resolve()
    if not any(rp == r or r in rp.parents for r in roots):
        raise CommonsError(f"{rp} is outside COMMONS_HOST_ROOTS")
    if base is not None:
        b = Path(base).expanduser().resolve()
        if not (rp == b or b in rp.parents):
            raise CommonsError(f"{rp} escapes its mount root {b}")
    return rp


# ------------------------------------------------------------------ the drive
def _norm(path: str) -> str:
    p = posixpath.normpath("/" + (path or "").strip().lstrip("/"))
    if ".." in p.split("/") or "\x00" in p:
        raise CommonsError("invalid path")
    return p


def _is_text(path: str, data: bytes, mime: str | None) -> bool:
    if (mime or "").startswith(("image/", "audio/", "video/")) or b"\x00" in data[:8192]:
        return False
    try:
        data.decode("utf-8")
        return True
    except UnicodeDecodeError:
        return False


class Drive:
    def __init__(self, s: Store, backend=None):
        self.s = s
        self.b = backend or backend_from_env(s)

    # ---- access control + quotas
    def _proj(self, project: str | None, need: str) -> str:
        if project:
            pid = self.s.project_id(project)
            self.s.check_project(pid, need)
            return project
        return ""

    def _quota(self, project: str, extra: int) -> None:
        mx = float(os.environ.get("COMMONS_DRIVE_MAX_FILE_MB", "25")) * 1e6
        if extra > mx:
            raise CommonsError(f"file too large ({extra / 1e6:.1f} MB > {mx / 1e6:.0f} MB)")
        q = float(os.environ.get("COMMONS_DRIVE_QUOTA_MB", "1024")) * 1e6
        used = self.s.q("SELECT COALESCE(SUM(size),0) n FROM drive_files WHERE project=? AND deleted_at IS NULL", (project,))[0]["n"]
        if used + extra > q:
            raise CommonsError(f"project quota exceeded ({(used + extra) / 1e6:.1f} MB > {q / 1e6:.0f} MB)")

    def _row(self, project: str, path: str, deleted: bool = False):
        r = self.s.q("SELECT * FROM drive_files WHERE project=? AND path=?" + ("" if deleted else " AND deleted_at IS NULL"),
                     (project, path))
        return dict(r[0]) if r else None

    def _mount_for(self, path: str):
        for m in self.s.q("SELECT * FROM drive_mounts"):
            pre = f"/mnt/{m['name']}"
            if path == pre or path.startswith(pre + "/"):
                return m, path[len(pre):].lstrip("/")
        return None, None

    # ---- write
    def write(self, path: str, content: bytes | str, *, project: str | None = None, agent_id: str | None = None,
              expected_version: int | None = None, message: str = "", tags: list[str] | None = None,
              mime: str | None = None, link: str | None = None, on_secret: str = "redact") -> dict:
        path = _norm(path)
        m, rel = self._mount_for(path)
        if m:
            return self._host_write(m, rel, content)
        project = self._proj(project, "write")
        data = content.encode() if isinstance(content, str) else bytes(content)
        mime = mime or mimetypes.guess_type(path)[0] or ("text/plain" if Path(path).suffix in TEXT_EXT else "application/octet-stream")
        found = []
        if _is_text(path, data, mime):
            txt = data.decode()
            found = [f["kind"] if isinstance(f, dict) else str(f) for f in scan(txt)]
            if found:
                if on_secret == "reject":
                    raise CommonsError(f"secret detected ({', '.join(sorted(set(found)))}); store it as a secret reference instead")
                data = redact(txt)[0].encode()
        cur = self._row(project, path)
        if cur and cur["lock_by"] and cur["lock_by"] != agent_id and (cur["lock_until"] or 0) > now():
            raise CommonsError(f"{path} is locked by {self.s.label(cur['lock_by'])[1]} until {_dt.datetime.fromtimestamp(cur['lock_until']):%H:%M:%S}")
        if expected_version is not None and (cur["version"] if cur else 0) != expected_version:
            raise CommonsError(f"version conflict: {path} is at v{cur['version'] if cur else 0}, you edited v{expected_version}. "
                               "Re-read, merge, and write again.")
        sha = hashlib.sha256(data).hexdigest()
        if cur and cur["sha"] == sha:
            return self._info(cur) | {"unchanged": True}
        self._quota(project, len(data) - (cur["size"] if cur else 0))
        self.b.put(sha, data)
        t = now()
        with self.s.lock:
            if cur:
                v = cur["version"] + 1
                c = self.s.db.execute("UPDATE drive_files SET size=?, mime=?, sha=?, version=?, updated_by=?, updated_at=?, "
                                      "tags=COALESCE(?, tags) WHERE id=? AND version=?",
                                      (len(data), mime, sha, v, agent_id, t, json.dumps(tags) if tags is not None else None,
                                       cur["id"], cur["version"]))
                if c.rowcount != 1:
                    raise CommonsError(f"version conflict on {path} (concurrent write); re-read and retry")
                fid = cur["id"]
            else:
                old = self._row(project, path, deleted=True)
                if old:  # re-create over a deleted file: keep its history
                    fid, v = old["id"], old["version"] + 1
                    self.s.db.execute("UPDATE drive_files SET size=?, mime=?, sha=?, version=?, updated_by=?, updated_at=?, "
                                      "deleted_at=NULL, tags=? WHERE id=?", (len(data), mime, sha, v, agent_id, t,
                                                                             json.dumps(tags or []), fid))
                else:
                    fid, v = new_id("fil"), 1
                    self.s.db.execute("INSERT INTO drive_files(id,project,path,size,mime,sha,version,tags,meta,created_by,"
                                      "updated_by,created_at,updated_at) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)",
                                      (fid, project, path, len(data), mime, sha, v, json.dumps(tags or []), "{}", agent_id,
                                       agent_id, t, t))
            self.s.db.execute("INSERT INTO drive_versions(file_id,version,sha,size,path,author,message,created_at) "
                              "VALUES(?,?,?,?,?,?,?,?)", (fid, v, sha, len(data), path, agent_id, redact(message)[0][:500], t))
        self._index(fid, project, path, data, mime, agent_id)
        if link:
            self.link(fid, link, "produced_by")
        self.s.audit(agent_id, "drive.write", f"{project}:{path}", {"version": v, "size": len(data)})
        return self._info(self._row(project, path)) | ({"redacted": sorted(set(found))} if found else {})

    def _index(self, fid, project, path, data, mime, agent_id):
        """Text files become a `document` memory item (search, KB, graph); the previous version's item is retired."""
        old = self.s.q("SELECT item_id FROM drive_files WHERE id=?", (fid,))[0]["item_id"]
        if old:
            self.s.x("UPDATE items SET superseded_by=? WHERE id=?", (fid, old))
        iid = None
        if _is_text(path, data, mime) and len(data) <= 1_000_000:
            txt = data.decode()
            iid = self.s.add_item("document", txt[:200_000], title=f"{project or 'shared'}:{path}", project=project or None,
                                  agent_id=agent_id, tags=["drive"], meta={"drive_file": fid, "path": path})["id"]
        self.s.x("UPDATE drive_files SET item_id=? WHERE id=?", (iid, fid))
        try:
            self.s.relate(fid, iid, "content") if iid else None
            if project:
                self.s.relate(fid, self.s.project_id(project), "in_project")
        except Exception:  # noqa: BLE001
            pass

    # ---- read
    def _info(self, r) -> dict:
        r = dict(r)
        return {"id": r["id"], "project": r["project"] or None, "path": r["path"], "size": r["size"], "mime": r["mime"],
                "version": r["version"], "sha": r["sha"][:16], "tags": json.loads(r["tags"] or "[]"),
                "updated_by": self.s.label(r["updated_by"])[1] if r["updated_by"] else None, "updated_at": r["updated_at"],
                "locked_by": self.s.label(r["lock_by"])[1] if r["lock_by"] and (r["lock_until"] or 0) > now() else None,
                "deleted": bool(r.get("deleted_at"))}

    def stat(self, path: str, project: str | None = None) -> dict:
        project = self._proj(project, "read")
        r = self._row(project, _norm(path))
        if not r:
            raise CommonsError(f"no such file {path}")
        return self._info(r) | {"links": [dict(x) for x in self.s.q("SELECT ref, rel FROM drive_links WHERE file_id=?", (r["id"],))]}

    def read_bytes(self, path: str, project: str | None = None, version: int | None = None) -> tuple[bytes, dict]:
        path = _norm(path)
        m, rel = self._mount_for(path)
        if m:
            p = check_host_path(os.path.join(m["root"], rel), base=m["root"])
            return p.read_bytes(), {"path": path, "mount": m["name"], "mime": mimetypes.guess_type(str(p))[0]}
        project = self._proj(project, "read")
        r = self._row(project, path)
        if not r:
            raise CommonsError(f"no such file {path}")
        sha = r["sha"]
        if version:
            v = self.s.q("SELECT sha FROM drive_versions WHERE file_id=? AND version=?", (r["id"], version))
            if not v:
                raise CommonsError(f"no version {version}")
            sha = v[0]["sha"]
        return self.b.get(sha), self._info(r)

    def read(self, path: str, project: str | None = None, *, version: int | None = None, max_tokens: int = 2000,
             offset_line: int = 0) -> dict:
        data, info = self.read_bytes(path, project, version)
        if not _is_text(info["path"], data, info.get("mime")):
            return info | {"binary": True, "bytes": len(data), "text": None,
                           "hint": "binary file: download via the dashboard or fs_read with a text file"}
        lines = data.decode().splitlines(keepends=True)
        budget, out, i = max_tokens * 4, [], offset_line
        while i < len(lines) and budget - len(lines[i]) >= 0:
            out.append(lines[i])
            budget -= len(lines[i])
            i += 1
        more = i < len(lines)
        return info | {"text": "".join(out), "lines": f"{offset_line + 1}-{i}", "total_lines": len(lines),
                       "truncated": more, "next_offset_line": i if more else None}

    def list(self, prefix: str = "/", project: str | None = None, recursive: bool = True, include_deleted: bool = False) -> dict:
        prefix = _norm(prefix)
        m, rel = self._mount_for(prefix)
        if m:
            base = check_host_path(os.path.join(m["root"], rel), base=m["root"])
            ents = [{"path": f"{prefix.rstrip('/')}/{p.name}", "dir": p.is_dir(), "size": p.stat().st_size if p.is_file() else None}
                    for p in sorted(base.iterdir())] if base.is_dir() else []
            return {"files": ents, "mount": m["name"], "read_only": bool(m["read_only"])}
        project = self._proj(project, "read")
        like = prefix.rstrip("/") + "/%"
        rows = self.s.q("SELECT * FROM drive_files WHERE project=? AND (path LIKE ? OR path=?)"
                        + ("" if include_deleted else " AND deleted_at IS NULL") + " ORDER BY path", (project, like, prefix))
        files = [self._info(r) for r in rows]
        if not recursive:
            depth = prefix.rstrip("/").count("/") + 1
            dirs = sorted({"/".join(f["path"].split("/")[:depth + 1]) for f in files if f["path"].count("/") > depth})
            files = [f for f in files if f["path"].count("/") == depth]
            return {"files": files, "dirs": dirs}
        mounts = [f"/mnt/{x['name']}" for x in self.s.q("SELECT name FROM drive_mounts")] if prefix == "/" else []
        return {"files": files, "mounts": mounts}

    # ---- edit / move / delete / restore / lock
    def edit(self, path: str, old: str, new: str, *, project: str | None = None, agent_id: str | None = None,
             expected_version: int | None = None, message: str = "") -> dict:
        data, info = self.read_bytes(path, project)
        txt = data.decode()
        n = txt.count(old)
        if n != 1:
            raise CommonsError(f"`old` text must occur exactly once (found {n}); include more context")
        return self.write(path, txt.replace(old, new), project=project, agent_id=agent_id,
                          expected_version=expected_version if expected_version is not None else info["version"],
                          message=message or "edit")

    def move(self, src: str, dst: str, project: str | None = None, agent_id: str | None = None) -> dict:
        project = self._proj(project, "write")
        src, dst = _norm(src), _norm(dst)
        r = self._row(project, src)
        if not r:
            raise CommonsError(f"no such file {src}")
        if self._row(project, dst):
            raise CommonsError(f"{dst} exists")
        self.s.x("DELETE FROM drive_files WHERE project=? AND path=? AND deleted_at IS NOT NULL", (project, dst))
        self.s.x("UPDATE drive_files SET path=?, updated_at=?, updated_by=? WHERE id=?", (dst, now(), agent_id, r["id"]))
        self.s.audit(agent_id, "drive.move", f"{project}:{src}", dst)
        return self._info(self._row(project, dst))

    def delete(self, path: str, project: str | None = None, agent_id: str | None = None) -> dict:
        project = self._proj(project, "write")
        r = self._row(project, _norm(path))
        if not r:
            raise CommonsError(f"no such file {path}")
        self.s.x("UPDATE drive_files SET deleted_at=? WHERE id=?", (now(), r["id"]))
        if r["item_id"]:
            self.s.x("UPDATE items SET superseded_by=? WHERE id=?", (r["id"], r["item_id"]))
        self.s.audit(agent_id, "drive.delete", f"{project}:{path}")
        return {"deleted": path, "restore": "fs_restore(path) brings back the last version"}

    def history(self, path: str, project: str | None = None) -> list[dict]:
        project = self._proj(project, "read")
        r = self._row(project, _norm(path), deleted=True)
        if not r:
            raise CommonsError(f"no such file {path}")
        return [{"version": v["version"], "size": v["size"], "sha": v["sha"][:16], "path": v["path"],
                 "author": self.s.label(v["author"])[1] if v["author"] else None, "message": v["message"], "at": v["created_at"]}
                for v in self.s.q("SELECT * FROM drive_versions WHERE file_id=? ORDER BY version DESC", (r["id"],))]

    def diff(self, path: str, a: int, b: int | None = None, project: str | None = None) -> str:
        x = self.read_bytes(path, project, a)[0].decode(errors="replace").splitlines(keepends=True)
        y = self.read_bytes(path, project, b)[0].decode(errors="replace").splitlines(keepends=True)
        return "".join(difflib.unified_diff(x, y, f"{path}@v{a}", f"{path}@v{b or 'latest'}"))

    def restore(self, path: str, version: int | None = None, project: str | None = None, agent_id: str | None = None) -> dict:
        p = _norm(path)
        proj = self._proj(project, "write")
        r = self._row(proj, p, deleted=True)
        if not r:
            raise CommonsError(f"no such file {path}")
        v = version or r["version"]
        sha = self.s.q("SELECT sha FROM drive_versions WHERE file_id=? AND version=?", (r["id"], v))
        if not sha:
            raise CommonsError(f"no version {v}")
        data = self.b.get(sha[0]["sha"])
        if r["deleted_at"]:
            self.s.x("UPDATE drive_files SET deleted_at=NULL WHERE id=?", (r["id"],))
            if r["item_id"]:
                self.s.x("UPDATE items SET superseded_by=NULL WHERE id=?", (r["item_id"],))
            if r["sha"] == sha[0]["sha"]:
                return self._info(self._row(proj, p)) | {"restored": v}
        return self.write(p, data, project=project, agent_id=agent_id, message=f"restore v{v}") | {"restored": v}

    def lock(self, path: str, agent_id: str, project: str | None = None, ttl: int = 600, release: bool = False) -> dict:
        proj = self._proj(project, "write")
        r = self._row(proj, _norm(path))
        if not r:
            raise CommonsError(f"no such file {path}")
        if release:
            self.s.x("UPDATE drive_files SET lock_by=NULL, lock_until=NULL WHERE id=? AND (lock_by=? OR lock_until<?)",
                     (r["id"], agent_id, now()))
            return self._info(self._row(proj, _norm(path)))
        with self.s.lock:
            c = self.s.db.execute("UPDATE drive_files SET lock_by=?, lock_until=? WHERE id=? AND (lock_by IS NULL OR lock_by=? "
                                  "OR lock_until<?)", (agent_id, now() + ttl, r["id"], agent_id, now()))
        if c.rowcount != 1:
            raise CommonsError(f"{path} is locked by {self.s.label(r['lock_by'])[1]}")
        return self._info(self._row(proj, _norm(path)))

    def link(self, file_id: str, ref: str, rel: str = "produced_by") -> None:
        self.s.x("INSERT INTO drive_links(file_id,ref,rel,created_at) VALUES(?,?,?,?) ON CONFLICT(file_id, ref, rel) DO NOTHING",
                 (file_id, ref, rel, now()))
        try:
            self.s.relate(file_id, ref, rel)
        except Exception:  # noqa: BLE001
            pass

    def artifacts(self, ref: str) -> list[dict]:
        return [self._info(r) for r in self.s.q("SELECT f.* FROM drive_files f JOIN drive_links l ON l.file_id=f.id "
                                                "WHERE l.ref=? AND f.deleted_at IS NULL", (ref,))]

    def search(self, query: str, project: str | None = None, max_tokens: int = 800, limit: int = 10) -> dict:
        project = self._proj(project, "read")
        res = self.s.search(query, project=project or None, kinds=["document"], tags=["drive"], max_tokens=max_tokens, limit=limit)
        hits = []
        for h in res.get("results", []):
            f = self.s.q("SELECT * FROM drive_files WHERE item_id=? AND deleted_at IS NULL", (h["id"],))
            if f:
                hits.append(self._info(f[0]) | {"snippet": h.get("snippet") or h.get("summary") or ""})
        like = f"%{query.lower()}%"
        for f in self.s.q("SELECT * FROM drive_files WHERE project=? AND lower(path) LIKE ? AND deleted_at IS NULL LIMIT ?",
                          (project, like, limit)):
            if not any(x["id"] == f["id"] for x in hits):
                hits.append(self._info(f) | {"snippet": "(path match)"})
        return {"results": hits[:limit], "text": "\n".join(f"{x['path']} v{x['version']} ({x['size']} B): {x['snippet'][:160]}"
                                                           for x in hits[:limit])}

    def usage(self) -> list[dict]:
        return [dict(r) for r in self.s.q("SELECT project, COUNT(*) files, COALESCE(SUM(size),0) bytes FROM drive_files "
                                          "WHERE deleted_at IS NULL GROUP BY project ORDER BY project")]

    def gc(self) -> int:
        """Delete blobs no version references (blobs are shared across files by hash)."""
        if not isinstance(self.b, LocalBlobs):
            return 0
        live = {r["sha"] for r in self.s.q("SELECT sha FROM drive_versions")}
        n = 0
        for p in self.b.root.glob("*/*"):
            if p.name not in live and not p.name.endswith(".tmp"):
                p.unlink()
                n += 1
        return n

    # ---- host mounts
    def mount_host(self, name: str, root: str, read_only: bool = True, project: str | None = None) -> dict:
        if not re.fullmatch(r"[a-zA-Z0-9_-]{1,40}", name):
            raise CommonsError("mount name: letters, digits, - and _")
        rp = check_host_path(root)
        self.s.x("INSERT OR REPLACE INTO drive_mounts(name,root,read_only,project,created_at) VALUES(?,?,?,?,?)",
                 (name, str(rp), 1 if read_only else 0, project or "", now()))
        return {"mount": f"/mnt/{name}", "root": str(rp), "read_only": read_only}

    def _host_write(self, m, rel, content) -> dict:
        if m["read_only"]:
            raise CommonsError(f"/mnt/{m['name']} is mounted read-only")
        p = check_host_path(os.path.join(m["root"], rel), base=m["root"])
        data = content.encode() if isinstance(content, str) else content
        if _is_text(str(p), data, None) and scan(data.decode()):
            data = redact(data.decode())[0].encode()
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_bytes(data)
        return {"path": f"/mnt/{m['name']}/{rel}", "size": len(data), "mount": m["name"]}

    # ---- sync adapters
    def export_dir(self, prefix: str, project: str | None, dest: Path) -> int:
        n = 0
        for f in self.list(prefix, project)["files"]:
            data, _ = self.read_bytes(f["path"], project)
            out = dest / f["path"][len(_norm(prefix).rstrip("/")):].lstrip("/")
            out.parent.mkdir(parents=True, exist_ok=True)
            out.write_bytes(data)
            n += 1
        return n

    def import_dir(self, src: Path, prefix: str, project: str | None, agent_id: str | None, message: str) -> dict:
        changed = same = 0
        for p in sorted(src.rglob("*")):
            if p.is_file() and ".git" not in p.parts:
                r = self.write(f"{_norm(prefix).rstrip('/')}/{p.relative_to(src).as_posix()}", p.read_bytes(), project=project,
                               agent_id=agent_id, message=message)
                same += bool(r.get("unchanged"))
                changed += not r.get("unchanged")
        return {"changed": changed, "unchanged": same}

    def sync_git(self, repo: str, *, direction: str = "pull", prefix: str = "/", project: str | None = None,
                 subdir: str = "", agent_id: str | None = None, commit: bool = True) -> dict:
        root = Path(repo).expanduser().resolve()
        if not (root / ".git").exists():
            raise CommonsError(f"{root} is not a git repository")
        base = root / subdir if subdir else root
        if direction == "pull":
            head = subprocess.run(["git", "-C", str(root), "rev-parse", "--short", "HEAD"], capture_output=True, text=True).stdout.strip()
            tracked = subprocess.run(["git", "-C", str(root), "ls-files", "-z", subdir or "."], capture_output=True, text=True).stdout
            tmp = Path(tempfile.mkdtemp())
            for f in filter(None, tracked.split("\x00")):
                src = root / f
                out = tmp / Path(f).relative_to(subdir) if subdir else tmp / f
                out.parent.mkdir(parents=True, exist_ok=True)
                shutil.copy2(src, out)
            r = self.import_dir(tmp, prefix, project, agent_id, f"git pull {root.name}@{head}")
            shutil.rmtree(tmp)
            return r | {"direction": "pull", "commit": head}
        n = self.export_dir(prefix, project, base)
        out = {"direction": "push", "files": n}
        if commit:
            subprocess.run(["git", "-C", str(root), "add", "-A", subdir or "."], check=True)
            r = subprocess.run(["git", "-C", str(root), "commit", "-q", "-m", f"Commons Drive sync from {prefix}"],
                               capture_output=True, text=True)
            out["committed"] = r.returncode == 0
        return out

    def sync_rclone(self, remote: str, *, direction: str = "push", prefix: str = "/", project: str | None = None,
                    agent_id: str | None = None) -> dict:
        """Google Drive / OneDrive / Dropbox / anything rclone supports (`rclone config` the remote first)."""
        exe = shutil.which("rclone")
        if not exe:
            raise CommonsError("rclone not found: install rclone and `rclone config` a remote (gdrive:, onedrive:, dropbox:)")
        tmp = Path(tempfile.mkdtemp())
        try:
            if direction == "push":
                n = self.export_dir(prefix, project, tmp)
                subprocess.run([exe, "copy", str(tmp), remote], check=True, capture_output=True, timeout=600)
                return {"direction": "push", "files": n, "remote": remote}
            subprocess.run([exe, "copy", remote, str(tmp)], check=True, capture_output=True, timeout=600)
            return self.import_dir(tmp, prefix, project, agent_id, f"rclone pull {remote}") | {"direction": "pull", "remote": remote}
        finally:
            shutil.rmtree(tmp, ignore_errors=True)
