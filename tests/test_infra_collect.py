"""Read-only infra collector: fixture output in, snapshot shape out. No network."""
import json
import shlex
import subprocess

import pytest

from commons_memory import infra_collect as IC

IP = json.dumps([
    {"ifname": "lo", "addr_info": [{"family": "inet", "local": "127.0.0.1", "prefixlen": 8}]},
    {"ifname": "vmbr0", "addr_info": [
        {"family": "inet", "local": "192.168.50.10", "prefixlen": 24},
        {"family": "inet6", "local": "fe80::1", "prefixlen": 64},
    ]},
    {"ifname": "docker0", "addr_info": [{"family": "inet", "local": "172.17.0.1", "prefixlen": 16}]},
])
OS_HOST = 'PRETTY_NAME="Debian GNU/Linux 12 (bookworm)"\nID=debian\n'
OS_GUEST = 'PRETTY_NAME="Ubuntu 24.04.1 LTS"\nID=ubuntu\n'
NODES = json.dumps([{"node": "pve", "status": "online"}])
STATUS = json.dumps({
    "cpu": 0.12,
    "cpuinfo": {"cores": 8, "cpus": 16, "model": "AMD EPYC", "sockets": 2},
    "memory": {"total": 67000000000, "used": 32000000000, "free": 35000000000},
    "pveversion": "pve-manager/8.2.2",
    "uptime": 100000,
})
ZPOOL = "tank  1.81T  1.20T  610G  -  -  10%  66%  1.00x  ONLINE  -\n"
PVESM = (
    "Name             Type     Status           Total            Used       Available        %\n"
    "local-zfs     zfspool     active       2000000000      800000000      1200000000   40.00%\n"
    "local             dir     active         10000000        2000000        8000000   20.00%\n"
)
FINDMNT = json.dumps({"filesystems": [{
    "target": "/", "source": "/dev/mapper/pve-root", "fstype": "ext4",
    "children": [
        {"target": "/mnt/library", "source": "nas:/volume1/library", "fstype": "nfs4", "options": "rw,relatime"},
        {"target": "/proc", "source": "proc", "fstype": "proc"},
    ],
}]})
DF = (
    "Filesystem     Type  1024-blocks      Used Available Capacity Mounted on\n"
    "/dev/mapper/pve-root ext4 100000000 20000000 80000000 20% /\n"
    "nas:/volume1/library nfs4 900000000 100000000 800000000 12% /mnt/library\n"
)
UNITS = (
    "altmount.service loaded active running Altmount\n"
    "plex-guard.service loaded active running Plex guard\n"
    "pve-firewall.service loaded active running Proxmox VE firewall\n"
    "proxmox-firewall.service loaded active running Proxmox VE firewall\n"
    "cron.service loaded active running Regular background program processing daemon\n"
    "systemd-logind.service loaded active running User Login Management\n"
    "ssh.service loaded active running OpenBSD Secure Shell server\n"
)
PCT = (
    "VMID       Status     Lock         Name\n"
    "106        running                 media\n"
    "107        stopped                 spare\n"
)
QM = (
    "      VMID NAME                 STATUS     MEM(MB)    BOOTDISK(GB) PID\n"
    "       200 haos                 running    4096               32.00 4242\n"
)
CFG_106 = """\
arch: amd64
cores: 4
hostname: media
memory: 4096
mp0: /mnt/library,mp=/library
mp1: local-zfs:subvol-106-disk-1,mp=/data,ro=1
net0: name=eth0,bridge=vmbr0,firewall=1,gw=192.168.50.1,hwaddr=BC:24:11:AA:BB:CC,ip=192.168.50.205/24,type=veth
onboot: 1
ostype: debian
rootfs: local-zfs:subvol-106-disk-0,size=32G
unprivileged: 1
"""
CFG_107 = """\
cores: 1
hostname: spare
memory: 512
net0: name=eth0,bridge=vmbr0,ip=dhcp,type=veth
onboot: 0
ostype: debian
rootfs: local:subvol-107-disk-0,size=4G
"""
CFG_200 = """\
cores: 2
memory: 4096
name: haos
ostype: l26
onboot: 1
net0: virtio=AA:BB:CC:DD:EE:FF,bridge=vmbr0
ipconfig0: ip=192.168.50.50/24,gw=192.168.50.1
scsi0: local-zfs:vm-200-disk-0,size=32G
agent: 1
"""
IFACES = json.dumps([
    {"hwaddr": "BC:24:11:AA:BB:CC", "inet": "192.168.50.205/24", "name": "eth0"},
    {"hwaddr": "BC:24:11:AA:BB:CD", "inet": "192.168.50.206/24", "name": "eth1"},
])
PS_GUEST = json.dumps({
    "ID": "abc123def456", "Image": "lscr.io/linuxserver/radarr:latest", "Names": "radarr",
    "State": "running", "Status": "Up 3 days", "Ports": "0.0.0.0:7878->7878/tcp",
}) + "\n"
PS_HOST = json.dumps({
    "ID": "feedface", "Image": "prom/node-exporter:latest", "Names": "node-exporter",
    "State": "running", "Ports": "0.0.0.0:9100->9100/tcp",
}) + "\n"
INSPECT_GUEST = json.dumps([{
    "Name": "/radarr",
    "Id": "abc123def4567890",
    "Config": {"Image": "lscr.io/linuxserver/radarr:latest", "Env": ["PUID=1000", "TOKEN=SUPERSECRET"]},
    "State": {"Status": "running"},
    "HostConfig": {
        "Binds": ["/library:/media:ro", "/config:/config"],
        "PortBindings": {"7878/tcp": [{"HostIp": "", "HostPort": "7878"}]},
    },
    "Mounts": [
        {"Type": "bind", "Source": "/library", "Destination": "/media", "RW": False, "Mode": "ro"},
        {"Type": "bind", "Source": "/config", "Destination": "/config", "RW": True, "Mode": ""},
    ],
    "NetworkSettings": {"Ports": {"7878/tcp": [{"HostIp": "0.0.0.0", "HostPort": "7878"}]}},
}])
INSPECT_HOST = json.dumps([{
    "Name": "/node-exporter",
    "Id": "feedfacebeef",
    "Config": {"Image": "prom/node-exporter:latest", "Env": ["TOKEN=SUPERSECRET"]},
    "State": {"Status": "running"},
    "Mounts": [{"Type": "bind", "Source": "/mnt/library", "Destination": "/data", "RW": False, "Mode": "ro"}],
    "NetworkSettings": {"Ports": {"9100/tcp": [{"HostIp": "0.0.0.0", "HostPort": "9100"}]}},
}])
SS = (
    "LISTEN 0 4096 0.0.0.0:7878 0.0.0.0:*\n"
    "LISTEN 0 128 127.0.0.1:8080 0.0.0.0:*\n"
    "LISTEN 0 128 *:22 *:*\n"
)
SYS = ["systemctl", "list-units", "--type=service", "--state=running", "--no-legend", "--plain"]
FMT = ["docker", "ps", "--format", "{{json .}}"]


def _fix(cmd):
    key = tuple(cmd)
    table = {
        ("hostname",): "pve\n",
        ("ip", "-j", "addr"): IP,
        ("cat", "/etc/os-release"): OS_HOST,
        ("pvesh", "get", "/nodes", "--output-format", "json"): NODES,
        ("pvesh", "get", "/nodes/pve/status", "--output-format", "json"): STATUS,
        ("zpool", "list", "-H"): ZPOOL,
        ("pvesm", "status"): PVESM,
        ("findmnt", "-J"): FINDMNT,
        ("df", "-PT"): DF,
        tuple(SYS): UNITS,
        tuple(FMT): PS_HOST,
        ("docker", "inspect", "feedface"): INSPECT_HOST,
        ("pct", "list"): PCT,
        ("qm", "list"): QM,
        ("pct", "config", "106"): CFG_106,
        ("pct", "config", "107"): CFG_107,
        ("qm", "config", "200"): CFG_200,
        ("pvesh", "get", "/nodes/pve/lxc/106/interfaces", "--output-format", "json"): IFACES,
        ("pct", "exec", "106", "--", "docker", "ps", "--format", "{{json .}}"): PS_GUEST,
        ("pct", "exec", "106", "--", "docker", "inspect", "abc123def456"): INSPECT_GUEST,
        ("pct", "exec", "106", "--", "ss", "-Hltnp"): SS,
        ("pct", "exec", "106", "--", "cat", "/etc/os-release"): OS_GUEST,
    }
    if key not in table:
        raise AssertionError("unexpected command: " + shlex.join(cmd))
    return table[key]


def _collect(monkeypatch):
    calls = []

    def fake(cmd, via=None):
        IC._allow(cmd, via)
        calls.append((list(cmd), via))
        assert via == "pve"
        return _fix(cmd)

    monkeypatch.setattr(IC, "run", fake)
    snap = IC.collect_proxmox("pve")
    return snap, calls


def test_proxmox_snapshot(monkeypatch):
    snap, calls = _collect(monkeypatch)
    by = {e["key"]: e for e in snap["entities"]}
    assert len(by) == len(snap["entities"])
    machine = by["pve"]
    assert machine["kind"] == "machine"
    assert machine["name"] == "pve"
    assert machine["attrs"]["ips"] == ["192.168.50.10", "172.17.0.1"]
    assert machine["attrs"]["cpu_count"] == 16
    assert machine["attrs"]["memory_total"] == 67000000000
    assert "fe80::1" not in machine["attrs"]["ips"]
    assert "127.0.0.1" not in machine["attrs"]["ips"]

    guest = by["pve/lxc/106"]
    assert guest["kind"] == "guest"
    assert guest["name"] == "media"
    assert guest["attrs"]["status"] == "running"
    assert guest["attrs"]["cores"] == 4
    assert guest["attrs"]["memory_mb"] == 4096
    assert guest["attrs"]["ips"] == ["192.168.50.205", "192.168.50.206"]
    assert guest["attrs"]["ostype"] == "debian"
    assert guest["attrs"]["onboot"] is True
    assert guest["attrs"]["os"] == "Ubuntu 24.04.1 LTS"
    assert {row["port"] for row in guest["attrs"]["listen"]} == {7878, 8080, 22}
    assert any(r["src"] == "pve/lxc/106" and r["rel"] == "runs_on" and r["dst"] == "pve" for r in snap["relations"])

    mounts = [r for r in snap["relations"] if r["src"] == "pve/lxc/106" and r["rel"] == "mounts"]
    lib = next(r for r in mounts if r["dst"] == "pve/mount/mnt/library")
    assert lib["attrs"]["guest_path"] == "/library"
    assert lib["attrs"]["host_path"] == "/mnt/library"
    assert lib["attrs"]["read_only"] is False
    data = next(r for r in mounts if r["dst"] == "pve/storage/local-zfs")
    assert data["attrs"]["guest_path"] == "/data"
    assert data["attrs"]["host_path"] == "local-zfs:subvol-106-disk-1"
    assert data["attrs"]["read_only"] is True
    assert by["pve/mount/mnt/library"]["kind"] == "storage"
    assert by["pve/mount/mnt/library"]["attrs"]["fstype"] == "nfs4"
    assert by["pve/storage/local-zfs"]["attrs"]["type"] == "zfspool"
    assert by["pve/zpool/tank"]["attrs"]["health"] == "ONLINE"
    assert any(r["src"] == "pve/lxc/106" and r["rel"] == "stores_in" and r["dst"] == "pve/storage/local-zfs"
               for r in snap["relations"])

    box = by["pve/lxc/106/docker/radarr"]
    assert box["kind"] == "service"
    assert box["attrs"]["image"] == "lscr.io/linuxserver/radarr:latest"
    assert box["attrs"]["status"] == "running"
    assert any(r["src"] == box["key"] and r["rel"] == "runs_on" and r["dst"] == "pve/lxc/106" for r in snap["relations"])
    binds = [r for r in snap["relations"] if r["src"] == box["key"] and r["rel"] == "mounts"]
    media = next(r for r in binds if r["attrs"]["container_path"] == "/media")
    assert media["dst"] == "pve/mount/mnt/library"
    assert media["attrs"]["guest_path"] == "/library"
    assert media["attrs"]["read_only"] is True
    cfg = next(r for r in binds if r["attrs"]["container_path"] == "/config")
    assert cfg["dst"] == "pve/lxc/106"
    assert cfg["attrs"]["guest_path"] == "/config"

    ep = "url/http://192.168.50.205:7878"
    assert by[ep]["kind"] == "endpoint"
    assert any(r["src"] == box["key"] and r["rel"] == "reachable_at" and r["dst"] == ep for r in snap["relations"])
    assert "url/http://192.168.50.205:22" not in by

    host_ct = by["pve/docker/node-exporter"]
    assert any(r["src"] == host_ct["key"] and r["rel"] == "runs_on" and r["dst"] == "pve" for r in snap["relations"])
    assert by["url/http://192.168.50.10:9100"]["kind"] == "endpoint"
    assert any(r["src"] == host_ct["key"] and r["rel"] == "mounts" and r["dst"] == "pve/mount/mnt/library"
               for r in snap["relations"])

    assert by["pve/service/altmount"]["kind"] == "service"
    assert by["pve/service/plex-guard"]["kind"] == "service"
    assert "pve/service/pve-firewall" not in by
    assert "pve/service/proxmox-firewall" not in by
    assert "pve/service/cron" not in by
    assert "pve/service/systemd-logind" not in by
    assert "pve/service/ssh" not in by

    spare = by["pve/lxc/107"]
    assert spare["attrs"]["status"] == "stopped"
    assert spare["attrs"]["ips"] == []
    assert "os" not in spare["attrs"]
    vm = by["pve/vm/200"]
    assert vm["kind"] == "guest" and vm["name"] == "haos"
    assert vm["attrs"]["ips"] == ["192.168.50.50"]
    assert any(r["src"] == "pve/vm/200" and r["rel"] == "runs_on" and r["dst"] == "pve" for r in snap["relations"])

    blob = json.dumps(snap)
    assert "SUPERSECRET" not in blob
    assert "PUID" not in blob
    assert "Env" not in blob
    assert not any(c[0][:3] == ["pct", "exec", "107"] for c in calls)
    assert not any(c[0][:2] == ["pct", "exec"] and c[0][2] == "200" for c in calls)


REFUSED = [
    ["pct", "stop", "106"],
    ["pct", "set", "106", "-onboot", "1"],
    ["pct", "reboot", "106"],
    ["pct", "start", "106"],
    ["pct", "destroy", "106"],
    ["qm", "stop", "200"],
    ["qm", "set", "200", "-memory", "512"],
    ["docker", "rm", "x"],
    ["docker", "stop", "x"],
    ["docker", "restart", "x"],
    ["systemctl", "restart", "ssh"],
    ["systemctl", "start", "ssh"],
    ["systemctl", "stop", "ssh"],
    ["pvesh", "set", "/nodes/pve"],
    ["pvesh", "create", "/nodes/pve/lxc"],
    ["pvesh", "delete", "/nodes/pve/lxc/106"],
    ["pvesh", "get", "/nodes/pve/status"],
    ["rm", "-rf", "/"],
    ["pct", "exec", "106", "--", "docker", "rm", "x"],
    ["pct", "exec", "106", "--", "bash", "-c", "reboot"],
    ["pct", "exec", "106", "--", "systemctl", "restart", "ssh"],
    ["sudo", "docker", "ps"],
    ["sudo", "-n", "reboot"],
    ["cat", "/etc/shadow"],
    ["dd", "if=/dev/zero", "of=/dev/sda"],
    ["ip", "link", "set", "eth0", "down"],
    ["zpool", "destroy", "tank"],
    ["findmnt", "-J", "--output", "help"],
]


def test_refuses_write_without_running(monkeypatch):
    def boom(*_a, **_k):
        raise AssertionError("subprocess ran")

    monkeypatch.setattr(subprocess, "run", boom)
    for cmd in REFUSED:
        with pytest.raises(IC.Refused):
            IC.run(cmd, "pve")
    with pytest.raises(IC.Refused):
        IC.run(["hostname"], "pve;reboot")
    with pytest.raises(IC.Refused):
        IC.run(["pct", "exec", "106", "--", "cat", "/etc/shadow"], None)


ALLOWED = [
    ["hostname"],
    ["ip", "-j", "addr"],
    ["findmnt", "-J"],
    ["zpool", "list", "-H"],
    ["df", "-PT"],
    ["pvesm", "status"],
    ["pct", "list"],
    ["pct", "config", "106"],
    ["qm", "list"],
    ["qm", "config", "200"],
    ["pvesh", "get", "/nodes", "--output-format", "json"],
    ["pvesh", "get", "/nodes/pve/status", "--output-format", "json"],
    ["pvesh", "get", "/nodes/pve/lxc/106/interfaces", "--output-format", "json"],
    SYS,
    ["docker", "ps"],
    ["docker", "ps", "--format", "{{json .}}"],
    ["docker", "inspect", "abc123", "radarr"],
    ["sudo", "-n", "docker", "ps", "--format", "{{json .}}"],
    ["sudo", "-n", "docker", "inspect", "abc123"],
    ["tailscale", "status", "--json"],
    ["cloudflared", "tunnel", "list", "--output", "json"],
    ["cat", "/etc/os-release"],
    ["ss", "-Hltnp"],
    ["ss", "-ltn"],
    ["pct", "exec", "106", "--", "docker", "ps", "--format", "{{json .}}"],
    ["pct", "exec", "106", "--", "docker", "inspect", "abc123"],
    ["pct", "exec", "106", "--", "ip", "-j", "addr"],
    ["pct", "exec", "106", "--", "ss", "-Hltnp"],
    ["pct", "exec", "106", "--", "ss", "-ltn"],
    ["pct", "exec", "106", "--", "hostname"],
    ["pct", "exec", "106", "--", "cat", "/etc/os-release"],
    ["pct", "exec", "106", "--", "findmnt", "-J"],
    ["pct", "exec", "106", "--", *SYS],
]


def test_allowlist_runs_only_reads(monkeypatch):
    seen = {}

    class Proc:
        returncode = 0
        stdout = "ok\n"
        stderr = ""

    def fake_run(argv, **kwargs):
        seen["argv"] = argv
        seen["timeout"] = kwargs.get("timeout")
        seen["stdin"] = kwargs.get("stdin")
        return Proc()

    monkeypatch.setattr(subprocess, "run", fake_run)
    assert IC.run(["hostname"], "pve") == "ok\n"
    assert seen["argv"][:5] == ["ssh", "-o", "BatchMode=yes", "-o", "ConnectTimeout=10"]
    assert seen["argv"][5] == "pve"
    assert seen["argv"][6] == "hostname"
    assert seen["timeout"] == 20
    assert seen["stdin"] is subprocess.DEVNULL
    assert IC.run(["tailscale", "status", "--json"], None) == "ok\n"
    assert seen["argv"] == ["tailscale", "status", "--json"]
    for cmd in ALLOWED:
        assert IC.run(cmd, "pve") == "ok\n"


def test_docker_sudo_then_skip_password(monkeypatch):
    calls = []

    def fake(cmd, via=None):
        IC._allow(cmd, via)
        calls.append(list(cmd))
        assert via == "nas"
        if cmd == ["hostname"]:
            return "nas\n"
        if cmd[:2] == ["ip", "-j"]:
            return json.dumps([{"ifname": "eth0", "addr_info": [{"family": "inet", "local": "192.168.50.20"}]}])
        if cmd[:2] == ["cat", "/etc/os-release"]:
            return 'PRETTY_NAME="Synology DSM 7"\n'
        if cmd[:1] == ["docker"]:
            raise IC.CmdError("exit 1: permission denied while trying to connect to the Docker daemon socket")
        if cmd[:4] == ["sudo", "-n", "docker", "ps"]:
            return PS_GUEST
        if cmd[:4] == ["sudo", "-n", "docker", "inspect"]:
            return INSPECT_GUEST
        raise AssertionError(cmd)

    monkeypatch.setattr(IC, "run", fake)
    snap = IC.collect_docker("nas")
    by = {e["key"]: e for e in snap["entities"]}
    assert by["nas"]["kind"] == "machine"
    assert by["nas/docker/radarr"]["kind"] == "service"
    assert any(r["src"] == "nas/docker/radarr" and r["rel"] == "runs_on" and r["dst"] == "nas" for r in snap["relations"])
    assert by["url/http://192.168.50.20:7878"]["kind"] == "endpoint"
    assert "SUPERSECRET" not in json.dumps(snap)
    assert any(c[:2] == ["sudo", "-n"] for c in calls)
    assert all("password" not in shlex.join(c) for c in calls)

    def prompting(cmd, via=None):
        IC._allow(cmd, via)
        if cmd == ["hostname"]:
            return "nas\n"
        if cmd[:2] == ["ip", "-j"]:
            return "[]"
        if cmd[:2] == ["cat", "/etc/os-release"]:
            return "ID=debian\n"
        if cmd[0] == "docker":
            raise IC.CmdError("exit 1: permission denied")
        if cmd[:2] == ["sudo", "-n"]:
            raise IC.CmdError("exit 1: sudo: a password is required")
        raise AssertionError(cmd)

    monkeypatch.setattr(IC, "run", prompting)
    skipped = IC.collect_docker("nas")
    assert any("password" in n for n in skipped["notes"])
    assert not any(e["kind"] == "service" for e in skipped["entities"])


def test_tailscale_members(monkeypatch):
    raw = json.dumps({
        "BackendState": "Running",
        "MagicDNSSuffix": "tailnet.ts.net",
        "Self": {"HostName": "macbook", "DNSName": "macbook.tailnet.ts.net.", "TailscaleIPs": ["100.64.0.1"],
                 "Online": True, "OS": "macOS"},
        "Peer": {"nodekey:abc": {"HostName": "pve", "DNSName": "pve.tailnet.ts.net.", "TailscaleIPs": ["100.64.0.2"],
                                 "Online": True, "OS": "linux"}},
    })

    def fake(cmd, via=None):
        IC._allow(cmd, via)
        assert via is None and cmd == ["tailscale", "status", "--json"]
        return raw

    monkeypatch.setattr(IC, "run", fake)
    snap = IC.collect_tailscale()
    by = {e["key"]: e for e in snap["entities"]}
    assert by["tailscale/tailnet.ts.net"]["kind"] == "network"
    assert by["tailscale/peer/macbook"]["kind"] == "machine"
    assert by["tailscale/peer/pve"]["attrs"]["ips"] == ["100.64.0.2"]
    members = [r for r in snap["relations"] if r["rel"] == "member_of"]
    assert {r["src"] for r in members} == {"tailscale/peer/macbook", "tailscale/peer/pve"}
    assert all(r["dst"] == "tailscale/tailnet.ts.net" for r in members)
    assert "nodekey" not in json.dumps(snap)


def test_cloudflared_ingress_and_missing(monkeypatch, tmp_path):
    listed = json.dumps([{
        "id": "11111111-1111-1111-1111-111111111111",
        "name": "home",
        "connections": [{"colo": "AMS", "origin_ip": "203.0.113.8"}],
    }])

    def fake(cmd, via=None):
        IC._allow(cmd, via)
        assert cmd == ["cloudflared", "tunnel", "list", "--output", "json"]
        return listed

    monkeypatch.setattr(IC, "run", fake)
    monkeypatch.setattr(IC, "_read_cloudflared_config", lambda: """
tunnel: 11111111-1111-1111-1111-111111111111
credentials-file: /Users/me/.cloudflared/1111.json
ingress:
  - hostname: radarr.example.com
    service: http://192.168.50.205:7878
  - service: http_status:404
""")
    snap = IC.collect_cloudflared()
    by = {e["key"]: e for e in snap["entities"]}
    assert by["cloudflared/home"]["kind"] == "tunnel"
    assert "203.0.113.8" not in json.dumps(snap)
    assert "credentials-file" not in json.dumps(snap)
    assert ".json" not in json.dumps(snap)
    ep = by["url/https://radarr.example.com"]
    assert ep["kind"] == "endpoint"
    via = next(r for r in snap["relations"] if r["rel"] == "exposed_via")
    assert via["src"] == ep["key"] and via["dst"] == "cloudflared/home"
    assert via["attrs"]["service"] == "http://192.168.50.205:7878"
    assert any(r["rel"] == "talks_to" and r["dst"] == "url/http://192.168.50.205:7878" for r in snap["relations"])

    def missing(cmd, via=None):
        IC._allow(cmd, via)
        raise IC.CmdError("not found: cloudflared")

    monkeypatch.setattr(IC, "run", missing)
    assert IC.collect_cloudflared() is None
    monkeypatch.setattr(IC, "run", lambda cmd, via=None: (_ for _ in ()).throw(IC.CmdError("not found: tailscale")))
    assert IC.collect_tailscale() is None


def test_cli_nostore_defaults(monkeypatch, tmp_path, capsys):
    seen = []

    def snap(collector, source):
        return {"collector": collector, "source": source, "at": 1.0, "entities": [], "relations": [], "notes": []}

    monkeypatch.setattr(IC, "collect_proxmox", lambda alias="pve": seen.append(("pve", alias)) or snap("proxmox", alias))
    monkeypatch.setattr(IC, "collect_tailscale", lambda: seen.append(("ts",)) or None)
    monkeypatch.setattr(IC, "collect_cloudflared", lambda: seen.append(("cf",)) or None)
    monkeypatch.setattr(IC, "collect_docker", lambda alias: seen.append(("d", alias)) or snap("docker", alias))
    from commons_memory.cli import main
    out = tmp_path / "out.json"
    rc = main(["--db", str(tmp_path / "no.db"), "infra", "collect", "--out", str(out)])
    assert rc == 0
    assert ("pve", "pve") in seen and ("ts",) in seen and ("cf",) in seen
    doc = json.loads(out.read_text())
    assert doc["snapshots"][0]["collector"] == "proxmox"
    assert not (tmp_path / "no.db").exists()
    seen.clear()
    rc = main(["--db", str(tmp_path / "no.db"), "infra", "collect", "--docker", "nas"])
    assert rc == 0
    assert seen == [("d", "nas")]
    assert "snapshots" in capsys.readouterr().out
    assert "infra" in IC_NOSTORE()


def IC_NOSTORE():
    from commons_memory.cli_v8 import NOSTORE
    return NOSTORE
