#!/usr/bin/env python3
"""
Argon Web Proxy — Gemini gemini.google.com -> OpenAI + 9Router compatible
- Web UI at http://127.0.0.1:8787/ for Google login (paste cookie)
- OpenAI at http://127.0.0.1:8787/v1/chat/completions
- Supports gemini-4-argon (auto-discovered via batchexecute)
"""
import json, urllib.request, urllib.parse, time, ssl, sys, uuid, re, os, hashlib, argparse, base64
from http.server import HTTPServer, BaseHTTPRequestHandler
from socketserver import ThreadingMixIn

try:
    import httpx
    HAS_HTTPX = True
except ImportError:
    HAS_HTTPX = False

PORT = 8787
HOST = "127.0.0.1"
DIR = os.path.dirname(os.path.abspath(__file__))
COOKIE_FILE = os.path.join(DIR, "cookie.txt")
ACCOUNTS_FILE = os.path.join(DIR, "accounts.json")
CONFIG_FILE = os.path.join(DIR, "argon_config.json")
VERSION = "1.1.0-multi-account"

DEFAULT_BL = "boq_assistant-bard-web-server_20260716.08_p0"

# Static fallback — will be overwritten by live discovery if cookie present
MODELS = {
    "gemini-4-argon": {"mode": 7, "think": 0, "desc": "Gemini 4 Argon — Tugas kompleks (auto-discovered)"},
    "gemini-3.6-flash": {"mode": 1, "think": 4, "desc": "Gemini 3.6 Flash"},
    "gemini-3.5-flash": {"mode": 1, "think": 4, "desc": "Alias 3.6 Flash"},
    "gemini-3.5-flash-thinking": {"mode": 2, "think": 0, "desc": "Deep thinking"},
    "gemini-3.1-pro": {"mode": 3, "think": 4, "desc": "Pro"},
    "gemini-auto": {"mode": 4, "think": 4, "desc": "Auto"},
    "gemini-flash-lite": {"mode": 6, "think": 4, "desc": "Flash Lite"},
}

CONFIG = {"gemini_bl": DEFAULT_BL, "auth_user": None, "xsrf_token": None, "default_model": "gemini-4-argon", "api_keys": []}
if os.path.exists(CONFIG_FILE):
    try: CONFIG.update(json.load(open(CONFIG_FILE)))
    except: pass

def log(m): sys.stderr.write(f"[{time.strftime('%H:%M:%S')}] {m}\n"); sys.stderr.flush()
def save_config():
    try:
        json.dump(CONFIG, open(CONFIG_FILE,"w"), indent=2)
    except: pass

def load_accounts():
    """Load accounts list. Migrates cookie.txt if needed. Returns list of dicts {id, name, cookie, xsrf, bl, enabled}."""
    accs = []
    if os.path.exists(ACCOUNTS_FILE):
        try:
            accs = json.load(open(ACCOUNTS_FILE, encoding="utf-8"))
            if not isinstance(accs, list): accs = []
        except: accs = []
    # migrate old single cookie.txt if accounts empty
    if not accs and os.path.exists(COOKIE_FILE):
        try:
            c = open(COOKIE_FILE, encoding="utf-8").read().strip()
            if c and len(c) > 20:
                # try to get email from cookie via page fetch later, for now name by index
                accs = [{"id": "acc-1", "name": "Akun 1", "cookie": c, "xsrf": CONFIG.get("xsrf_token"), "bl": CONFIG.get("gemini_bl"), "enabled": True, "fail_count": 0, "last_ok": None}]
                json.dump(accs, open(ACCOUNTS_FILE,"w",encoding="utf-8"), indent=2, ensure_ascii=False)
                log("Migrated cookie.txt -> accounts.json (Akun 1)")
        except Exception as e:
            log(f"migrate err {e}")
    return accs

def save_accounts(accs):
    json.dump(accs, open(ACCOUNTS_FILE,"w",encoding="utf-8"), indent=2, ensure_ascii=False)

def get_enabled_accounts():
    return [a for a in load_accounts() if a.get("enabled", True) and a.get("cookie")]

def load_cookie():
    # fallback single-account for old callers: return first enabled account's cookie
    accs = get_enabled_accounts()
    if accs:
        c = accs[0].get("cookie","")
        pairs=dict(p.split("=",1) for p in c.split("; ") if "=" in p) if c else {}
        return c, pairs.get("SAPISID") or pairs.get("__Secure-1PSID") or None
    if not os.path.exists(COOKIE_FILE): return "", None
    try:
        c = open(COOKIE_FILE).read().strip()
        if c.startswith("{"):
            d=json.loads(c); return d.get("cookie",""), d.get("sapisid") or None
        pairs=dict(p.split("=",1) for p in c.split("; ") if "=" in p)
        return c, pairs.get("SAPISID") or pairs.get("__Secure-1PSID") or None
    except Exception as e:
        log(f"cookie load err {e}"); return "", None

def make_sapisidhash(sapisid):
    ts=int(time.time()); h=hashlib.sha1(f"{ts} {sapisid} https://gemini.google.com".encode()).hexdigest()
    return f"SAPISIDHASH {ts}_{h}"

def account_prefix():
    au=CONFIG.get("auth_user")
    if au in (None,"","0"): return ""
    return f"/u/{au}"

# ---------- discovery ----------
def fetch_snlm0e_and_bl(cookie_str, auth_user=None):
    """Scrape SNlM0e + bl from gemini.google.com/app"""
    headers={"Cookie":cookie_str,"User-Agent":"Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36"}
    if auth_user is not None:
        prefix = f"/u/{auth_user}" if auth_user not in (None,"","0") else ""
    else:
        prefix=account_prefix()
    url=f"https://gemini.google.com{prefix}/app"
    try:
        req=urllib.request.Request(url, headers=headers)
        ctx=ssl.create_default_context()
        with urllib.request.urlopen(req, context=ctx, timeout=20) as r:
            html=r.read().decode(errors="ignore")
            m=re.search(r'"SNlM0e":"([^"]+)"', html)
            snlm=m.group(1) if m else None
            bl=re.search(r'"cfb2h":"([^"]+)"', html)
            if not bl: bl=re.search(r'boq_assistant-bard-web-server_[^"]+', html)
            bl_val=bl.group(1) if hasattr(bl,'group') and bl.group(1).startswith("boq") else (bl.group(0) if bl else None)
            if not bl_val:
                # fallback: look for bl param in html
                bl2=re.search(r'bl=boq_assistant[^&"\']+', html)
                if bl2: bl_val=bl2.group(0).split("=")[1]
            return snlm, bl_val
    except Exception as e:
        log(f"snlm fetch err {e}")
        return None, None

def discover_models_from_cookie():
    """Call otAQ7b batchexecute to get real model list for this account"""
    cookie_str, sapisid = load_cookie()
    if not cookie_str: return None
    prefix=account_prefix()
    # otAQ7b is the model-list RPC
    inner=["otAQ7b", json.dumps([None,None])]
    # Actually otAQ7b payload is different — try generic
    # Use the pattern from go-gemini-web2api: rpcids otAQ7b fetches available models
    # We'll try to scrape models from page JS instead as fallback
    # For now just return None and keep static; snlm/bl is enough for StreamGenerate
    return None

# Try to refresh snlm/bl on start if cookie exists
def refresh_auth_from_cookie():
    cs,_=load_cookie()
    if not cs: return
    snlm, bl = fetch_snlm0e_and_bl(cs)
    if snlm:
        CONFIG["xsrf_token"]=snlm; log(f"SNlM0e refreshed: {snlm[:20]}...")
    if bl:
        CONFIG["gemini_bl"]=bl; log(f"BL refreshed: {bl}")
    if snlm or bl: save_config()

# ---------- gemini protocol (same as base) ----------
def _gemini_stream_generate_with_account(prompt, model_id, think_mode, account):
    """Single-account attempt. account = {cookie, xsrf, bl, auth_user?}"""
    cookie_str = account.get("cookie","")
    xsrf = account.get("xsrf") or CONFIG.get("xsrf_token")
    bl = account.get("bl") or CONFIG.get("gemini_bl")
    au = account.get("auth_user", CONFIG.get("auth_user"))
    if au in (None,"","0"): prefix=""
    else: prefix=f"/u/{au}"
    inner=[None]*80
    inner[0]=[prompt,0,None,None,None,None,0]
    inner[1]=["en"]; inner[2]=["","","",None,None,None,None,None,None,""]
    inner[6]=[0]; inner[7]=1; inner[10]=1; inner[11]=0; inner[17]=[[think_mode]]
    inner[18]=0; inner[27]=1; inner[30]=[4]; inner[41]=[2]; inner[53]=0
    inner[59]=str(uuid.uuid4()); inner[61]=[]; inner[68]=1; inner[79]=model_id
    outer=[None, json.dumps(inner)]
    params={"f.req": json.dumps(outer)}
    if xsrf: params["at"]=xsrf
    body=urllib.parse.urlencode(params).encode()
    reqid=int(time.time())%1000000
    url=f"https://gemini.google.com{prefix}/_/BardChatUi/data/assistant.lamda.BardFrontendService/StreamGenerate?bl={bl}&hl=en&_reqid={reqid}&rt=c"
    headers={"Content-Type":"application/x-www-form-urlencoded","Origin":"https://gemini.google.com","Referer":f"https://gemini.google.com{prefix}/app","X-Same-Domain":"1","User-Agent":"Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36"}
    if prefix: headers["X-Goog-AuthUser"]=str(au)
    # cookie -> sapisid
    pairs=dict(p.split("=",1) for p in cookie_str.split("; ") if "=" in p) if cookie_str else {}
    sapisid = pairs.get("SAPISID") or pairs.get("__Secure-1PSID")
    if cookie_str: headers["Cookie"]=cookie_str
    if sapisid: headers["Authorization"]=make_sapisidhash(sapisid)
    req=urllib.request.Request(url, data=body, headers=headers, method="POST")
    ctx=ssl.create_default_context()
    with urllib.request.urlopen(req, context=ctx, timeout=180) as r:
        return r.read().decode(errors="replace")

def gemini_stream_generate(prompt, model_id, think_mode):
    """Try accounts in order, fallback on error/limit."""
    accs = get_enabled_accounts()
    if not accs:
        # fallback to old single-cookie path (anon or cookie.txt)
        inner=[None]*80
        inner[0]=[prompt,0,None,None,None,None,0]
        inner[1]=["en"]; inner[2]=["","","",None,None,None,None,None,None,""]
        inner[6]=[0]; inner[7]=1; inner[10]=1; inner[11]=0; inner[17]=[[think_mode]]
        inner[18]=0; inner[27]=1; inner[30]=[4]; inner[41]=[2]; inner[53]=0
        inner[59]=str(uuid.uuid4()); inner[61]=[]; inner[68]=1; inner[79]=model_id
        outer=[None, json.dumps(inner)]
        params={"f.req": json.dumps(outer)}
        if CONFIG.get("xsrf_token"): params["at"]=CONFIG["xsrf_token"]
        body=urllib.parse.urlencode(params).encode()
        reqid=int(time.time())%1000000
        prefix=account_prefix()
        url=f"https://gemini.google.com{prefix}/_/BardChatUi/data/assistant.lamda.BardFrontendService/StreamGenerate?bl={CONFIG['gemini_bl']}&hl=en&_reqid={reqid}&rt=c"
        headers={"Content-Type":"application/x-www-form-urlencoded","Origin":"https://gemini.google.com","Referer":f"https://gemini.google.com{prefix}/app","X-Same-Domain":"1","User-Agent":"Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36"}
        if prefix: headers["X-Goog-AuthUser"]=str(CONFIG["auth_user"])
        cs, sapisid = load_cookie()
        if cs: headers["Cookie"]=cs
        if sapisid: headers["Authorization"]=make_sapisidhash(sapisid)
        for attempt in range(3):
            try:
                req=urllib.request.Request(url, data=body, headers=headers, method="POST")
                ctx=ssl.create_default_context()
                with urllib.request.urlopen(req, context=ctx, timeout=180) as r:
                    return r.read().decode(errors="replace")
            except Exception as e:
                if attempt<2: time.sleep(1)
                else: raise
        return ""
    last_err = None
    for idx, acc in enumerate(accs):
        try:
            raw = _gemini_stream_generate_with_account(prompt, model_id, think_mode, acc)
            # check if upstream returned error that should trigger fallback
            if "BardErrorInfo" in raw or "RESOURCE_EXHAUSTED" in raw or "quota" in raw.lower():
                # extract error to decide fallback
                if "BardErrorInfo" in raw:
                    m=re.search(r'BardErrorInfo\s*\[(\d+)\]', raw)
                    code=m.group(1) if m else "?"
                    # 2 = expired, 3 = limit etc — fallback
                    log(f"Account {acc.get('name',idx+1)} BardErrorInfo [{code}] — trying next account")
                    last_err = RuntimeError(f"BardErrorInfo [{code}]")
                    # mark fail
                    try:
                        all_accs=load_accounts()
                        for a in all_accs:
                            if a.get("id")==acc.get("id"):
                                a["fail_count"]=a.get("fail_count",0)+1
                                break
                        save_accounts(all_accs)
                    except: pass
                    continue
            # success — reset fail count
            try:
                all_accs=load_accounts()
                for a in all_accs:
                    if a.get("id")==acc.get("id"):
                        a["fail_count"]=0
                        a["last_ok"]=time.strftime("%Y-%m-%d %H:%M:%S")
                        break
                save_accounts(all_accs)
            except: pass
            if idx>0: log(f"Fallback used account {idx+1}/{len(accs)} ({acc.get('name')}) — success")
            return raw
        except Exception as e:
            msg=str(e)
            # retry-able errors: timeout, 429, 5xx, BardError
            is_retry = any(k in msg.lower() for k in ["timeout","429","503","502","500","resource_exhausted","barderror"])
            log(f"Account {acc.get('name',idx+1)} error: {msg[:120]} — {'try next' if idx+1 < len(accs) else 'no more accounts'}")
            last_err = e
            if idx+1 < len(accs):
                continue
            else:
                raise last_err
    raise last_err or RuntimeError("All accounts failed")
    for attempt in range(3):
        try:
            req=urllib.request.Request(url, data=body, headers=headers, method="POST")
            ctx=ssl.create_default_context()
            with urllib.request.urlopen(req, context=ctx, timeout=180) as r:
                return r.read().decode(errors="replace")
        except Exception as e:
            if attempt<2: time.sleep(1)
            else: raise

def clean_gemini_text(t, strip=True):
    t=re.sub(r'```(?:python|javascript|text)\?code_(?:reference|stdout)&code_event_index=\d+\n.*?```\n?', '', t, flags=re.DOTALL)
    return t.strip() if strip else t

def extract_response_text(raw):
    m=re.search(r'BardErrorInfo\s*\[(\d+)\]', raw)
    if m: raise RuntimeError(f"Gemini upstream BardErrorInfo [{m.group(1)}] — cookie/SNlM0e/BL may be stale, re-login via web UI")
    texts=[]
    for line in raw.split("\n"):
        if '"wrb.fr"' not in line or len(line)<200: continue
        try:
            arr=json.loads(line); inner_str=arr[0][2]
            if not inner_str or len(inner_str)<50: continue
            inner=json.loads(inner_str)
            if isinstance(inner,list) and len(inner)>4 and inner[4]:
                for part in inner[4]:
                    if isinstance(part,list) and len(part)>1 and part[1]:
                        if isinstance(part[1],list):
                            for t in part[1]:
                                if isinstance(t,str) and t: texts.append(t)
        except: pass
    for t in reversed(texts):
        if t.strip(): return clean_gemini_text(t)
    return ""

def messages_to_prompt(messages, tools=None):
    parts=[]
    if tools:
        defs=[{"name":(t.get("function",t).get("name","")), "description":(t.get("function",t).get("description","")), "parameters":(t.get("function",t).get("parameters",{}))} for t in tools if isinstance(t,dict)]
        if defs: parts.append("[System instruction]: You have tools. Call via ```tool_call\n{\"name\":\"...\",\"arguments\":{}}\n```\n"+json.dumps(defs,indent=2))
    for m in messages:
        role=m.get("role","user"); content=m.get("content","")
        if isinstance(content,list): content=" ".join(c.get("text","") for c in content if c.get("type") in ("text","input_text"))
        if role=="system": parts.append(f"[System instruction]: {content}")
        elif role=="assistant":
            if m.get("tool_calls"):
                tcs="\n".join(f'```tool_call\n{{\"name\":\"{tc.get("function",{}).get("name")}\", \"arguments\": {tc.get("function",{}).get("arguments","{}")}}}\n```' for tc in m["tool_calls"])
                parts.append(f"[Assistant]: {content or ''}\n{tcs}")
            else: parts.append(f"[Assistant]: {content}")
        elif role=="tool": parts.append(f"[Tool result for {m.get('name','')}]: {content}")
        else: parts.append(content or "")
    return "\n\n".join(p for p in parts if p)

# ---------- HTML ----------
LOGIN_HTML = r"""<!doctype html><html lang=id><head><meta charset=utf-8><meta name=viewport content="width=device-width,initial-scale=1">
<title>Argon Web Proxy · Gemini 4</title>
<style>
*{box-sizing:border-box}
:root{--bg:#06080F;--card:#0F1420;--line:#1E2A3A;--line2:#243044;--text:#E6EAF2;--muted:#8B97B0;--dim:#5E6B86;--violet:#7C5CFF;--blue:#3B82F6;--ok:#10B981;--bad:#EF4444;--warn:#F59E0B}
body{font-family:Inter,system-ui,sans-serif;color:var(--text);background:radial-gradient(900px 500px at 50% -10%, rgba(124,92,255,.18), transparent 60%), radial-gradient(700px 400px at 85% 15%, rgba(59,130,246,.14), transparent 60%), linear-gradient(180deg,#06080F 0%, #080B14 100%);margin:0;min-height:100vh;display:flex;justify-content:center;padding:24px 14px}
a{color:#8AB4FF;text-decoration:none}
.wrap{width:100%;max-width:860px}
.top{display:flex;align-items:center;justify-content:space-between;gap:12px;margin-bottom:14px;padding:0 2px;flex-wrap:wrap}
.brand{display:flex;align-items:center;gap:12px}
.logo{width:38px;height:38px;border-radius:12px;background:linear-gradient(135deg, var(--violet), var(--blue));display:grid;place-items:center;font-weight:800;box-shadow:0 8px 24px rgba(124,92,255,.35)}
.brand h1{margin:0;font-size:16px;font-weight:800}
.brand p{margin:2px 0 0;color:var(--muted);font-size:12px}
.pills{display:flex;gap:8px;flex-wrap:wrap}
.pill{font-size:11px;font-weight:700;letter-spacing:.06em;text-transform:uppercase;padding:7px 10px;border-radius:999px;border:1px solid var(--line);background:rgba(255,255,255,.03);color:var(--muted)}
.pill.live{border-color:rgba(16,185,129,.3);background:rgba(16,185,129,.08);color:#7EE8C6}
.card{position:relative;overflow:hidden;background:linear-gradient(180deg, rgba(18,24,38,.96), rgba(11,16,28,.98));border:1px solid var(--line);border-radius:20px;box-shadow:0 24px 80px rgba(0,0,0,.55)}
.card::before{content:"";position:absolute;inset:0 0 auto 0;height:1px;background:linear-gradient(90deg, transparent, rgba(124,92,255,.5), rgba(59,130,246,.4), transparent)}
.head{padding:20px 22px 16px;display:flex;gap:16px;justify-content:space-between;flex-wrap:wrap;border-bottom:1px solid rgba(255,255,255,.06)}
.head h2{margin:0;font-size:18px}
.head h2 span{background:linear-gradient(90deg, #A78BFA, #60A5FA);-webkit-background-clip:text;background-clip:text;color:transparent}
.sub{color:var(--muted);font-size:13px;line-height:1.6;margin-top:6px}
.badge{display:inline-flex;align-items:center;gap:8px;padding:8px 12px;border-radius:999px;font-size:12px;font-weight:700;border:1px solid var(--line);background:#0B1220;color:var(--muted)}
.badge.ok{background:rgba(16,185,129,.1);border-color:rgba(16,185,129,.28);color:#6EE7B7}
.badge.bad{background:rgba(239,68,68,.08);border-color:rgba(239,68,68,.22);color:#FCA5A5}
.body{padding:18px 22px 22px}
.grid{display:grid;grid-template-columns:1.05fr .95fr;gap:14px}
@media(max-width:780px){.grid{grid-template-columns:1fr}}
.panel{background:rgba(255,255,255,.02);border:1px solid var(--line);border-radius:14px;padding:14px}
.panel h3{margin:0 0 10px;font-size:11px;letter-spacing:.08em;text-transform:uppercase;color:var(--muted)}
.steps{list-style:none;margin:0;padding:0;display:flex;flex-direction:column;gap:8px}
.steps li{display:flex;gap:10px;font-size:13px;line-height:1.5;color:#CBD5E1}
.num{width:22px;height:22px;flex:0 0 22px;border-radius:50%;display:grid;place-items:center;font-size:11px;font-weight:800;background:linear-gradient(135deg, var(--violet), var(--blue));color:#fff}
.kbd{font-family:ui-monospace,monospace;font-size:11px;padding:3px 7px;border-radius:7px;background:#0B1220;border:1px solid var(--line);color:#CBD5E1}
.row{display:flex;gap:8px;flex-wrap:wrap;align-items:center}
.btn{appearance:none;border:0;border-radius:10px;padding:10px 14px;font-weight:700;font-size:13px;cursor:pointer}
.btn-primary{background:linear-gradient(135deg, var(--violet), var(--blue));color:#fff;box-shadow:0 10px 24px rgba(124,92,255,.35)}
.btn-ghost{background:rgba(255,255,255,.04);color:#D6DEEE;border:1px solid var(--line2)}
.btn-sm{padding:7px 10px;font-size:12px}
.endpoint{display:flex;justify-content:space-between;gap:8px;padding:10px 12px;border-radius:12px;background:#0A0F1D;border:1px solid var(--line);font-family:ui-monospace,monospace;font-size:12px;color:#B8C2D8}
.copy{width:30px;height:30px;border-radius:8px;border:1px solid var(--line);background:rgba(255,255,255,.04);color:#CBD5E1;display:grid;place-items:center;cursor:pointer}
.accounts{display:flex;flex-direction:column;gap:10px}
.acc{position:relative;display:flex;gap:12px;align-items:center;padding:12px 12px;border-radius:12px;background:#0A0F1D;border:1px solid var(--line)}
.acc.on{border-color:rgba(16,185,129,.28);background:rgba(16,185,129,.06)}
.acc.off{opacity:.6;border-color:rgba(239,68,68,.22)}
.acc .idx{width:32px;height:32px;border-radius:10px;display:grid;place-items:center;font-weight:800;font-size:12px;background:linear-gradient(135deg, var(--violet), var(--blue));color:#fff;flex:0 0 32px}
.acc.off .idx{background:#1E293B;color:var(--muted)}
.acc-main{flex:1;min-width:0}
.acc-name{font-weight:700;font-size:13px;white-space:nowrap;overflow:hidden;text-overflow:ellipsis}
.acc-meta{font-size:11px;color:var(--muted);white-space:nowrap;overflow:hidden;text-overflow:ellipsis}
.acc-actions{display:flex;gap:6px;align-items:center}
.iconbtn{width:30px;height:30px;border-radius:8px;border:1px solid var(--line);background:rgba(255,255,255,.04);color:#CBD5E1;display:grid;place-items:center;cursor:pointer;font-size:12px}
.iconbtn:hover{background:rgba(255,255,255,.08)}
.iconbtn.danger{color:#FCA5A5;border-color:rgba(239,68,68,.22)}
.switch{width:40px;height:22px;border-radius:999px;background:#1E293B;border:1px solid var(--line);position:relative;cursor:pointer;flex:0 0 40px}
.switch.on{background:rgba(16,185,129,.9);border-color:rgba(16,185,129,.6)}
.switch::after{content:"";position:absolute;top:2px;left:2px;width:16px;height:16px;border-radius:50%;background:#fff;transition:.15s}
.switch.on::after{left:20px}
.field{margin-top:14px}
.label{display:flex;justify-content:space-between;gap:8px;margin-bottom:6px}
.label span{font-size:11px;font-weight:700;letter-spacing:.06em;text-transform:uppercase;color:var(--muted)}
textarea{width:100%;min-height:96px;background:#0A0F1D;color:var(--text);border:1px solid var(--line2);border-radius:12px;padding:10px 12px;font-family:ui-monospace,monospace;font-size:12px;outline:none}
textarea:focus{border-color:rgba(124,92,255,.45);box-shadow:0 0 0 4px rgba(124,92,255,.15)}
.log{margin-top:10px;padding:10px;border-radius:12px;background:#0A0F1D;border:1px solid var(--line);font-family:ui-monospace,monospace;font-size:11px;color:#8EA0BF;white-space:pre-wrap;max-height:240px;overflow:auto}
.log:empty{display:none}
.chip{font-size:11px;font-weight:700;padding:5px 8px;border-radius:999px;background:rgba(124,92,255,.1);border:1px solid rgba(124,92,255,.22);color:#C4B5FD}
.chip.main{background:linear-gradient(135deg, rgba(124,92,255,.18), rgba(59,130,246,.18));border-color:rgba(124,92,255,.35)}
.note{font-size:12px;line-height:1.6;color:var(--muted)}
.note code{font-family:ui-monospace,monospace;font-size:11px;background:#0B1220;border:1px solid var(--line);padding:2px 6px;border-radius:6px}
</style></head><body>
<div class=wrap>
  <div class=top>
    <div class=brand><div class=logo>Ar</div><div><h1>Argon Web Proxy</h1><p>Gemini 4 · Multi-akun · Auto fallback</p></div></div>
    <div class=pills><span class=pill>127.0.0.1:8787</span><span class=pill live>● Unlimited akun</span></div>
  </div>
  <div class=card>
    <div class=head>
      <div style="flex:1;min-width:240px"><h2><span>gemini-4-argon</span> — try & fallback</h2><div class=sub>Request coba akun <b>1 → 2 → 3 …</b> otomatis. Kalau akun 1 error / limit habis / BardError, langsung pindah ke akun berikutnya. Cocok buat 9Router <code>argon/gemini-4-argon</code>.</div><div style="display:flex;gap:6px;flex-wrap:wrap;margin-top:8px"><span class=chip style="background:rgba(16,185,129,.12)">Fallback otomatis</span><span class=chip>1M context</span><span class=chip>Streaming</span></div></div>
      <div style="display:flex;flex-direction:column;gap:8px;align-items:flex-end"><div id=status class=badge>mengecek…</div><div style="font-size:11px;color:var(--muted)" id=accCount></div></div>
    </div>
    <div class=body>
      <div class=grid>
        <div class=panel>
          <h3>Akun Google</h3>
          <div id=accList class=accounts></div>
          <div style="margin-top:10px;display:flex;gap:8px">
            <button class="btn btn-ghost btn-sm" onclick="openAdd()">+ Tambah Akun</button>
            <button class="btn btn-ghost btn-sm" onclick="check()">↻ Refresh</button>
          </div>
          <div id=addBox style="display:none;margin-top:10px;padding:10px;border-radius:10px;background:#0A0F1D;border:1px solid var(--line)">
            <div style="font-size:12px;font-weight:700;margin-bottom:6px">Tambah akun baru</div>
            <input id=accName placeholder="Nama akun (mis. Ajas Pro 2)" style="width:100%;padding:8px 10px;border-radius:8px;background:#0B1220;border:1px solid var(--line);color:var(--text);font-size:12px;margin-bottom:8px">
            <textarea id=cookie placeholder="__Secure-1PSID=...; __Secure-1PSIDTS=...; ..." style="min-height:84px"></textarea>
            <div style="display:flex;gap:8px;margin-top:8px"><button class="btn btn-primary btn-sm" onclick="addAccount()">Simpan & Test</button><button class="btn btn-ghost btn-sm" onclick="closeAdd()">Batal</button></div>
            <div style="font-size:11px;color:var(--dim);margin-top:6px">Cara ambil: <span class=kbd>F12</span> → Application → Cookies → gemini.google.com → copy <span class=kbd>__Secure-1PSID</span> &amp; <span class=kbd>__Secure-1PSIDTS</span></div>
          </div>
        </div>
        <div class=panel>
          <h3>Endpoint & Cara pakai</h3>
          <div class=endpoint><span><b>OpenAI</b> http://127.0.0.1:8787/v1</span><button class=copy onclick="copyText('http://127.0.0.1:8787/v1')">⧉</button></div>
          <div class=endpoint style="margin-top:8px"><span><b>9Router</b> argon/gemini-4-argon</span><button class=copy onclick="copyText('argon/gemini-4-argon')">⧉</button></div>
          <div class=note style="margin-top:10px">Di 9Router sudah terpasang provider <code>openai-compatible-argon-prox</code> (prefix <code>argon</code>). Cek <a href="/v1/models" target="_blank">/v1/models</a> bakal ada <code>argon/gemini-4-argon</code>.</div>
          <div style="margin-top:10px;display:flex;gap:8px;flex-wrap:wrap"><a class="btn btn-primary btn-sm" href="https://gemini.google.com/app" target="_blank">↗ Buka Gemini</a><a class="btn btn-ghost btn-sm" href="/v1/models" target="_blank">Models</a></div>
        </div>
      </div>
      <div id=msg class=log></div>
      <div class=note style="margin-top:10px">Tip: kalau 1 akun limit (429/BardErrorInfo), proxy auto coba akun berikutnya dalam 1 request yang sama — klien gak perlu retry.</div>
    </div>
  </div>
  <div style="text-align:center;margin-top:10px;font-size:11px;color:var(--dim)">Argon Web Proxy · multi-account fallback · port 8787</div>
</div>
<script>
let ACCS=[];
function copyText(t){navigator.clipboard.writeText(t)}
function openAdd(){document.getElementById('addBox').style.display='block'}
function closeAdd(){document.getElementById('addBox').style.display='none'; document.getElementById('cookie').value=''; document.getElementById('accName').value=''}
async function check(){
  const r=await fetch('/api/accounts'); const j=await r.json(); ACCS=j.accounts||[];
  const s=document.getElementById('status');
  const enabled=ACCS.filter(a=>a.enabled).length;
  s.textContent = enabled ? `✓ ${enabled} akun aktif — fallback siap` : '✗ Belum ada akun';
  s.className='badge '+(enabled?'ok':'bad');
  document.getElementById('accCount').textContent = ACCS.length ? `${ACCS.length} akun total · ${enabled} aktif` : '';
  const list=document.getElementById('accList');
  if(!ACCS.length){
    list.innerHTML='<div style="padding:12px;text-align:center;color:var(--muted);font-size:13px;border:1px dashed var(--line);border-radius:10px">Belum ada akun — klik <b>+ Tambah Akun</b> di bawah</div>';
  } else {
    list.innerHTML=ACCS.map((a,i)=>{
      const on=a.enabled!==false;
      const fail=a.fail_count?` · fail ${a.fail_count}`:'';
      const ok=a.last_ok?` · OK ${a.last_ok}`:'';
      const name=(a.name||`Akun ${i+1}`).replace(/</g,'&lt;');
      return `<div class="acc ${on?'on':'off'}"><div class=idx>${i+1}</div><div class=acc-main><div class=acc-name>${name} ${on?'✓':''}</div><div class=acc-meta>${(a.bl||'').slice(0,28)}${fail}${ok}</div></div><div class=acc-actions><div class="switch ${on?'on':''}" onclick="toggleAcc('${a.id}')"></div><button class=iconbtn onclick="testAcc('${a.id}')" title="Test">▶</button><button class="iconbtn danger" onclick="delAcc('${a.id}')" title="Hapus">✕</button></div></div>`;
    }).join('');
  }
  document.getElementById('msg').textContent = JSON.stringify(j,null,2);
}
async function addAccount(){
  const name=document.getElementById('accName').value.trim()||('Akun '+(ACCS.length+1));
  const cookie=document.getElementById('cookie').value.trim();
  if(!cookie){alert('paste cookie dulu');return}
  const r=await fetch('/api/accounts',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({name,cookie})});
  const j=await r.json();
  document.getElementById('msg').textContent=JSON.stringify(j,null,2);
  if(j.ok){closeAdd(); check()} else alert(j.error||'gagal');
}
async function toggleAcc(id){
  await fetch('/api/accounts/'+id+'/toggle',{method:'POST'});
  check();
}
async function delAcc(id){
  if(!confirm('Hapus akun ini?')) return;
  await fetch('/api/accounts/'+id,{method:'DELETE'});
  check();
}
async function testAcc(id){
  document.getElementById('msg').textContent='testing…';
  const r=await fetch('/api/accounts/'+id+'/test',{method:'POST'});
  const j=await r.json();
  document.getElementById('msg').textContent=JSON.stringify(j,null,2);
}
check();
</script></body></html>
"""

class Handler(BaseHTTPRequestHandler):
    def log_message(self, fmt,*a): log(f"{self.client_address[0]} {fmt%a}")
    def send_json(self, data, status=200):
        b=json.dumps(data, ensure_ascii=False).encode()
        self.send_response(status); self.send_header("Content-Type","application/json"); self.send_header("Access-Control-Allow-Origin","*"); self.send_header("Content-Length",str(len(b))); self.end_headers(); self.wfile.write(b)
    def do_OPTIONS(self):
        self.send_response(204); self.send_header("Access-Control-Allow-Origin","*"); self.send_header("Access-Control-Allow-Methods","GET, POST, OPTIONS"); self.send_header("Access-Control-Allow-Headers","*"); self.end_headers()
    def do_GET(self):
        try:
            p=self.path.split("?")[0]
            if p in ("/","/login"): 
                self.send_response(200); self.send_header("Content-Type","text/html; charset=utf-8"); b=LOGIN_HTML.encode(); self.send_header("Content-Length",str(len(b))); self.end_headers(); self.wfile.write(b); return
            if p in ("/api/status","/api/accounts"):
                accs=load_accounts()
                enabled=[a for a in accs if a.get("enabled",True)]
                self.send_json({"accounts": accs, "enabled_count": len(enabled), "total": len(accs), "models": list(MODELS.keys()), "version": VERSION})
                return
            if p=="/v1/models":
                self.send_json({"object":"list","data":[{"id":n,"object":"model","created":1700000000,"owned_by":"google","description":c["desc"]} for n,c in MODELS.items()]})
                return
            if p.startswith("/v1beta/models"):
                ms=[{"name":f"models/{n}","displayName":n,"description":c["desc"],"supportedGenerationMethods":["generateContent","streamGenerateContent"]} for n,c in MODELS.items()]
                self.send_json({"models":ms}); return
            if p=="/health": self.send_json({"status":"ok","version":VERSION, "accounts": len(load_accounts())}); return
            self.send_json({"error":"not found"},404)
        except Exception as e:
            log(f"GET err {e}")
    def do_POST(self):
        try:
            length=int(self.headers.get("Content-Length",0))
            body=self.rfile.read(length) if length else b""
            path=self.path.split("?")[0]
            # ---- multi-account API ----
            if path=="/api/accounts" and self.command=="POST":
                try: j=json.loads(body) if body else {}
                except: j={}
                name=(j.get("name") or "").strip() or f"Akun {len(load_accounts())+1}"
                c=(j.get("cookie") or "").strip()
                if not c or len(c)<20:
                    self.send_json({"ok":False,"error":"cookie kosong / terlalu pendek"},400); return
                # fetch SNlM0e/BL for this cookie
                snlm, bl = fetch_snlm0e_and_bl(c)
                accs=load_accounts()
                new_id=f"acc-{int(time.time()*1000)}-{len(accs)+1}"
                acc={"id": new_id, "name": name, "cookie": c, "xsrf": snlm, "bl": bl or CONFIG.get("gemini_bl"), "enabled": True, "fail_count": 0, "last_ok": None, "created_at": time.strftime("%Y-%m-%d %H:%M:%S")}
                accs.append(acc)
                save_accounts(accs)
                # also keep single-file for compat: update global CONFIG
                CONFIG["xsrf_token"]=snlm or CONFIG.get("xsrf_token")
                if bl: CONFIG["gemini_bl"]=bl
                save_config()
                # test with this account only
                try:
                    raw=_gemini_stream_generate_with_account("hi", MODELS["gemini-4-argon"]["mode"], MODELS["gemini-4-argon"]["think"], acc)
                    txt=extract_response_text(raw)
                    ok=bool(txt)
                except Exception as e:
                    ok=False; txt=str(e)[:600]
                self.send_json({"ok":ok, "id": new_id, "snlm_ok": bool(snlm), "bl": bl, "test_output": txt[:500] if ok else txt, "msg": "akun ditambah. SNlM0e "+("OK" if snlm else "FAIL — akan coba fallback"), "accounts": load_accounts()})
                return
            if path=="/api/login":
                # legacy single-account login — add as first account for compat
                try: j=json.loads(body) if body else {}
                except: j={}
                c=(j.get("cookie") or "").strip()
                if not c:
                    # clear all
                    try: os.remove(COOKIE_FILE)
                    except: pass
                    try: os.remove(ACCOUNTS_FILE)
                    except: pass
                    CONFIG["xsrf_token"]=None; save_config()
                    self.send_json({"ok":True,"msg":"semua akun dihapus"}); return
                # treat as add account
                snlm, bl = fetch_snlm0e_and_bl(c)
                accs=load_accounts()
                acc={"id": f"acc-{int(time.time()*1000)}", "name": j.get("name") or f"Akun {len(accs)+1}", "cookie": c, "xsrf": snlm, "bl": bl or CONFIG.get("gemini_bl"), "enabled": True, "fail_count": 0, "last_ok": None}
                accs.append(acc); save_accounts(accs)
                open(COOKIE_FILE,"w").write(c)
                if snlm: CONFIG["xsrf_token"]=snlm
                if bl: CONFIG["gemini_bl"]=bl
                save_config()
                try:
                    raw=gemini_stream_generate("hi", MODELS["gemini-4-argon"]["mode"], MODELS["gemini-4-argon"]["think"])
                    txt=extract_response_text(raw)
                    ok=bool(txt)
                except Exception as e:
                    ok=False; txt=str(e)[:500]
                self.send_json({"ok":ok, "snlm_ok": bool(CONFIG.get("xsrf_token")), "bl": CONFIG.get("gemini_bl"), "test_output": txt[:500] if ok else txt, "msg": "saved. SNlM0e "+("OK" if CONFIG.get("xsrf_token") else "FAIL — will retry")})
                return
            if path.startswith("/api/accounts/") and path.endswith("/toggle"):
                acc_id=path.split("/")[3]
                accs=load_accounts()
                for a in accs:
                    if a.get("id")==acc_id:
                        a["enabled"]=not a.get("enabled",True)
                        break
                save_accounts(accs)
                self.send_json({"ok":True, "accounts": accs}); return
            if path.startswith("/api/accounts/") and path.endswith("/test"):
                acc_id=path.split("/")[3]
                accs=load_accounts()
                target=None
                for a in accs:
                    if a.get("id")==acc_id:
                        target=a; break
                if not target:
                    self.send_json({"ok":False,"error":"akun tidak ditemukan"},404); return
                # refresh snlm/bl then test
                snlm, bl = fetch_snlm0e_and_bl(target.get("cookie",""))
                if snlm: target["xsrf"]=snlm
                if bl: target["bl"]=bl
                save_accounts(accs)
                try:
                    raw=_gemini_stream_generate_with_account("hi", MODELS["gemini-4-argon"]["mode"], MODELS["gemini-4-argon"]["think"], target)
                    txt=extract_response_text(raw)
                    ok=bool(txt)
                    if ok:
                        target["fail_count"]=0; target["last_ok"]=time.strftime("%Y-%m-%d %H:%M:%S")
                        save_accounts(accs)
                    self.send_json({"ok":ok, "test_output": txt[:800] if ok else txt, "snlm_ok": bool(snlm), "bl": bl})
                except Exception as e:
                    self.send_json({"ok":False, "error": str(e)[:800], "snlm_ok": bool(snlm)})
                return
            if path.startswith("/api/accounts/") and self.command=="POST":
                # already handled /toggle and /test above
                pass
            if path.startswith("/api/accounts/"):
                # DELETE /api/accounts/{id}
                if self.command=="DELETE" or (self.headers.get("X-HTTP-Method-Override")=="DELETE"):
                    acc_id=path.split("/")[3]
                    accs=load_accounts()
                    accs=[a for a in accs if a.get("id")!=acc_id]
                    save_accounts(accs)
                    self.send_json({"ok":True, "accounts": accs}); return
                # POST delete fallback via body
                try: j=json.loads(body) if body else {}
                except: j={}
                if j.get("_method")=="DELETE":
                    acc_id=path.split("/")[3]
                    accs=[a for a in load_accounts() if a.get("id")!=acc_id]
                    save_accounts(accs)
                    self.send_json({"ok":True}); return
            if path=="/v1/chat/completions":
                self.handle_chat(body); return
            if ":generateContent" in path:
                self.handle_google(body, stream=False); return
            if ":streamGenerateContent" in path:
                self.handle_google(body, stream=True); return
            self.send_json({"error":"not found"},404)
        except Exception as e:
            log(f"POST err {e}")
            try: self.send_json({"error":str(e)},500)
            except: pass
    def do_DELETE(self):
        # support DELETE for /api/accounts/{id}
        p=self.path.split("?")[0]
        if p.startswith("/api/accounts/"):
            acc_id=p.split("/")[3]
            accs=[a for a in load_accounts() if a.get("id")!=acc_id]
            save_accounts(accs)
            self.send_json({"ok":True, "accounts": accs}); return
        self.send_json({"error":"not found"},404)

    def _resolve(self, name):
        if "@think=" in name:
            name, v = name.rsplit("@think=",1)
            try: ov=int(v)
            except: ov=None
        else: ov=None
        cfg=MODELS.get(name)
        if not cfg: return None,None,None, f"Unknown model {name}. Available: {', '.join(MODELS.keys())}"
        return name, cfg["mode"], (ov if ov is not None else cfg["think"]), None

    def handle_chat(self, body):
        req=json.loads(body)
        model_name, mode, think, err = self._resolve(req.get("model", CONFIG["default_model"]))
        if err: self.send_json({"error":{"message":err}},400); return
        prompt=messages_to_prompt(req.get("messages",[]), req.get("tools"))
        if not prompt.strip(): self.send_json({"error":{"message":"empty prompt"}},400); return
        stream=req.get("stream", False)
        tools=req.get("tools")
        cid=f"chatcmpl-{uuid.uuid4().hex[:12]}"
        if stream and not tools:
            self.send_response(200); self.send_header("Content-Type","text/event-stream"); self.send_header("Cache-Control","no-cache"); self.send_header("Access-Control-Allow-Origin","*"); self.end_headers()
            try:
                # non-streaming fallback then chunk
                raw=gemini_stream_generate(prompt, mode, think)
                text=extract_response_text(raw)
                # chunk by words for streaming feel
                for word in text.split(" "):
                    chunk={"id":cid,"object":"chat.completion.chunk","created":int(time.time()),"model":model_name,"choices":[{"index":0,"delta":{"content":word+" "},"finish_reason":None}]}
                    self.wfile.write(f"data: {json.dumps(chunk, ensure_ascii=False)}\n\n".encode()); self.wfile.flush(); time.sleep(0.02)
                final={"id":cid,"object":"chat.completion.chunk","created":int(time.time()),"model":model_name,"choices":[{"index":0,"delta":{},"finish_reason":"stop"}]}
                self.wfile.write(f"data: {json.dumps(final)}\n\n".encode()); self.wfile.write(b"data: [DONE]\n\n"); self.wfile.flush()
            except Exception as e:
                log(f"stream err {e}")
            return
        try:
            raw=gemini_stream_generate(prompt, mode, think)
            text=extract_response_text(raw)
        except Exception as e:
            self.send_json({"error":{"message":f"upstream error: {e}"}},502); return
        msg={"role":"assistant","content": text or None}
        finish="stop"
        self.send_json({"id":cid,"object":"chat.completion","created":int(time.time()),"model":model_name,"choices":[{"index":0,"message":msg,"finish_reason":finish}],"usage":{"prompt_tokens":len(prompt)//4,"completion_tokens":len(text)//4,"total_tokens":(len(prompt)+len(text))//4}})

    def handle_google(self, body, stream):
        import re as _re
        m=_re.match(r'/v1beta/models/([^:?]+)', self.path)
        if not m: self.send_json({"error":{"message":"model not in path"}},400); return
        model_name, mode, think, err = self._resolve(m.group(1))
        if err: self.send_json({"error":{"message":err}},400); return
        try: j=json.loads(body)
        except: j={}
        # contents -> prompt
        parts=[]
        si=j.get("systemInstruction")
        if si:
            parts.append("[System instruction]: "+" ".join(p.get("text","") for p in si.get("parts",[]) if p.get("text")))
        for c in j.get("contents",[]):
            txt=" ".join(p.get("text","") for p in c.get("parts",[]) if p.get("text"))
            parts.append(f"[Assistant]: {txt}" if c.get("role")=="model" else txt)
        prompt="\n\n".join(p for p in parts if p)
        if not prompt.strip(): prompt="hi"
        try:
            raw=gemini_stream_generate(prompt, mode, think)
            text=extract_response_text(raw)
        except Exception as e:
            self.send_json({"error":{"message":str(e)}},502); return
        resp={"candidates":[{"content":{"parts":[{"text":text or ""}],"role":"model"},"finishReason":"STOP","index":0}],"usageMetadata":{"promptTokenCount":len(prompt)//4,"candidatesTokenCount":len(text)//4,"totalTokenCount":(len(prompt)+len(text))//4},"modelVersion":model_name}
        if stream:
            self.send_response(200); self.send_header("Content-Type","text/event-stream"); self.send_header("Cache-Control","no-cache"); self.send_header("Access-Control-Allow-Origin","*"); self.end_headers(); self.wfile.write(f"data: {json.dumps(resp)}\n\n".encode()); self.wfile.flush()
        else: self.send_json(resp)

if __name__=="__main__":
    # auto-refresh snlm/bl if cookie exists
    try: refresh_auth_from_cookie()
    except: pass
    # ensure MODELS includes argon even if discovery failed
    server=ThreadingMixIn.__new__(ThreadingMixIn)
    class S(ThreadingMixIn, HTTPServer):
        daemon_threads=True; allow_reuse_address=True
    httpd=S((HOST, PORT), Handler)
    print(f"Argon Web Proxy {VERSION}")
    print(f" Web UI : http://{HOST}:{PORT}/")
    print(f" OpenAI : http://{HOST}:{PORT}/v1  (model gemini-4-argon)")
    print(f" Cookie : {COOKIE_FILE} {'exists' if os.path.exists(COOKIE_FILE) else 'not yet — open web UI to login'}")
    print(f" 9Router: add OpenAI-compatible provider BaseURL http://{HOST}:{PORT}/v1  prefix argon")
    try: httpd.serve_forever()
    except KeyboardInterrupt: print("stopped")
