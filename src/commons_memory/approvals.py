"""Approvals only the owner can give, from wherever they are, in one step.

When the decision chain hands a credential use (or an upstream MCP call) to the user, the agent gets an approval link
(/approve/<question>) to show the user. The page says exactly what is asked: the key, the method, the URL, the request
body and the reason. Anyone can open it, so an agent can pass it on, but approving needs proof that the owner is
there: the owner approval PIN (set once, on first use or with `commons approvals set-pin` on the hub), after which
that browser stays trusted for 12 hours through an HttpOnly cookie an agent never sees. Phone and email links stay
one-tap: they are signed and only ever reach the owner. A "yes" typed into an agent's chat never counts, because an
agent can type that too. Approving runs the stored request at once and hands the result to the waiting agent.
"""
from __future__ import annotations

import hashlib
import hmac
import html
import json
import os
import re
import secrets

from .ids import iso, now
from .store import CommonsError, Store

SESSION_S = 12 * 3600
LOCK_AFTER, LOCK_S = 5, 900
QUESTION = re.compile(r"\[(vault|mcp) ([A-Za-z0-9_]+)\]")


def _get(s: Store, key: str):
    r = s.q("SELECT value FROM meta WHERE key=?", (key,))
    return json.loads(r[0]["value"]) if r else None


def _put(s: Store, key: str, value) -> None:
    s.x("INSERT OR REPLACE INTO meta VALUES(?,?)", (key, json.dumps(value)))


def has_pin(s: Store) -> bool:
    return bool(_get(s, "approval_pin"))


def setup_code(s: Store, rotate: bool = False) -> str:
    """A one-time code for choosing the first PIN. It is printed in the hub's own log at startup and by
    `commons approvals code` on the hub, never sent over the API, so an agent that can only reach Commons can't use it."""
    rec = _get(s, "approval_setup")
    if rec and not rotate and rec.get("code"):
        return rec["code"]
    code = f"{secrets.randbelow(10**4):04d}-{secrets.randbelow(10**4):04d}"
    _put(s, "approval_setup", {"code": code, "at": iso(now())})
    return code


def set_pin(s: Store, pin: str, current: str = "", code: str = "", by: str = "owner", client: str = "") -> dict:
    """The first PIN needs the setup code from the hub's log; changing it needs the current PIN. On the hub itself
    (`commons approvals set-pin`, by="host") neither is needed."""
    if not re.fullmatch(r"\d{6,12}", pin or ""):
        raise CommonsError("the approval PIN is 6 to 12 digits")
    if by != "host":
        if has_pin(s):
            if not check_pin(s, current, client):
                raise CommonsError("enter the current PIN to change it (or run `commons approvals set-pin` on the hub)")
        else:
            rec = _get(s, "approval_setup") or {}
            if not rec.get("code") or not hmac.compare_digest(rec["code"], (code or "").strip()):
                _fail(s, client)
                raise CommonsError("wrong setup code: it is in the hub's log (`docker logs commons | grep 'setup code'`) "
                                   "or run `commons approvals code` on the hub")
    s.x("DELETE FROM meta WHERE key='approval_setup'")  # one-time: spent once a PIN exists
    salt = secrets.token_hex(16)
    _put(s, "approval_pin", {"salt": salt, "hash": _hash(pin, salt), "at": iso(now()), "by": by})
    s.audit(None, "approvals.pin_set", by, {})
    return {"pin_set": True}


def _hash(pin: str, salt: str) -> str:
    return hashlib.pbkdf2_hmac("sha256", pin.encode(), salt.encode(), 200_000).hex()


def check_pin(s: Store, pin: str, client: str = "") -> bool:
    """Wrong tries lock that device (client address) for 15 minutes, not everyone: a stranger on the LAN can't lock the
    owner out. A trusted browser (session cookie) and phone links don't need the PIN at all."""
    rec, lock = _get(s, "approval_pin"), _get(s, f"approval_lock:{client}") or {"fails": 0, "until": 0}
    if not rec or not pin:
        return False
    if lock["until"] > now():
        raise CommonsError(f"too many wrong PINs: approvals are locked for {int(lock['until'] - now()) // 60 + 1} min")
    ok = hmac.compare_digest(_hash(pin, rec["salt"]), rec["hash"])
    if ok:
        _put(s, f"approval_lock:{client}", {"fails": 0, "until": 0})
    else:
        _fail(s, client)
    return ok


def _fail(s: Store, client: str = "") -> None:
    key = f"approval_lock:{client}"
    lock = _get(s, key) or {"fails": 0, "until": 0}
    if lock["until"] > now():
        raise CommonsError(f"too many wrong tries: locked for {int(lock['until'] - now()) // 60 + 1} min")
    fails = lock["fails"] + 1
    _put(s, key, {"fails": 0 if fails >= LOCK_AFTER else fails, "until": now() + LOCK_S if fails >= LOCK_AFTER else 0})
    s.audit(None, "approvals.wrong_pin_or_code", client, {"fails": fails})


def _session_sig(s: Store, exp: str) -> str:
    from .phone import _secret
    pin = (_get(s, "approval_pin") or {}).get("hash", "")[:32]  # bound to the PIN: changing it logs every browser out
    return hmac.new(_secret(s), f"approver:{exp}:{pin}".encode(), hashlib.sha256).hexdigest()


def session_token(s: Store) -> str:
    exp = str(int(now() + SESSION_S))
    return exp + "." + _session_sig(s, exp)


def session_ok(s: Store, token: str) -> bool:
    exp, _, sig = (token or "").partition(".")
    if not exp.isdigit() or int(exp) < now() or not has_pin(s):
        return False
    return hmac.compare_digest(sig, _session_sig(s, exp))


def needs_proof(question_text: str) -> bool:
    """Credential and MCP approvals need the owner; ordinary questions don't."""
    return bool(QUESTION.search(question_text or ""))


def url(s: Store, question_id: str, host: str = "") -> str:
    from . import settings
    base = (settings.get(s, "dashboard_url") or "").rstrip("/")
    if not base and host:
        from .dashboard import _host_allowed
        h = host.rsplit(":", 1)[0] if host.count(":") == 1 else host
        if _host_allowed(h, s):  # the Host header comes from the agent: never build a link to a name we don't serve
            base = f"http://{h}:{os.environ.get('COMMONS_DASHBOARD_PORT', '8766')}"
    return f"{base}/approve/{question_id}" if base else f"/approve/{question_id}"


def details(s: Store, question_id: str) -> dict:
    from . import board as B
    item = B.get(s, question_id)
    m = QUESTION.search(item["text"])
    out = {"id": question_id, "status": item["status"], "question": item["text"], "kind": m.group(1) if m else "question",
           "answer": item.get("evidence"), "approve_url": url(s, question_id)}
    if m and m.group(1) == "vault":
        d = s.q("SELECT * FROM vault_decisions WHERE id=?", (m.group(2),))
        if d:
            d = dict(d[0])
            pend = json.loads(d.get("pending") or "{}")
            out |= {"agent": s.label(d["agent_id"])[1], "credential": d["cred"], "method": d["method"], "url": d["url"],
                    "reason": d["justification"], "why_asked": d["reason"], "body": (pend.get("body") or "")[:4000],
                    "result": pend.get("result"),
                    # approved, and Commons is running it right now: the result is seconds away
                    "running": d["verdict"] == "user_allowed" and bool(pend) and "result" not in pend}
    return out


def answer(s: Store, question_id: str, choice: str, pin: str = "", session: str = "", client: str = "") -> dict:
    """choice yes | all (this and similar writes to the same service for an hour) | always (this key, from now on) | no."""
    if choice not in ("yes", "all", "always", "no"):
        raise CommonsError("choose yes, all, always or no")
    if not has_pin(s):
        raise CommonsError("set your approval PIN first (Setup page, or the box on this page)")
    if not (session_ok(s, session) or check_pin(s, pin, client)):
        raise CommonsError("wrong PIN")
    from . import board as B
    item = B.get(s, question_id)
    if item["status"] not in ("open", "doing", "blocked"):
        return details(s, question_id) | {"already": item["status"]}
    if choice == "always":  # trust this key from now on, at the owner's risk; it can be changed back on Setup
        d = details(s, question_id)
        if d.get("credential"):
            s.x("UPDATE vault_creds SET risk='always' WHERE name=?", (d["credential"],))
            s.audit(None, "vault.always_allow", d["credential"], {"question": question_id})
    B.answer(s, question_id, {"yes": "yes", "all": "yes all", "always": "yes", "no": "no"}[choice],
             by="owner (approval page)")
    return details(s, question_id)


PAGE = """<!doctype html><html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width, initial-scale=1">
<title>Approve · Commons</title><style>
:root{--bg:#f6f5f2;--card:#fff;--ink:#1d1b18;--mut:#6b665d;--acc:#c2410c;--ok:#15803d;--bad:#b91c1c;--line:#e7e3dc}
@media (prefers-color-scheme:dark){:root{--bg:#141311;--card:#1e1c19;--ink:#f1eee8;--mut:#a39d92;--line:#2f2c27}}
body{margin:0;background:var(--bg);color:var(--ink);font:15px/1.5 -apple-system,system-ui,sans-serif}
main{max-width:640px;margin:0 auto;padding:24px 16px}.card{background:var(--card);border:1px solid var(--line);border-radius:14px;padding:18px;margin-bottom:14px}
h1{font-size:20px;margin:0 0 6px}.mut{color:var(--mut)}code,pre{font:13px/1.45 ui-monospace,Menlo,monospace}
pre{white-space:pre-wrap;word-break:break-word;background:var(--bg);border-radius:10px;padding:10px;max-height:320px;overflow:auto}
dl{display:grid;grid-template-columns:auto 1fr;gap:6px 14px;margin:0}dt{color:var(--mut)}dd{margin:0;word-break:break-word}
.row{display:flex;flex-wrap:wrap;gap:10px;margin-top:12px}button{min-height:44px;padding:0 18px;border-radius:10px;border:1px solid var(--line);
background:var(--card);color:var(--ink);font-weight:600;cursor:pointer}button.ok{background:var(--ok);color:#fff;border-color:var(--ok)}
button.bad{color:var(--bad)}input{min-height:44px;font-size:18px;letter-spacing:4px;padding:0 12px;border-radius:10px;border:1px solid var(--line);
background:var(--bg);color:var(--ink);width:180px}.done{color:var(--ok);font-weight:600}.err{color:var(--bad)}
</style></head><body><main>%(body)s</main><script>
async function act(choice){const pin=(document.getElementById('pin')||{}).value||'';const r=await fetch(location.pathname,{method:'POST',
headers:{'Content-Type':'application/json','X-Commons':'1'},body:JSON.stringify({choice,pin})});const d=await r.json();
if(d.error){document.getElementById('msg').textContent=d.error;document.getElementById('msg').className='err';return}location.reload()}
async function setpin(){const pin=document.getElementById('newpin').value;const r=await fetch(location.pathname,{method:'POST',
headers:{'Content-Type':'application/json','X-Commons':'1'},body:JSON.stringify({set_pin:pin,code:(document.getElementById('code')||{}).value||''})});const d=await r.json();
if(d.error){document.getElementById('msg').textContent=d.error;document.getElementById('msg').className='err';return}location.reload()}
</script></body></html>"""


def render(s: Store, question_id: str, trusted: bool) -> str:
    e = html.escape
    if question_id == "pin":  # choose (or change) the approval PIN from Setup, with nothing waiting
        if not has_pin(s):
            return PAGE % {"body": '<div class="card"><h1>Approval PIN</h1></div>' + _pin_card()}
        return PAGE % {"body": '<div class="card"><h1>Approval PIN is set</h1><p class="mut">Change it with '
                               '<code>commons approvals set-pin</code> on the hub.</p></div>'}
    try:
        d = details(s, question_id)
    except CommonsError:
        return PAGE % {"body": '<div class="card"><h1>Not found</h1><p class="mut">This approval link is not valid.</p></div>'}
    rows = "".join(f"<dt>{e(k)}</dt><dd>{v}</dd>" for k, v in (
        ("Agent", e(d.get("agent") or "?")), ("Key", e(d.get("credential") or "-")),
        ("Request", f"<code>{e(d.get('method') or '')} {e(d.get('url') or '')}</code>"),
        ("Their reason", e(d.get("reason") or d["question"])), ("Why you're asked", e(d.get("why_asked") or ""))) if v)
    body = f'<div class="card"><h1>{"Approve this request?" if d["status"] in ("open", "doing", "blocked") else "Answered"}</h1>' \
           f'<p class="mut">Commons asks you because only you can allow it. A yes typed into an agent\'s chat can\'t count.</p>' \
           f'<dl>{rows}</dl>' + (f'<p class="mut">Request body</p><pre>{e(d["body"])}</pre>' if d.get("body") else "") + "</div>"
    if d["status"] not in ("open", "doing", "blocked"):
        res = d.get("result") or {}
        body += f'<div class="card"><p class="done">{e(d.get("answer") or "")}</p>' + \
                (f'<p class="mut">Ran it: HTTP {e(str(res.get("status")))}</p>' if res else "") + "</div>"
    elif not has_pin(s):
        body += _pin_card()
    else:
        pin = "" if trusted else ('<input id="pin" inputmode="numeric" autocomplete="current-password" '
                                  'placeholder="PIN" aria-label="approval PIN">')
        body += (f'<div class="card"><div class="row">{pin}<button class="ok" onclick="act(\'yes\')">Approve</button>'
                 f'<button onclick="act(\'all\')">Approve similar for 1 h</button>'
                 f'<button onclick="act(\'always\')" title="your risk: change it back on Setup">Always allow this key</button>'
                 f'<button class="bad" onclick="act(\'no\')">Deny</button></div><p id="msg" class="mut">'
                 f'{"This browser is trusted for approvals." if trusted else "Enter your PIN once; this browser is then trusted for 12 hours."}'
                 '</p></div>')
    return PAGE % {"body": body}


def _pin_card() -> str:
    return ('<div class="card"><p><b>Choose your approval PIN</b> (6 to 12 digits), once. Agents never see it; key '
            'approvals need it once per browser per 12 hours. To prove you run this hub, enter the setup code from its '
            'log: <code>docker logs commons | grep "setup code"</code>, or <code>commons approvals code</code> on the hub.'
            '</p><div class="row"><input id="code" placeholder="setup code" aria-label="setup code" style="letter-spacing:1px">'
            '<input id="newpin" inputmode="numeric" autocomplete="new-password" placeholder="new PIN" aria-label="new approval PIN">'
            '<button class="ok" onclick="setpin()">Set PIN</button></div><p id="msg"></p></div>')


def unlock(s: Store, pin: str, client: str = "") -> dict:
    """PIN -> a trusted-browser session for owner-only dashboard changes (approve-everything, the decision chain, keys)."""
    if not has_pin(s):
        raise CommonsError("set your approval PIN first: open /approve/pin")
    if not check_pin(s, pin, client):
        raise CommonsError("wrong PIN")
    return {"unlocked": True}
