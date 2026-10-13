"""Approvals only the owner can give, from wherever they are, in one step.

When the decision chain hands a credential use (or an upstream MCP call) to the user, the agent gets an approval link
(/approve/<question>) to show the user. The page says exactly what is asked: the key, the method, the URL, the request
body and the reason. Anyone can open it, so an agent can pass it on, but approving needs proof that the owner is
there: the owner password. The owner chooses it on their first visit to the dashboard: for 30 minutes after Commons
starts (or after `commons approvals reset`) the first visitor claims the hub, with no code, from any browser. After that
the browser stays signed in for 14 days through an HttpOnly cookie an agent never sees, and the owner can lock the
whole dashboard behind the same password (Setup: dashboard_lock). Phone and email links stay
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

SESSION_S = 14 * 24 * 3600
# how long a fresh (or reset) hub can be claimed without a code; 0 = only with the setup code from the hub's log
CLAIM_S = int(os.environ.get("COMMONS_CLAIM_MINUTES", "30")) * 60
LOCK_AFTER, LOCK_S = 5, 900
QUESTION = re.compile(r"\[(vault|mcp) ([A-Za-z0-9_]+)\]")


def _get(s: Store, key: str):
    r = s.q("SELECT value FROM meta WHERE key=?", (key,))
    return json.loads(r[0]["value"]) if r else None


def _put(s: Store, key: str, value) -> None:
    s.x("INSERT OR REPLACE INTO meta VALUES(?,?)", (key, json.dumps(value)))


def has_pin(s: Store) -> bool:
    return bool(_get(s, "approval_pin"))


def open_claim(s: Store) -> None:
    """Commons just started (or the owner reset the password on the hub): the first visitor may choose it for a while."""
    if not has_pin(s):
        _put(s, "approval_claim", {"until": now() + CLAIM_S})


def claim_left(s: Store) -> int:
    """Seconds left to claim this hub without a code (0 once a password exists or the window has closed)."""
    return 0 if has_pin(s) else max(0, int((_get(s, "approval_claim") or {}).get("until", 0) - now()))


def info(s: Store) -> dict:
    from . import settings
    rec = _get(s, "approval_pin") or {}
    return {"set": bool(rec), "at": rec.get("at"), "from": rec.get("from"), "claim_left_s": claim_left(s),
            "claim_minutes": CLAIM_S // 60,
            "locked": bool(settings.get(s, "dashboard_lock")) and bool(rec)}


def reset(s: Store) -> dict:
    """On the hub only: forget the owner password and open a new claim window."""
    s.x("DELETE FROM meta WHERE key='approval_pin'")
    open_claim(s)
    s.audit(None, "approvals.pin_reset", "host", {})
    return {"reset": True, "claim_minutes": CLAIM_S // 60}


def setup_code(s: Store, rotate: bool = False) -> str:
    """A one-time code for choosing the first owner password (a headless fallback for the claim window). It is printed in the hub's own log at startup and by
    `commons approvals code` on the hub, never sent over the API, so an agent that can only reach Commons can't use it."""
    rec = _get(s, "approval_setup")
    if rec and not rotate and rec.get("code"):
        return rec["code"]
    code = f"{secrets.randbelow(10**4):04d}-{secrets.randbelow(10**4):04d}"
    _put(s, "approval_setup", {"code": code, "at": iso(now())})
    return code


def set_pin(s: Store, pin: str, current: str = "", code: str = "", by: str = "owner", client: str = "") -> dict:
    """The owner password. The first one: anyone during the claim window, else the setup code from the hub's log.
    Changing it needs the current one. On the hub itself (`commons approvals set-pin`, by="host") neither is needed."""
    if not 6 <= len(pin or "") <= 128:
        raise CommonsError("the owner password needs at least 6 characters")
    if by != "host":
        if has_pin(s):
            if not check_pin(s, current, client):
                raise CommonsError("enter the current password to change it (or run `commons approvals reset` on the hub)")
        elif not claim_left(s):
            rec = _get(s, "approval_setup") or {}
            if not rec.get("code") or not hmac.compare_digest(rec["code"], (code or "").strip()):
                _fail(s, client)
                raise CommonsError("the 30 minutes to claim this hub have passed: restart Commons and set the password "
                                   "within 30 minutes, or enter the setup code from the hub's log")
    s.x("DELETE FROM meta WHERE key IN ('approval_setup','approval_claim')")  # spent once a password exists
    salt = secrets.token_hex(16)
    first = not has_pin(s)
    _put(s, "approval_pin", {"salt": salt, "hash": _hash(pin, salt), "at": iso(now()), "by": by, "from": client or by})
    s.audit(None, "approvals.pin_set", by, {"from": client})
    if first and by != "host":  # a claim that wasn't the owner must not go unnoticed
        from .escalation import notify
        from .phone import push
        msg = (f"The owner password was set from {client or 'a browser'}. Not you? Run `commons approvals reset` on the "
               "hub (docker exec commons commons approvals reset) and choose a new one.")
        notify(s, "Commons now has an owner password", msg, level="info")
        push(s, "Commons owner password set", msg)
    return {"pin_set": True}


def _hash(pin: str, salt: str) -> str:
    return hashlib.pbkdf2_hmac("sha256", pin.encode(), salt.encode(), 200_000).hex()


def check_pin(s: Store, pin: str, client: str = "") -> bool:
    """Wrong tries lock that device (client address) for 15 minutes, not everyone: a stranger on the LAN can't lock the
    owner out. A trusted browser (session cookie) and phone links don't need the password at all."""
    rec, lock = _get(s, "approval_pin"), _get(s, f"approval_lock:{client}") or {"fails": 0, "until": 0}
    if not rec or not pin:
        return False
    if lock["until"] > now():
        raise CommonsError(f"too many wrong passwords: try again in {int(lock['until'] - now()) // 60 + 1} min")
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
    pin = (_get(s, "approval_pin") or {}).get("hash", "")[:32]  # bound to the password: changing it logs every browser out
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
        raise CommonsError("set your owner password first (the box on this page)")
    if not (session_ok(s, session) or check_pin(s, pin, client)):
        raise CommonsError("wrong password")
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
button.bad{color:var(--bad)}input{min-height:44px;font-size:16px;padding:0 12px;border-radius:10px;border:1px solid var(--line);
background:var(--bg);color:var(--ink);flex:1 1 220px;min-width:0}.done{color:var(--ok);font-weight:600}.err{color:var(--bad)}
</style></head><body><main>%(body)s</main><script>
async function act(choice){const pin=(document.getElementById('pin')||{}).value||'';const r=await fetch(location.pathname,{method:'POST',
headers:{'Content-Type':'application/json','X-Commons':'1'},body:JSON.stringify({choice,pin})});const d=await r.json();
if(d.error){document.getElementById('msg').textContent=d.error;document.getElementById('msg').className='err';return}location.reload()}
async function setpin(){const pin=document.getElementById('newpin').value;const r=await fetch(location.pathname,{method:'POST',
headers:{'Content-Type':'application/json','X-Commons':'1'},body:JSON.stringify({set_pin:pin,code:(document.getElementById('code')||{}).value||''})});const d=await r.json();
if(d.error){document.getElementById('msg').textContent=d.error;document.getElementById('msg').className='err';return}location.reload()}
async function signin(){const pin=document.getElementById('pin').value;const r=await fetch('/approve/unlock',{method:'POST',
headers:{'Content-Type':'application/json','X-Commons':'1'},body:JSON.stringify({pin})});const d=await r.json();
if(d.error){document.getElementById('msg').textContent=d.error;document.getElementById('msg').className='err';return}location.href='/'}
</script></body></html>"""


def render(s: Store, question_id: str, trusted: bool) -> str:
    e = html.escape
    if question_id == "pin":  # choose the owner password, with nothing waiting
        if not has_pin(s):
            return PAGE % {"body": _pin_card(s, "<h1>Make this hub yours</h1>")}
        return PAGE % {"body": '<div class="card"><h1>The owner password is set</h1><p class="mut">Forgot it? Run '
                               '<code>commons approvals reset</code> on the hub (in Docker: <code>docker exec commons '
                               'commons approvals reset</code>), then choose a new one here within 30 minutes.</p></div>'}
    if question_id == "login":  # the owner locked the dashboard
        return PAGE % {"body": '<div class="card"><h1>Commons is locked</h1><p class="mut">Sign in with the owner '
                               'password. This browser then stays signed in for 14 days.</p><div class="row">'
                               '<input id="pin" type="password" autocomplete="current-password" placeholder="owner password" '
                               'aria-label="owner password" onkeydown="if(event.key===\'Enter\')signin()" autofocus>'
                               '<button class="ok" onclick="signin()">Sign in</button></div><p id="msg"></p></div>'}
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
        body += _pin_card(s)
    else:
        pin = "" if trusted else ('<input id="pin" type="password" autocomplete="current-password" '
                                  'placeholder="owner password" aria-label="owner password">')
        body += (f'<div class="card"><div class="row">{pin}<button class="ok" onclick="act(\'yes\')">Approve</button>'
                 f'<button onclick="act(\'all\')">Approve similar for 1 h</button>'
                 f'<button onclick="act(\'always\')" title="your risk: change it back on Setup">Always allow this key</button>'
                 f'<button class="bad" onclick="act(\'no\')">Deny</button></div><p id="msg" class="mut">'
                 f'{"You are signed in on this browser." if trusted else "Enter your password once; this browser then stays signed in for 14 days."}'
                 '</p></div>')
    return PAGE % {"body": body}


def _pin_card(s: Store, head: str = "") -> str:
    pw = ('<input id="newpin" type="password" autocomplete="new-password" placeholder="owner password (6+ characters)" '
          'aria-label="owner password">')
    if claim_left(s):  # fresh hub: the first visitor claims it, from any browser
        return (f'<div class="card">{head}<p><b>Choose your owner password.</b> Only the owner can approve agents\' key use and '
                f'change keys or settings. Anyone can claim this hub for {claim_left(s) // 60 + 1} more minutes, so set it '
                f'now.</p><div class="row">{pw}<button class="ok" onclick="setpin()">Set password</button></div>'
                '<p id="msg"></p></div>')
    return (f'<div class="card">{head}<p><b>Choose your owner password.</b> The 30 minutes to claim this hub have passed. '
            'Restart Commons (Container Manager, or <code>docker restart commons</code>) and open this page again '
            'within 30 minutes. Running it headless? Enter the setup code from its log instead.</p>'
            f'<div class="row"><input id="code" placeholder="setup code (optional)" aria-label="setup code">{pw}'
            '<button class="ok" onclick="setpin()">Set password</button></div><p id="msg"></p></div>')


def unlock(s: Store, pin: str, client: str = "") -> dict:
    """Owner password -> a trusted-browser session for owner-only dashboard changes (approve-everything, the decision chain, keys)."""
    if not has_pin(s):
        raise CommonsError("set your owner password first: open /approve/pin")
    if not check_pin(s, pin, client):
        raise CommonsError("wrong password")
    return {"unlocked": True}
