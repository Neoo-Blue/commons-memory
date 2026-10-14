"""Read-only infrastructure snapshot for the Commons map.

One helper, `run`, is the only way out to a shell. A regex allowlist runs before
subprocess, so a write, start, stop, or edit cannot slip through a later edit
to a collector. The hub merges snapshots; this module only describes one machine.
"""
from __future__ import annotations

import argparse
import json
import re
import shlex
import subprocess
import sys
import time
from pathlib import Path
from urllib.parse import urlsplit, urlunsplit

TIMEOUT = 20

# Longer alternatives first so a short prefix cannot satisfy a longer argv.
_DOCKER_PS_FMT = r"docker ps --format '\{\{json \.\}\}'"
_DOCKER_INSPECT = r"docker inspect(?: [A-Za-z0-9][A-Za-z0-9_.-]*)+"
_SYSTEMCTL = r"systemctl list-units --type=service --state=running --no-legend --plain"
_INNER_ALT = (
    rf"{_DOCKER_PS_FMT}|docker ps|{_DOCKER_INSPECT}|{_SYSTEMCTL}"
    r"|ip -j addr|findmnt -J|cat /etc/os-release|ss -Hltnp|ss -ltn|hostname"
)
_INNER = re.compile(rf"^(?:{_INNER_ALT})$")
_HOST = re.compile(
    rf"^(?:{_INNER_ALT}"
    rf"|sudo -n {_DOCKER_PS_FMT}|sudo -n docker ps|sudo -n {_DOCKER_INSPECT}"
    r"|zpool list -H|df -PT|pvesm status|pct list|pct config [0-9]{1,12}|qm list|qm config [0-9]{1,12}"
    r"|pvesh get /[A-Za-z0-9_./-]+ --output-format json"
    r"|tailscale status --json|cloudflared tunnel list --output json"
    r")$"
)
_EXEC = re.compile(r"^pct exec ([0-9]{1,12}) -- (?P<inner>.+)$")
_ALIAS = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.:@-]{0,200}$")
_SAFE_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]*$")
_NODE = re.compile(r"^[A-Za-z0-9](?:[A-Za-z0-9-]{0,62}[A-Za-z0-9])?$")
_VMID = re.compile(r"^[0-9]{1,12}$")
_SECRET_KEY = re.compile(r"^(env|environment|env_vars|password|passwd|secret|token|api_key|apikey|credentials)$", re.I)
_SECRET_ASSIGN = re.compile(r"(?i)\b(token|password|passwd|secret|api[_-]?key)\s*[:=]\s*\S+")
_PORT_RE = re.compile(r"(?:(?P<ip>\[[^\]]+\]|[\d.]+|\*|::):)?(?P<hp>\d+)->(?P<cp>\d+)/tcp")
_QM_ROW = re.compile(
    r"^\s*(?P<vmid>\d+)\s+(?P<name>\S+)\s+(?P<status>\S+)\s+(?P<mem>\d+)"
)
_NET_FS = {"nfs", "nfs4", "cifs", "smb3", "fuse.sshfs", "fuse.rclone", "ceph", "glusterfs", "fuse.mergerfs"}
_SKIP_FS = {
    "proc", "sysfs", "devtmpfs", "tmpfs", "devpts", "cgroup", "cgroup2", "overlay", "squashfs",
    "securityfs", "pstore", "autofs", "mqueue", "hugetlbfs", "debugfs", "tracefs", "fusectl",
    "configfs", "ramfs", "efivarfs", "bpf", "nsfs", "rpc_pipefs", "binfmt_misc",
}
# Base Debian and Proxmox units are not applications. Keep names outside this set
# (altmount, plex-guard). pve-firewall and ssh belong to the install, so they drop.
_BASE_EXACT = {
    "cron", "crond", "atd", "dbus", "ssh", "sshd", "rsyslog", "networking", "ifup",
    "systemd-journald", "systemd-logind", "systemd-timesyncd", "systemd-udevd",
    "systemd-networkd", "systemd-resolved", "polkit", "udisks2", "accounts-daemon",
    "avahi-daemon", "bluetooth", "wpa_supplicant", "modemmanager", "packagekit",
    "unattended-upgrades", "fwupd", "upower", "smartmontools", "irqbalance",
    "haveged", "rngd", "console-setup", "keyboard-setup", "setvtrgb", "apparmor",
    "rpcbind", "postfix", "chrony", "chronyd", "ntp", "ntpsec", "corosync",
    "ksmtuned", "watchdog-mux", "rrdcached", "lxcfs", "lxc-monitord", "qmeventd",
    "spiceproxy", "pvedaemon", "pveproxy", "pvestatd", "pvescheduler", "pvefw-logger",
    "pve-cluster", "pve-firewall", "pve-ha-crm", "pve-ha-lrm", "pve-lxc-syscalld",
    "pvebanner", "pvenetcommit", "pve-daily-update", "proxmox-firewall", "zfs-zed", "zfs-import-cache",
    "zfs-mount", "zfs-share", "zfs-volume-wait",
}
_BASE_PREFIX = (
    "systemd-", "user@", "user-", "getty@", "serial-getty@", "container-getty@",
    "dbus-", "apt-", "fstrim", "lvm2-", "nfs-", "rpc-", "zfs-", "pve",
)
_LOCKS = {
    "backup", "migrate", "rollback", "snapshot", "create", "destroy", "mounted",
    "disk", "fstrim", "clone", "unknown", "suspending", "suspended",
}
_DISK_KEY = re.compile(r"^(scsi|virtio|sata|ide|efidisk|tpmstate|unused)\d+$")
_MP_KEY = re.compile(r"^mp\d+$")
_CLOUDFLARED_PATHS = (
    Path.home() / ".cloudflared" / "config.yml",
    Path.home() / ".cloudflared" / "config.yaml",
    Path("/etc/cloudflared/config.yml"),
    Path("/usr/local/etc/cloudflared/config.yml"),
)


class Refused(RuntimeError):
    """The argv is not read-only allowlisted. Nothing was executed."""


class CmdError(RuntimeError):
    """An allowlisted command ran and failed, timed out, or the binary is missing."""


def _allow(cmd: list[str], via: str | None) -> None:
    """Reject anything that is not a known read before a process exists."""
    if via is not None and (not isinstance(via, str) or _ALIAS.fullmatch(via) is None):
        raise Refused("ssh alias is not a plain host or config alias")
    if not isinstance(cmd, list) or not cmd:
        raise Refused("command must be a non-empty argv list")
    for arg in cmd:
        if not isinstance(arg, str) or not arg or "\x00" in arg or "\n" in arg or "\r" in arg or ".." in arg:
            raise Refused("refusing empty, control, or '..' argument")
    canon = shlex.join(cmd)
    if cmd[0] == "pct" and len(cmd) > 1 and cmd[1] == "exec":
        matched = _EXEC.fullmatch(canon)
        if matched is None or _INNER.fullmatch(matched.group("inner")) is None:
            raise Refused(f"not an allowlisted read-only command: {canon}")
        return
    if _HOST.fullmatch(canon) is None:
        raise Refused(f"not an allowlisted read-only command: {canon}")


def _argv(cmd: list[str], via: str | None) -> list[str]:
    if via is None:
        return list(cmd)
    # One quoted remote string. The local ssh argv is not a shell, and the remote
    # shell only sees what shlex.join produced from the already-allowlisted list.
    return ["ssh", "-o", "BatchMode=yes", "-o", "ConnectTimeout=10", via, shlex.join(cmd)]


def run(cmd: list[str], via: str | None = None) -> str:
    """Run one allowlisted read locally (`via` None) or through BatchMode ssh.

    The allowlist raises before subprocess. Timeout is 20s. stdin is closed so a
    command cannot sit waiting for a password or a confirmation.
    """
    _allow(cmd, via)
    argv = _argv(cmd, via)
    try:
        proc = subprocess.run(
            argv, capture_output=True, text=True, timeout=TIMEOUT,
            errors="replace", stdin=subprocess.DEVNULL,
        )
    except subprocess.TimeoutExpired as exc:
        raise CmdError(f"timeout after {TIMEOUT}s: {shlex.join(cmd)}") from exc
    except FileNotFoundError as exc:
        raise CmdError(f"not found: {argv[0]}") from exc
    if proc.returncode != 0:
        err = _redact_text((proc.stderr or proc.stdout or "").strip().replace("\n", " "))
        raise CmdError(f"exit {proc.returncode}: {err[:300]}")
    return proc.stdout


def _redact_text(text: str) -> str:
    return _SECRET_ASSIGN.sub(lambda m: m.group(1) + "=[redacted]", text)


def _public_url(url: str) -> str:
    """Drop userinfo and query. A service URL is an address, not a place to keep a password."""
    try:
        parts = urlsplit(url)
    except ValueError:
        return url
    if parts.scheme not in ("http", "https") or not parts.hostname:
        return url
    host = f"[{parts.hostname}]" if ":" in parts.hostname else parts.hostname
    if parts.port:
        host = f"{host}:{parts.port}"
    return urlunsplit((parts.scheme, host, parts.path or "", "", ""))


def _scrub(obj) -> None:
    """Drop credential-shaped keys and assignment secrets. Container inspect Env never survives."""
    if isinstance(obj, dict):
        for key in list(obj):
            if _SECRET_KEY.match(str(key)):
                del obj[key]
            elif isinstance(obj[key], str):
                obj[key] = _redact_text(obj[key])
            else:
                _scrub(obj[key])
    elif isinstance(obj, list):
        for i, item in enumerate(obj):
            if isinstance(item, str):
                obj[i] = _redact_text(item)
            else:
                _scrub(item)


def _binary_missing(text: str) -> bool:
    low = text.lower()
    return (
        low.startswith("not found:")
        or "no such file or directory" in low
        or "command not found" in low
        or "executable file not found" in low
    )


def _loads(text: str):
    raw = (text or "").strip()
    if not raw:
        raise json.JSONDecodeError("empty", raw, 0)
    try:
        return json.loads(raw)
    except json.JSONDecodeError:
        starts = [n for n in (raw.find("{"), raw.find("[")) if n >= 0]
        if not starts:
            raise
        return json.loads(raw[min(starts):])


def _uniq(items: list[str]) -> list[str]:
    seen: set[str] = set()
    out = []
    for item in items:
        if item and item not in seen:
            seen.add(item)
            out.append(item)
    return out


def _as_int(value):
    try:
        return int(str(value).strip())
    except (TypeError, ValueError):
        return None


def _flag(value) -> bool:
    return str(value or "").strip().lower() in {"1", "true", "yes", "on"}


class Snap:
    """One collector's entities and relations, deduped by key."""

    def __init__(self, collector: str, source: str):
        self.collector = collector
        self.source = source
        self.at = time.time()
        self.entities: list[dict] = []
        self.relations: list[dict] = []
        self.notes: list[str] = []
        self._by: dict[str, dict] = {}
        self._rels: set[str] = set()

    def note(self, msg: str) -> None:
        text = _redact_text(" ".join(str(msg).split()))[:400]
        if text and text not in self.notes:
            self.notes.append(text)

    def ent(self, key: str, kind: str, name: str, attrs: dict | None = None) -> dict:
        attrs = {k: v for k, v in (attrs or {}).items() if v is not None and v != ""}
        cur = self._by.get(key)
        if cur is None:
            cur = {"key": key, "kind": kind, "name": name, "attrs": attrs}
            self._by[key] = cur
            self.entities.append(cur)
            return cur
        for k, v in attrs.items():
            if k not in cur["attrs"] or cur["attrs"][k] in (None, "", [], {}):
                cur["attrs"][k] = v
        if cur["name"] in ("", key) and name:
            cur["name"] = name
        return cur

    def get(self, key: str) -> dict | None:
        return self._by.get(key)

    def rel(self, src: str, rel: str, dst: str, attrs: dict | None = None) -> None:
        attrs = attrs or {}
        sig = json.dumps([src, rel, dst, attrs], sort_keys=True, default=str)
        if sig in self._rels:
            return
        self._rels.add(sig)
        self.relations.append({"src": src, "rel": rel, "dst": dst, "attrs": attrs})

    def as_dict(self) -> dict:
        _scrub(self.entities)
        _scrub(self.relations)
        return {
            "collector": self.collector,
            "source": self.source,
            "at": self.at,
            "entities": self.entities,
            "relations": self.relations,
            "notes": self.notes,
        }


def _try(snap: Snap, cmd: list[str], via: str | None, label: str | None = None) -> str | None:
    try:
        return run(cmd, via)
    except Refused as exc:
        snap.note(f"refused {label or shlex.join(cmd)}: {exc}")
    except CmdError as exc:
        snap.note(f"{label or shlex.join(cmd)}: {exc}")
    return None


def _usable_ip(value: str) -> str:
    """An address worth recording. Loopback and link-local are not how anything else reaches the guest."""
    ip = str(value or "").split("/", 1)[0].split("%", 1)[0].strip()
    if not ip or ip in {"::1", "0.0.0.0", "::"}:
        return ""
    if ip.startswith("127.") or ip.startswith("fe80") or ip.startswith("169.254."):
        return ""
    return ip


def _docker_bridge(ip: str) -> bool:
    """172.16.0.0/12 is where Docker's default pools sit, so it loses to a LAN address."""
    parts = ip.split(".")
    if len(parts) != 4 or parts[0] != "172":
        return False
    try:
        second = int(parts[1])
    except ValueError:
        return False
    return 16 <= second <= 31


def _parse_ips(text: str) -> list[str]:
    try:
        data = _loads(text)
    except json.JSONDecodeError:
        return []
    if isinstance(data, dict):
        data = data.get("addr") or data.get("interfaces") or []
    out = []
    for iface in data if isinstance(data, list) else []:
        if not isinstance(iface, dict) or iface.get("ifname") in ("lo", "lo0"):
            continue
        for addr in iface.get("addr_info") or []:
            if not isinstance(addr, dict):
                continue
            local = _usable_ip(str(addr.get("local") or ""))
            fam = str(addr.get("family") or "")
            if fam not in ("inet", "inet6", "2", "10", "AF_INET", "AF_INET6"):
                continue
            if local:
                out.append(local)
    return _uniq(out)


def _primary(ips: list[str]) -> str | None:
    v4 = [ip for ip in ips if "." in ip and ":" not in ip]

    def rank(ip: str) -> int:
        if _docker_bridge(ip):
            return 2
        if ip.startswith(("192.168.", "10.")):
            return 0
        return 1

    v4.sort(key=rank)
    if v4:
        return v4[0]
    return ips[0] if ips else None


def _os_pretty(text: str) -> str:
    vals = {}
    for line in text.splitlines():
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        vals[key.strip()] = value.strip().strip('"').strip("'")
    return vals.get("PRETTY_NAME") or vals.get("NAME") or ""


def _parse_kv(text: str) -> dict[str, str]:
    out = {}
    for line in text.splitlines():
        if ":" not in line:
            continue
        key, value = line.split(":", 1)
        key = key.strip()
        if key and " " not in key:
            out[key] = value.strip()
    return out


def _parse_opts(spec: str) -> tuple[str, dict[str, str]]:
    parts = [p for p in spec.split(",") if p != ""]
    if not parts:
        return "", {}
    opts = {}
    for part in parts[1:]:
        if "=" in part:
            key, value = part.split("=", 1)
            opts[key] = value
    return parts[0], opts


def _parse_pct(text: str) -> list[dict]:
    rows = []
    for line in text.splitlines():
        parts = line.split()
        if len(parts) < 3 or not parts[0].isdigit():
            continue
        vmid, status = parts[0], parts[1]
        rest = parts[2:]
        lock = ""
        if rest[0] in _LOCKS and len(rest) >= 2:
            lock, rest = rest[0], rest[1:]
        rows.append({"vmid": vmid, "status": status, "lock": lock, "name": " ".join(rest)})
    return rows


def _parse_qm(text: str) -> list[dict]:
    rows = []
    for line in text.splitlines():
        matched = _QM_ROW.match(line)
        if matched and matched.group("vmid") != "VMID":
            rows.append(matched.groupdict())
    return rows


def _parse_pvesm(text: str) -> list[dict]:
    rows = []
    pat = re.compile(
        r"^(\S+)\s+(\S+)\s+(active|disabled|inactive|unknown|online|offline)\s+(\S+)\s+(\S+)\s+(\S+)\s+(\S+)",
        re.I,
    )
    for line in text.splitlines():
        matched = pat.match(line.strip())
        if not matched or matched.group(1).lower() == "name":
            continue
        rows.append({
            "name": matched.group(1),
            "type": matched.group(2),
            "status": matched.group(3),
            "total": matched.group(4),
            "used": matched.group(5),
            "avail": matched.group(6),
            "percent": matched.group(7),
        })
    return rows


def _parse_zpool(text: str) -> list[dict]:
    rows = []
    for line in text.splitlines():
        parts = line.split()
        if len(parts) < 4 or parts[0].lower() == "name":
            continue
        rows.append({
            "name": parts[0],
            "size": parts[1],
            "alloc": parts[2],
            "free": parts[3],
            "cap": parts[7] if len(parts) > 7 else "",
            "health": parts[9] if len(parts) > 9 else "",
        })
    return rows


def _walk_fs(node, acc: list) -> None:
    if isinstance(node, dict):
        acc.append(node)
        for child in node.get("children") or []:
            _walk_fs(child, acc)
    elif isinstance(node, list):
        for item in node:
            _walk_fs(item, acc)


def _keep_mount(fstype: str, target: str) -> bool:
    fs = (fstype or "").lower()
    if fs in _SKIP_FS or not target.startswith("/"):
        return False
    if fs in _NET_FS:
        return True
    return target.startswith("/mnt/") and fs not in ("", "ext4", "ext3", "xfs", "btrfs", "zfs")


def _parse_findmnt(text: str) -> list[dict]:
    try:
        data = _loads(text)
    except json.JSONDecodeError:
        return []
    nodes: list = []
    if isinstance(data, dict):
        _walk_fs(data.get("filesystems") or data, nodes)
    else:
        _walk_fs(data, nodes)
    out = []
    for node in nodes:
        target = str(node.get("target") or "")
        fstype = str(node.get("fstype") or "")
        if _keep_mount(fstype, target):
            out.append({
                "target": target,
                "source": str(node.get("source") or ""),
                "fstype": fstype,
                "options": str(node.get("options") or ""),
            })
    return out


def _parse_df(text: str) -> list[dict]:
    rows = []
    for line in text.splitlines()[1:]:
        parts = line.split()
        if len(parts) < 7:
            continue
        rows.append({"source": parts[0], "fstype": parts[1], "target": " ".join(parts[6:])})
    return rows


def _parse_units(text: str) -> list[str]:
    names = []
    for line in text.splitlines():
        parts = line.strip().lstrip("●• ").split()
        if not parts:
            continue
        name = parts[0]
        if name.endswith(".service"):
            name = name[:-8]
        if name and not name.startswith("-"):
            names.append(name)
    return names


def _notable(name: str) -> bool:
    head = name.split("@", 1)[0]
    if name in _BASE_EXACT or head in _BASE_EXACT:
        return False
    low = name.lower()
    return not any(low.startswith(prefix) for prefix in _BASE_PREFIX)


def _parse_ss(text: str) -> list[dict]:
    rows = []
    for line in text.splitlines():
        parts = line.split()
        if not parts:
            continue
        head = parts[0].lower()
        if head in ("state", "netid"):
            continue
        if head in ("listen", "unconn"):
            local = parts[3] if len(parts) > 3 else ""
        elif head in ("tcp", "tcp6", "udp"):
            local = parts[4] if len(parts) > 4 else ""
        else:
            continue
        addr, port = _split_hostport(local)
        if port is None:
            continue
        rows.append({"addr": addr, "port": port})
    return rows


def _split_hostport(value: str) -> tuple[str, int | None]:
    value = value.strip()
    if value.startswith("["):
        matched = re.match(r"^\[(.*)\]:(\d+)$", value)
        if not matched:
            return value, None
        return matched.group(1), int(matched.group(2))
    if ":" not in value:
        return value, None
    addr, _, port = value.rpartition(":")
    if not port.isdigit():
        return value, None
    return addr, int(port)


def _parse_ps(text: str) -> list[dict]:
    raw = (text or "").strip()
    if not raw:
        return []
    rows = []
    if raw[0] in "[{":
        try:
            data = _loads(raw)
        except json.JSONDecodeError:
            data = None
        if isinstance(data, list):
            rows = [r for r in data if isinstance(r, dict)]
        elif isinstance(data, dict) and ("ID" in data or "Names" in data or "Id" in data):
            rows = [data]
    if not rows:
        for line in raw.splitlines():
            line = line.strip()
            if not line or not line.startswith("{"):
                continue
            try:
                item = json.loads(line)
            except json.JSONDecodeError:
                continue
            if isinstance(item, dict):
                rows.append(item)
    out = []
    for row in rows:
        name = str(row.get("Names") or row.get("Name") or "").split(",")[0].strip().lstrip("/")
        out.append({
            "id": str(row.get("ID") or row.get("Id") or ""),
            "name": name,
            "image": str(row.get("Image") or ""),
            "status": str(row.get("State") or row.get("Status") or ""),
            "ports": str(row.get("Ports") or ""),
        })
    return out


def _binds(text_binds) -> list[dict]:
    mounts = []
    for bind in text_binds or []:
        if not isinstance(bind, str) or ":" not in bind:
            continue
        parts = bind.split(":")
        mode = ""
        if len(parts) >= 3 and ("ro" in parts[-1].split(",") or parts[-1] in ("ro", "rw")):
            mode = parts[-1]
            parts = parts[:-1]
        if len(parts) < 2:
            continue
        dest = parts[-1]
        source = ":".join(parts[:-1])
        if source.startswith("/") and dest.startswith("/"):
            mounts.append({"source": source, "dest": dest, "read_only": "ro" in mode.split(",")})
    return mounts


def _extract_inspect(obj: dict) -> dict:
    """Fields the map needs. Env is never copied, even to discard later."""
    cfg = obj.get("Config") if isinstance(obj.get("Config"), dict) else {}
    state = obj.get("State") if isinstance(obj.get("State"), dict) else {}
    net = obj.get("NetworkSettings") if isinstance(obj.get("NetworkSettings"), dict) else {}
    host = obj.get("HostConfig") if isinstance(obj.get("HostConfig"), dict) else {}
    mounts = []
    for mount in obj.get("Mounts") or []:
        if not isinstance(mount, dict) or mount.get("Type") not in ("bind", None):
            continue
        if mount.get("Type") != "bind":
            continue
        source, dest = mount.get("Source") or "", mount.get("Destination") or ""
        if not source or not dest:
            continue
        mode = str(mount.get("Mode") or "")
        mounts.append({
            "source": source,
            "dest": dest,
            "read_only": mount.get("RW") is False or "ro" in mode.split(","),
        })
    if not mounts:
        mounts = _binds(host.get("Binds"))
    ports = net.get("Ports")
    if not ports and isinstance(host.get("PortBindings"), dict):
        ports = host.get("PortBindings")
    image = cfg.get("Image") or ""
    if image and not str(image).startswith("sha256:"):
        pass
    elif obj.get("Config") and isinstance(cfg.get("Image"), str):
        image = cfg.get("Image") or ""
    return {
        "name": str(obj.get("Name") or "").lstrip("/"),
        "id": str(obj.get("Id") or ""),
        "image": str(image or ""),
        "status": str(state.get("Status") or ""),
        "mounts": mounts,
        "ports": ports if isinstance(ports, dict) else {},
    }


def _parse_inspect(text: str) -> list[dict]:
    try:
        data = _loads(text)
    except json.JSONDecodeError:
        return []
    if isinstance(data, dict):
        data = [data]
    if not isinstance(data, list):
        return []
    return [_extract_inspect(item) for item in data if isinstance(item, dict)]


def _published(ports, ps_ports: str, fallback_ip: str | None) -> list[tuple[str, int, int]]:
    found = []
    if isinstance(ports, dict):
        for cport, binds in ports.items():
            if not str(cport).endswith("/tcp"):
                continue
            cnum = _as_int(str(cport).split("/", 1)[0])
            for bind in binds or []:
                if not isinstance(bind, dict):
                    continue
                host_port = _as_int(bind.get("HostPort"))
                if host_port is None:
                    continue
                hip = str(bind.get("HostIp") or "")
                if hip in ("", "0.0.0.0", "::", "*"):
                    ip = fallback_ip
                elif hip.startswith("::ffff:"):
                    ip = hip.rsplit(":", 1)[-1]
                else:
                    ip = hip
                if ip and cnum is not None:
                    found.append((ip, host_port, cnum))
    if found:
        return _dedup_ports(found)
    for matched in _PORT_RE.finditer(ps_ports or ""):
        hip = matched.group("ip") or ""
        hip = hip.strip("[]")
        ip = fallback_ip if hip in ("", "0.0.0.0", "::", "*") else hip
        if ip:
            found.append((ip, int(matched.group("hp")), int(matched.group("cp"))))
    return _dedup_ports(found)


def _dedup_ports(rows: list[tuple[str, int, int]]) -> list[tuple[str, int, int]]:
    seen = set()
    out = []
    for row in rows:
        if row in seen:
            continue
        seen.add(row)
        out.append(row)
    return out


def _endpoint(ip: str, port: int) -> tuple[str, str]:
    host = f"[{ip}]" if ":" in ip else ip
    url = f"http://{host}:{port}"
    return f"url/{url}", url


def _safe_name(name: str, fallback: str) -> str:
    name = (name or "").strip().lstrip("/")
    if _SAFE_ID.fullmatch(name or ""):
        return name
    cleaned = re.sub(r"[^A-Za-z0-9_.-]", "-", name).strip("-")
    return cleaned or fallback


def _mount_key(alias: str, path: str) -> str:
    rel = path.strip().lstrip("/")
    rel = re.sub(r"[^A-Za-z0-9_./+-]", "-", rel)
    return f"{alias}/mount/{rel}"


def _storage_key(alias: str, name: str) -> str:
    return f"{alias}/storage/{name}"


def _ingest_docker(snap: Snap, ps_text: str, inspect_text: str | None, *, prefix: str,
                   parent_key: str, fallback_ip: str | None, mount_dst) -> None:
    extracted = _parse_inspect(inspect_text or "")
    by = {}
    for item in extracted:
        if item["id"]:
            by[item["id"]] = item
            by[item["id"][:12]] = item
        if item["name"]:
            by[item["name"]] = item
    for row in _parse_ps(ps_text):
        info = by.get(row["id"]) or by.get(row["id"][:12]) or by.get(row["name"]) or {}
        cid = (info.get("id") or row["id"] or "container")[:12]
        name = _safe_name(info.get("name") or row["name"], cid or "container")
        key = f"{prefix}/{name}"
        if snap.get(key) is not None:
            key = f"{prefix}/{name}-{cid}"
        image = info.get("image") or row["image"]
        status = info.get("status") or row["status"]
        ports = _published(info.get("ports"), row["ports"], fallback_ip)
        if not ports and fallback_ip is None and (row["ports"] or info.get("ports")):
            snap.note(f"{name}: published ports but no address to attach them to")
        snap.ent(key, "service", name, {
            "image": image,
            "status": status,
            "id": cid,
            "ports": [f"{hp}->{cp}/tcp" for _ip, hp, cp in ports],
        })
        snap.rel(key, "runs_on", parent_key, {})
        for ip, host_port, container_port in ports:
            ekey, url = _endpoint(ip, host_port)
            snap.ent(ekey, "endpoint", url, {"ip": ip, "port": host_port, "proto": "tcp"})
            snap.rel(key, "reachable_at", ekey, {"port": host_port, "container_port": container_port, "proto": "tcp"})
        for mount in info.get("mounts") or []:
            dst, extra = mount_dst(mount["source"])
            attrs = {
                "guest_path": mount["source"],
                "container_path": mount["dest"],
                "read_only": bool(mount["read_only"]),
            }
            attrs.update(extra)
            snap.rel(key, "mounts", dst, attrs)


def _run_docker(snap: Snap, via: str | None, args: list[str]) -> str | None:
    """Plain docker, then `sudo -n`. `-n` fails closed instead of asking for a password."""
    errors = []
    where = via or "local"
    for prefix in ([], ["sudo", "-n"]):
        try:
            return run([*prefix, "docker", *args], via)
        except CmdError as exc:
            errors.append(str(exc))
            low = errors[-1].lower()
            if "password" in low or "askpass" in low or "a terminal is required" in low:
                snap.note(f"docker on {where} would prompt for a sudo password; skipped")
                return None
        except Refused as exc:
            snap.note(f"refused docker on {where}: {exc}")
            return None
    if errors and all(_binary_missing(err) for err in errors):
        snap.note(f"no docker on {where}")
    elif errors:
        snap.note(f"docker on {where}: {errors[-1][:240]}")
    return None


def _ids_from_ps(text: str) -> list[str]:
    ids = []
    for row in _parse_ps(text):
        cid = row["id"]
        if cid and _SAFE_ID.fullmatch(cid):
            ids.append(cid)
        elif row["name"] and _SAFE_ID.fullmatch(row["name"]):
            ids.append(row["name"])
    return _uniq(ids)


def _inspect_text(snap: Snap, via: str | None, ids: list[str], *, guest: str | None = None) -> str | None:
    chunks = []
    for start in range(0, len(ids), 30):
        part = ids[start:start + 30]
        if guest is None:
            text = _run_docker(snap, via, ["inspect", *part])
        else:
            text = _try(snap, ["pct", "exec", guest, "--", "docker", "inspect", *part], via, f"lxc {guest} docker inspect")
        if text:
            chunks.append(text)
    if not chunks:
        return None
    if len(chunks) == 1:
        return chunks[0]
    merged = []
    for chunk in chunks:
        try:
            data = _loads(chunk)
        except json.JSONDecodeError:
            continue
        if isinstance(data, list):
            merged.extend(data)
        elif isinstance(data, dict):
            merged.append(data)
    return json.dumps(merged)


def _net_ips(spec: str) -> list[str]:
    _source, opts = _parse_opts(spec)
    # net0 is all key=value; ipconfig0 is too. Treat a missing '=' first field as empty.
    if "=" in spec.split(",", 1)[0]:
        opts = {}
        for part in spec.split(","):
            if "=" in part:
                key, value = part.split("=", 1)
                opts[key] = value
    ips = []
    for key in ("ip", "ip6"):
        value = opts.get(key) or ""
        ip = _usable_ip(value)
        if ip and value not in ("dhcp", "auto", "manual"):
            ips.append(ip)
    return ips


def _iface_ips(text: str) -> list[str]:
    try:
        data = _loads(text)
    except json.JSONDecodeError:
        return []
    if isinstance(data, dict):
        data = data.get("data") or data.get("result") or data.get("interfaces") or []
    ips = []
    rows = data if isinstance(data, list) else []
    if isinstance(data, dict):
        rows = list(data.values())
    for row in rows:
        if not isinstance(row, dict):
            continue
        for key in ("inet", "inet6", "ip", "ip6"):
            value = row.get(key)
            ip = _usable_ip(value) if isinstance(value, str) and value not in ("dhcp", "auto", "manual") else ""
            if ip:
                ips.append(ip)
        for addr in row.get("ip-addresses") or []:
            if isinstance(addr, dict):
                ip = _usable_ip(str(addr.get("ip-address") or ""))
                if ip:
                    ips.append(ip)
    return _uniq(ips)


def _vol(spec: str) -> tuple[str, str] | None:
    source = spec.split(",", 1)[0]
    if source.startswith("/") or ":" not in source:
        return None
    storage, volume = source.split(":", 1)
    if _SAFE_ID.fullmatch(storage or ""):
        return storage, volume
    return None


def _pick_node(text: str, hostname: str) -> str:
    names = []
    try:
        data = _loads(text)
    except json.JSONDecodeError:
        data = []
    if isinstance(data, dict):
        data = data.get("data") or []
    for row in data if isinstance(data, list) else []:
        if isinstance(row, dict):
            name = row.get("node") or row.get("name")
            if isinstance(name, str):
                names.append(name)
    short = hostname.split(".")[0]
    for candidate in (short, hostname):
        if candidate in names and _NODE.fullmatch(candidate):
            return candidate
    if len(names) == 1 and _NODE.fullmatch(names[0]):
        return names[0]
    if _NODE.fullmatch(short):
        return short
    return ""


def _add_host_mounts(snap: Snap, alias: str, findmnt_text: str | None, df_text: str | None) -> None:
    seen = set()
    for row in _parse_findmnt(findmnt_text or ""):
        key = _mount_key(alias, row["target"])
        seen.add(row["target"])
        snap.ent(key, "storage", row["target"], {
            "source": row["source"], "fstype": row["fstype"], "options": row["options"], "target": row["target"],
        })
    for row in _parse_df(df_text or ""):
        if row["fstype"].lower() not in _NET_FS or row["target"] in seen:
            continue
        key = _mount_key(alias, row["target"])
        snap.ent(key, "storage", row["target"], {
            "source": row["source"], "fstype": row["fstype"], "target": row["target"],
        })


def _guest_mounts(snap: Snap, alias: str, guest_key: str, cfg: dict[str, str]) -> dict[str, str]:
    """mpN mounts the guest onto a host path or a pvesm storage. Returns guest_path -> dst key."""
    paths = {}
    for key, value in cfg.items():
        if not _MP_KEY.fullmatch(key):
            continue
        source, opts = _parse_opts(value)
        guest_path = opts.get("mp") or ""
        read_only = _flag(opts.get("ro"))
        if source.startswith("/"):
            dst = _mount_key(alias, source)
            snap.ent(dst, "storage", source, {"target": source})
            host_path = source
        else:
            vol = _vol(value)
            if vol is None:
                snap.note(f"{guest_key} {key}: unparsed mount {source}")
                continue
            dst = _storage_key(alias, vol[0])
            snap.ent(dst, "storage", vol[0], {})
            host_path = source
        snap.rel(guest_key, "mounts", dst, {
            "guest_path": guest_path, "host_path": host_path, "read_only": read_only,
        })
        if guest_path:
            paths[guest_path] = dst
    return paths


def _root_disks(snap: Snap, alias: str, guest_key: str, cfg: dict[str, str]) -> None:
    items = []
    if "rootfs" in cfg:
        items.append(("rootfs", cfg["rootfs"]))
    items.extend((key, cfg[key]) for key in cfg if _DISK_KEY.fullmatch(key))
    for device, spec in items:
        vol = _vol(spec)
        if vol is None:
            continue
        _source, opts = _parse_opts(spec)
        dst = _storage_key(alias, vol[0])
        snap.ent(dst, "storage", vol[0], {})
        snap.rel(guest_key, "stores_in", dst, {
            "device": device, "volume": vol[1], "size": opts.get("size") or "",
        })


def collect_proxmox(ssh_alias: str = "pve") -> dict:
    """Snapshot one Proxmox node: machine, guests, mounts, and docker inside running LXCs.

    `pct exec` runs only against guests whose list status is running, and only
    with the inner reads the allowlist already permits.
    """
    alias = ssh_alias
    snap = Snap("proxmox", alias)
    if _ALIAS.fullmatch(alias or "") is None:
        snap.note(f"refusing ssh alias {alias!r}")
        return snap.as_dict()
    host = (_try(snap, ["hostname"], alias) or "").strip().split()[:1]
    hostname = host[0] if host else alias
    ips = _parse_ips(_try(snap, ["ip", "-j", "addr"], alias) or "")
    os_name = _os_pretty(_try(snap, ["cat", "/etc/os-release"], alias) or "")
    nodes = _try(snap, ["pvesh", "get", "/nodes", "--output-format", "json"], alias)
    node = _pick_node(nodes or "", hostname)
    status = {}
    if node:
        raw = _try(snap, ["pvesh", "get", f"/nodes/{node}/status", "--output-format", "json"], alias)
        if raw:
            try:
                status = _loads(raw)
            except json.JSONDecodeError:
                snap.note(f"pvesh status for {node} was not json")
                status = {}
    if isinstance(status, dict) and isinstance(status.get("data"), dict) and "memory" not in status:
        status = status["data"]
    if not isinstance(status, dict):
        status = {}
    cpuinfo = status.get("cpuinfo") if isinstance(status.get("cpuinfo"), dict) else {}
    mem = status.get("memory") if isinstance(status.get("memory"), dict) else {}
    machine = snap.ent(alias, "machine", hostname, {
        "hostname": hostname,
        "node": node,
        "ips": ips,
        "os": os_name,
        "cpu": status.get("cpu"),
        "cpu_count": cpuinfo.get("cpus"),
        "cpu_model": cpuinfo.get("model"),
        "cores": cpuinfo.get("cores"),
        "sockets": cpuinfo.get("sockets"),
        "memory_total": mem.get("total"),
        "memory_used": mem.get("used"),
        "pveversion": status.get("pveversion"),
        "uptime": status.get("uptime"),
    })
    for pool in _parse_zpool(_try(snap, ["zpool", "list", "-H"], alias) or ""):
        snap.ent(f"{alias}/zpool/{pool['name']}", "storage", pool["name"], {
            "kind": "zpool", "size": pool["size"], "alloc": pool["alloc"], "free": pool["free"],
            "cap": pool["cap"], "health": pool["health"],
        })
    for store in _parse_pvesm(_try(snap, ["pvesm", "status"], alias) or ""):
        snap.ent(_storage_key(alias, store["name"]), "storage", store["name"], {
            "type": store["type"], "status": store["status"], "total": store["total"],
            "used": store["used"], "avail": store["avail"], "percent": store["percent"],
        })
    _add_host_mounts(
        snap, alias,
        _try(snap, ["findmnt", "-J"], alias),
        _try(snap, ["df", "-PT"], alias),
    )
    for unit in _parse_units(_try(snap, [
        "systemctl", "list-units", "--type=service", "--state=running", "--no-legend", "--plain",
    ], alias) or ""):
        if not _notable(unit):
            continue
        key = f"{alias}/service/{_safe_name(unit, 'service')}"
        snap.ent(key, "service", unit, {"systemd": True})
        snap.rel(key, "runs_on", machine["key"], {})
    _host_docker(snap, alias, machine["key"], _primary(ips))
    for guest in _parse_pct(_try(snap, ["pct", "list"], alias) or ""):
        _one_guest(snap, alias, node, guest, "lxc")
    for guest in _parse_qm(_try(snap, ["qm", "list"], alias) or ""):
        _one_guest(snap, alias, node, guest, "qemu")
    return snap.as_dict()


def _host_docker(snap: Snap, alias: str, machine_key: str, fallback_ip: str | None) -> None:
    ps = _run_docker(snap, alias, ["ps", "--format", "{{json .}}"])
    if not ps or not _parse_ps(ps):
        return
    ids = _ids_from_ps(ps)
    inspected = _inspect_text(snap, alias, ids) if ids else None

    def mount_dst(source: str):
        key = _mount_key(alias, source)
        if snap.get(key) is not None:
            return key, {"host_path": source}
        return machine_key, {"host_path": source}

    _ingest_docker(
        snap, ps, inspected, prefix=f"{alias}/docker", parent_key=machine_key,
        fallback_ip=fallback_ip, mount_dst=mount_dst,
    )


def _one_guest(snap: Snap, alias: str, node: str, row: dict, kind: str) -> None:
    vmid = str(row.get("vmid") or "")
    if _VMID.fullmatch(vmid) is None:
        snap.note(f"skipping guest with odd vmid {vmid!r}")
        return
    status = str(row.get("status") or "")
    cfg_text = _try(snap, ["pct" if kind == "lxc" else "qm", "config", vmid], alias)
    cfg = _parse_kv(cfg_text or "")
    name = cfg.get("hostname") or cfg.get("name") or row.get("name") or f"{kind}-{vmid}"
    ips = []
    for key, value in cfg.items():
        if key.startswith("net") or key.startswith("ipconfig"):
            ips.extend(_net_ips(value))
    if kind == "lxc" and status.lower() == "running" and node:
        raw = _try(
            snap,
            ["pvesh", "get", f"/nodes/{node}/lxc/{vmid}/interfaces", "--output-format", "json"],
            alias,
            f"lxc {vmid} interfaces",
        )
        if raw:
            ips = _uniq(_iface_ips(raw) + ips)
    guest_key = f"{alias}/{'lxc' if kind == 'lxc' else 'vm'}/{vmid}"
    attrs = {
        "vmid": _as_int(vmid),
        "type": kind,
        "status": status,
        "cores": _as_int(cfg.get("cores")),
        "memory_mb": _as_int(cfg.get("memory") or row.get("mem")),
        "ips": _uniq(ips),
        "ostype": cfg.get("ostype"),
        "onboot": _flag(cfg.get("onboot")),
        "lock": row.get("lock") or None,
    }
    snap.ent(guest_key, "guest", name, attrs)
    snap.rel(guest_key, "runs_on", alias, {})
    mp_paths = _guest_mounts(snap, alias, guest_key, cfg) if kind == "lxc" else {}
    _root_disks(snap, alias, guest_key, cfg)
    if kind == "lxc" and status.lower() == "running":
        _lxc_runtime(snap, alias, vmid, guest_key, _primary(attrs["ips"] or []), mp_paths)


def _lxc_runtime(snap: Snap, alias: str, vmid: str, guest_key: str, fallback_ip: str | None,
                 mp_paths: dict[str, str]) -> None:
    try:
        ps = run(["pct", "exec", vmid, "--", "docker", "ps", "--format", "{{json .}}"], alias)
    except CmdError as exc:
        low = str(exc).lower()
        if _binary_missing(str(exc)) or "docker:" in low or "not found" in low:
            snap.note(f"lxc {vmid}: no docker")
        else:
            snap.note(f"lxc {vmid}: docker ps: {exc}")
        ps = None
    except Refused as exc:
        snap.note(f"lxc {vmid}: refused docker ps: {exc}")
        ps = None
    if ps and _parse_ps(ps):
        ids = _ids_from_ps(ps)
        inspected = _inspect_text(snap, alias, ids, guest=vmid) if ids else None

        def mount_dst(source: str):
            # A bind whose source is an LXC mp guest path continues to that host storage.
            dst = mp_paths.get(source)
            if dst:
                ent = snap.get(dst)
                host_path = (ent or {}).get("attrs", {}).get("target") or (ent or {}).get("name")
                extra = {"host_path": host_path} if host_path else {}
                return dst, extra
            return guest_key, {}

        _ingest_docker(
            snap, ps, inspected, prefix=f"{guest_key}/docker", parent_key=guest_key,
            fallback_ip=fallback_ip, mount_dst=mount_dst,
        )
    ss = _try(snap, ["pct", "exec", vmid, "--", "ss", "-Hltnp"], alias, f"lxc {vmid} ss")
    if ss is None:
        ss = _try(snap, ["pct", "exec", vmid, "--", "ss", "-ltn"], alias, f"lxc {vmid} ss")
    if ss is not None:
        guest = snap.get(guest_key)
        if guest is not None:
            guest["attrs"]["listen"] = _parse_ss(ss)
    osr = _try(snap, ["pct", "exec", vmid, "--", "cat", "/etc/os-release"], alias, f"lxc {vmid} os-release")
    pretty = _os_pretty(osr or "")
    if pretty and snap.get(guest_key) is not None:
        snap.get(guest_key)["attrs"]["os"] = pretty


def collect_docker(ssh_alias: str) -> dict:
    """A non-Proxmox docker host. sudo is non-interactive only; a password prompt is a skip."""
    alias = ssh_alias
    snap = Snap("docker", alias)
    if _ALIAS.fullmatch(alias or "") is None:
        snap.note(f"refusing ssh alias {alias!r}")
        return snap.as_dict()
    host = (_try(snap, ["hostname"], alias) or "").strip().split()[:1]
    hostname = host[0] if host else alias
    ips = _parse_ips(_try(snap, ["ip", "-j", "addr"], alias) or "")
    os_name = _os_pretty(_try(snap, ["cat", "/etc/os-release"], alias) or "")
    machine = snap.ent(alias, "machine", hostname, {"hostname": hostname, "ips": ips, "os": os_name})
    ps = _run_docker(snap, alias, ["ps", "--format", "{{json .}}"])
    if not ps or not _parse_ps(ps):
        return snap.as_dict()
    ids = _ids_from_ps(ps)
    inspected = _inspect_text(snap, alias, ids) if ids else None

    def mount_dst(source: str):
        return machine["key"], {"host_path": source}

    _ingest_docker(
        snap, ps, inspected, prefix=f"{alias}/docker", parent_key=machine["key"],
        fallback_ip=_primary(ips), mount_dst=mount_dst,
    )
    return snap.as_dict()


def collect_tailscale() -> dict | None:
    """Local tailnet peers as network members. A missing binary is skipped with no snapshot."""
    try:
        raw = run(["tailscale", "status", "--json"], None)
    except CmdError as exc:
        if _binary_missing(str(exc)):
            return None
        snap = Snap("tailscale", "local")
        snap.note(f"tailscale status: {exc}")
        return snap.as_dict()
    except Refused as exc:
        snap = Snap("tailscale", "local")
        snap.note(f"refused tailscale: {exc}")
        return snap.as_dict()
    try:
        data = _loads(raw)
    except json.JSONDecodeError:
        snap = Snap("tailscale", "local")
        snap.note("tailscale status was not json")
        return snap.as_dict()
    if not isinstance(data, dict):
        snap = Snap("tailscale", "local")
        snap.note("tailscale status was not an object")
        return snap.as_dict()
    suffix = str(data.get("MagicDNSSuffix") or "").strip(".")
    net_key = f"tailscale/{suffix}" if suffix else "tailscale"
    snap = Snap("tailscale", "local")
    snap.ent(net_key, "network", suffix or "tailscale", {"backend": data.get("BackendState") or ""})
    peers = []
    self_peer = data.get("Self")
    if isinstance(self_peer, dict):
        peers.append(self_peer)
    peer_map = data.get("Peer") or data.get("Peers") or {}
    if isinstance(peer_map, dict):
        peers.extend(v for v in peer_map.values() if isinstance(v, dict))
    elif isinstance(peer_map, list):
        peers.extend(v for v in peer_map if isinstance(v, dict))
    for peer in peers:
        host = str(peer.get("HostName") or peer.get("DNSName") or "peer").strip(".")
        host = host.split(".")[0]
        slug = _safe_name(host, "peer")
        key = f"tailscale/peer/{slug}"
        if snap.get(key) is not None:
            key = f"{key}-{len(snap.entities)}"
        dns = str(peer.get("DNSName") or "").strip(".")
        ips = [str(ip) for ip in (peer.get("TailscaleIPs") or []) if isinstance(ip, str)]
        snap.ent(key, "machine", host, {
            "dns": dns, "ips": ips, "os": peer.get("OS") or "", "online": peer.get("Online"),
        })
        snap.rel(key, "member_of", net_key, {})
    return snap.as_dict()


def _read_cloudflared_config() -> str:
    """Local config only. The credentials file named inside it is never opened."""
    for path in _CLOUDFLARED_PATHS:
        try:
            if path.is_file():
                return path.read_text(errors="replace")
        except OSError:
            continue
    return ""


def _ingress(text: str) -> list[dict]:
    """cloudflared's ingress list. Stdlib has no YAML parser; this reads only that shape."""
    items: list[dict] = []
    current: dict | None = None
    inside = False
    tunnel = ""
    for raw in text.splitlines():
        line = raw.split("#", 1)[0].rstrip()
        if not line.strip():
            continue
        if not inside:
            matched = re.match(r"^tunnel:\s*(\S+)\s*$", line.strip())
            if matched:
                tunnel = matched.group(1).strip("\"'")
            if re.match(r"^ingress:\s*$", line.strip()):
                inside = True
            continue
        if line[0] not in " \t":
            break
        stripped = line.strip()
        host = re.match(r"^-\s+hostname:\s*(\S+)\s*$", stripped)
        service = re.match(r"^-\s+service:\s*(\S+)\s*$", stripped)
        host_kv = re.match(r"^hostname:\s*(\S+)\s*$", stripped)
        service_kv = re.match(r"^service:\s*(\S+)\s*$", stripped)
        if host or service:
            current = {}
            items.append(current)
            if host:
                current["hostname"] = host.group(1).strip("\"'")
            if service:
                current["service"] = service.group(1).strip("\"'")
            continue
        if current is None:
            continue
        if host_kv:
            current["hostname"] = host_kv.group(1).strip("\"'")
        elif service_kv:
            current["service"] = service_kv.group(1).strip("\"'")
    return [{"tunnel": tunnel, **item} for item in items]


def collect_cloudflared() -> dict | None:
    """Local tunnels. Missing `cloudflared` is silent. Hostnames come from a readable config."""
    try:
        raw = run(["cloudflared", "tunnel", "list", "--output", "json"], None)
    except CmdError as exc:
        if _binary_missing(str(exc)):
            return None
        snap = Snap("cloudflared", "local")
        snap.note(f"cloudflared tunnel list: {exc}")
        return snap.as_dict()
    except Refused as exc:
        snap = Snap("cloudflared", "local")
        snap.note(f"refused cloudflared: {exc}")
        return snap.as_dict()
    try:
        data = _loads(raw)
    except json.JSONDecodeError:
        snap = Snap("cloudflared", "local")
        snap.note("cloudflared tunnel list was not json")
        return snap.as_dict()
    if isinstance(data, dict):
        data = data.get("tunnels") or data.get("result") or []
    snap = Snap("cloudflared", "local")
    tunnels = []
    for row in data if isinstance(data, list) else []:
        if not isinstance(row, dict):
            continue
        name = str(row.get("name") or row.get("id") or "tunnel")
        tid = str(row.get("id") or "")
        slug = _safe_name(name, "tunnel")
        key = f"cloudflared/{slug}"
        cons = row.get("connections") if isinstance(row.get("connections"), list) else []
        colos = _uniq([str(c.get("colo")) for c in cons if isinstance(c, dict) and c.get("colo")])
        snap.ent(key, "tunnel", name, {"id": tid, "connections": len(cons), "colos": colos})
        tunnels.append({"key": key, "id": tid, "name": name})
    config = _read_cloudflared_config()
    if not config:
        if tunnels:
            snap.note("cloudflared config not readable; hostnames omitted")
        return snap.as_dict()
    rules = _ingress(config)
    tunnel_name = next((r.get("tunnel") for r in rules if r.get("tunnel")), "")
    target = None
    for tun in tunnels:
        if tunnel_name and tunnel_name in (tun["id"], tun["name"]):
            target = tun
            break
    if target is None and len(tunnels) == 1:
        target = tunnels[0]
    if target is None and any(r.get("hostname") for r in rules):
        snap.note("cloudflared config did not match a listed tunnel; hostnames not linked")
        return snap.as_dict()
    linked = False
    for rule in rules:
        host = str(rule.get("hostname") or "").strip(".")
        service = _public_url(str(rule.get("service") or ""))
        if not host or not target:
            continue
        linked = True
        url = f"https://{host}"
        ekey = f"url/{url}"
        snap.ent(ekey, "endpoint", url, {"hostname": host})
        snap.rel(ekey, "exposed_via", target["key"], {"service": service} if service else {})
        if service.startswith("http://") or service.startswith("https://"):
            origin = f"url/{service}"
            snap.ent(origin, "endpoint", service, {})
            snap.rel(target["key"], "talks_to", origin, {})
    if tunnels and not linked:
        snap.note("cloudflared config has no ingress hostnames")
    return snap.as_dict()


def _safe_call(fn, *args):
    try:
        return fn(*args)
    except Exception as exc:  # noqa: BLE001 - one source must not drop the others
        sys.stderr.write(f"commons infra: {fn.__name__} failed: {exc}\n")
        return {
            "collector": fn.__name__.removeprefix("collect_"),
            "source": args[0] if args else "local",
            "at": time.time(),
            "entities": [],
            "relations": [],
            "notes": [f"collector failed: {exc.__class__.__name__}: {_redact_text(str(exc))[:300]}"],
        }


def submit(payload: dict) -> dict:
    """POST snapshots to the hub's /agent/infra with the same URL and key the capture hooks use."""
    import os
    import urllib.request
    from .capture import _key
    url = (os.environ.get("COMMONS_URL") or "").rstrip("/").removesuffix("/mcp")
    if not url:
        raise SystemExit("--submit needs COMMONS_URL (the hub, e.g. http://192.168.50.111:8770) and COMMONS_API_KEY "
                         "or COMMONS_AGENT with ~/.commons/agents/<agent>.key")
    req = urllib.request.Request(url + "/agent/infra", data=json.dumps(payload).encode(), method="POST",
                                 headers={"Content-Type": "application/json", "Authorization": f"Bearer {_key()}"})
    with urllib.request.urlopen(req, timeout=120) as r:  # noqa: S310 - the operator's own hub
        return json.loads(r.read() or b"{}")


def main(argv: list[str] | None = None) -> int:
    """`commons infra collect [--proxmox pve] [--docker ALIAS ...] [--tailscale] [--cloudflared] [--out FILE]`.

    With no source flags the default is Proxmox `pve` plus local tailscale and cloudflared.
    """
    argv = list(sys.argv[1:] if argv is None else argv)
    if argv and argv[0] == "infra":
        argv = argv[1:]
    if argv and argv[0] == "collect":
        argv = argv[1:]
    parser = argparse.ArgumentParser(prog="commons infra collect")
    parser.add_argument("--proxmox", nargs="?", const="pve", default=None,
                        help="ssh alias of a Proxmox node")
    parser.add_argument("--docker", action="append", default=None, metavar="ALIAS",
                        help="ssh alias of a plain docker host (repeatable)")
    parser.add_argument("--tailscale", action="store_true")
    parser.add_argument("--cloudflared", action="store_true")
    parser.add_argument("--out", help="write JSON here (default stdout)")
    parser.add_argument("--submit", action="store_true", help="send the snapshot to the hub ($COMMONS_URL, agent key)")
    args = parser.parse_args(argv)
    flagged = any(
        a == "--proxmox" or a.startswith("--proxmox=") or a == "--docker" or a.startswith("--docker=")
        or a == "--tailscale" or a == "--cloudflared"
        for a in argv
    )
    if not flagged:
        args.proxmox = "pve"
        args.tailscale = True
        args.cloudflared = True
    snaps = []
    if args.proxmox:
        snaps.append(_safe_call(collect_proxmox, args.proxmox))
    for alias in args.docker or []:
        got = _safe_call(collect_docker, alias)
        if got is not None:
            snaps.append(got)
    if args.tailscale:
        got = _safe_call(collect_tailscale)
        if got is not None:
            snaps.append(got)
    if args.cloudflared:
        got = _safe_call(collect_cloudflared)
        if got is not None:
            snaps.append(got)
    text = json.dumps({"snapshots": snaps}, indent=2) + "\n"
    if args.submit:
        got = submit({"snapshots": [x for x in snaps if x]})
        for m in got.get("merged", []):
            print(f"hub: {m['source']}: {m['nodes']} nodes, {m['edges']} links, {m['stale']} now stale", file=sys.stderr)
        if not args.out:
            return 0
    if args.out:
        path = Path(args.out)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text)
    else:
        sys.stdout.write(text)
    return 0
