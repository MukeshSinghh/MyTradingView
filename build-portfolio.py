#!/usr/bin/env python3
"""
build-portfolio.py - turn your Google Sheet into passphrase-encrypted portfolio
dashboards (responsive desktop + mobile-first edition).

USAGE
    pip install openpyxl cryptography
    python build-portfolio.py --password "YourSecretPassphrase"
    python build-portfolio.py --xlsx MyBook.xlsx --password "YourSecretPassphrase"
    python build-portfolio.py --open

You can also set the environment variable PORTFOLIO_PASS instead of passing --password,
or omit both to be prompted securely in the terminal.
"""

import argparse, base64, datetime as dt, getpass, io, json, os, secrets, subprocess, sys, urllib.error, urllib.request, webbrowser
from pathlib import Path

try:
    import openpyxl
except ImportError:
    sys.exit("This script needs openpyxl.  Run:  pip install openpyxl")

try:
    from cryptography.hazmat.primitives import hashes
    from cryptography.hazmat.primitives.ciphers.aead import AESGCM
    from cryptography.hazmat.primitives.kdf.pbkdf2 import PBKDF2HMAC
except ImportError:
    sys.exit("This script needs cryptography for passphrase encryption.  Run:  pip install cryptography")

# --------------------------------------------------------------------------- #
# CONFIG - edit this part
# --------------------------------------------------------------------------- #
SHEET_ID = "1TIkGclnHEn4ZRqV4-NySWWcrlAZ8NwlY8hN8tw3MVoI"

ACCOUNTS = [
    dict(id="dhan-me",     label="Dhan",     sub="Yours",  fmt="dhan",   tabs=["Dhan-M"]),
    dict(id="dhan-wife",   label="Dhan",     sub="Wifey",  fmt="dhan",   tabs=["Dhan-H"]),
    dict(id="gullak-me",   label="Gullak",   sub="Yours",  fmt="gullak", tabs=["Gullak-M"]),
    dict(id="gullak-wife", label="Gullak",   sub="Wifey",  fmt="gullak", tabs=["Gullak-H"]),
    #dict(id="indmoney",    label="INDmoney", sub="",       fmt="dhan",   tabs=["INDMoney"]),
    #dict(id="groww",       label="Groww",    sub="",       fmt="dhan",   tabs=["Grow"]),
]

UNLISTED = {"MSEI"}


# --------------------------------------------------------------------------- #
# Encryption helper (PBKDF2-HMAC-SHA256 + AES-256-GCM)
# --------------------------------------------------------------------------- #
def encrypt_payload(json_str: str, password: str) -> dict:
    salt = secrets.token_bytes(16)
    iv = secrets.token_bytes(12)
    kdf = PBKDF2HMAC(
        algorithm=hashes.SHA256(),
        length=32,
        salt=salt,
        iterations=250_000,
    )
    key = kdf.derive(password.encode("utf-8"))
    ct = AESGCM(key).encrypt(iv, json_str.encode("utf-8"), None)
    return {
        "encrypted": True,
        "salt": base64.b64encode(salt).decode("ascii"),
        "iv": base64.b64encode(iv).decode("ascii"),
        "ct": base64.b64encode(ct).decode("ascii"),
    }


# --------------------------------------------------------------------------- #
# Loading the workbook
# --------------------------------------------------------------------------- #
def download_workbook(sheet_id):
    url = f"https://docs.google.com/spreadsheets/d/{sheet_id}/export?format=xlsx"
    req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
    try:
        blob = urllib.request.urlopen(req, timeout=60).read()
    except urllib.error.HTTPError as e:
        sys.exit(
            f"Google refused the download (HTTP {e.code}). Set the sheet to 'Anyone with the link can view', "
            "or download it via File > Download > Microsoft Excel and run with --xlsx FILE."
        )
    if not blob.startswith(b"PK"):
        sys.exit(
            "The sheet is private, so Google returned a login page. Share it as 'Anyone with the link can view', "
            "or download it manually and run with --xlsx FILE."
        )
    return io.BytesIO(blob)


# --------------------------------------------------------------------------- #
# Parsing helpers
# --------------------------------------------------------------------------- #
num = lambda v: isinstance(v, (int, float)) and not isinstance(v, bool)


def fix_misdated(rows, tab):
    for i in range(1, len(rows) - 1):
        d, prev, nxt = rows[i][0], rows[i - 1][0], rows[i + 1][0]
        if d < min(prev, nxt) - dt.timedelta(days=90):
            try:
                cand = d.replace(year=d.year + 1)
            except ValueError:
                continue
            if prev <= cand <= nxt:
                print(f"  note [{tab}]: {d} sits between {prev} and {nxt}; treating it as {cand}.")
                rows[i][0] = cand
    return rows


def read_transactions(ws, last_col, tab, skip_summary=True):
    rows = []
    for r in ws.iter_rows(min_row=2, max_col=last_col, values_only=True):
        d, amt, note = r[0], r[1], r[last_col - 1]
        if not isinstance(d, (dt.datetime, dt.date)) or not num(amt):
            continue
        if skip_summary and str(note or "").strip().lower().startswith("sum("):
            continue
        rows.append([d.date() if isinstance(d, dt.datetime) else d, float(amt), str(note or "")])
    rows = fix_misdated(rows, tab)
    rows.sort(key=lambda t: t[0])
    return [[d.isoformat(), a, n] for d, a, n in rows]


def parse_dhan(ws, acc_id, tab):
    tx = read_transactions(ws, 4, tab)
    holdings = []
    for r in ws.iter_rows(min_row=2, min_col=9, max_col=16, values_only=True):
        acct, share, qty, buy, cur, _pl, bal, inv = r
        if not (isinstance(acct, str) and isinstance(share, str) and num(bal) and num(inv)):
            continue
        qty = float(qty) if num(qty) else 0.0
        a = acct.upper()
        kind = (
            "ETF" if "ETF" in a
            else "Mutual fund" if "MF" in a
            else "Unlisted" if share.replace("NSE:", "").upper() in UNLISTED
            else "Stock"
        )
        name = share.replace("NSE:", "")
        if kind == "Unlisted":
            name += " (unlisted)"
        mf = kind == "Mutual fund"
        holdings.append(
            dict(
                a=acc_id, n=name, c=kind,
                q=None if mf else qty,
                b=None if mf or not num(buy) else buy,
                p=None if mf or not num(cur) else cur,
                i=float(inv), v=float(bal),
                closed=qty <= 0 and not mf,
            )
        )
    return tx, holdings


def parse_gullak(ws, acc_id, tab):
    tx = read_transactions(ws, 3, tab, skip_summary=False)
    holdings = []
    for r in ws.iter_rows(min_row=2, min_col=8, max_col=16, values_only=True):
        acct, share, _q, _b, _c, _pl, bal, inv = r[:8]
        if not (isinstance(acct, str) and isinstance(share, str) and num(bal) and num(inv)):
            continue
        s = share.lower()
        kind = "Digital gold" if "gold" in s else "Digital silver" if "silver" in s else "Digital gold/silver"
        holdings.append(
            dict(a=acc_id, n=share, c=kind, q=None, b=None, p=None, i=float(inv), v=float(bal), closed=False)
        )
    return tx, holdings


PARSERS = {"dhan": parse_dhan, "gullak": parse_gullak}


# --------------------------------------------------------------------------- #
# Build
# --------------------------------------------------------------------------- #
def build(wb):
    sheets = {ws.title.strip().lower(): ws for ws in wb.worksheets}
    accounts, holdings = [], []
    for cfg in ACCOUNTS:
        ws = next((sheets[t.lower()] for t in cfg["tabs"] if t.lower() in sheets), None)
        entry = dict(id=cfg["id"], label=cfg["label"], sub=cfg["sub"], tx=[], note="")
        if ws is None:
            entry["note"] = f"No tab named “{cfg['tabs'][0]}” was found in the workbook."
            print(f"  skipped {cfg['label']} {cfg['sub']}: tab '{cfg['tabs'][0]}' not found")
        else:
            tx, hs = PARSERS[cfg["fmt"]](ws, cfg["id"], ws.title)
            entry["tx"] = tx
            holdings += hs
            print(f"  read {ws.title}: {len(tx)} transactions, {len(hs)} holdings")
        accounts.append(entry)
    return dict(asOf=dt.date.today().isoformat(), accounts=accounts, holdings=holdings)

def push_to_github(files, as_of):
    targets = [str(f) for f in files]
    commit_msg = f"Update portfolio snapshot ({as_of})"
    try:
        subprocess.run(["git", "add", "--", *targets], check=True)
        status = subprocess.run(
            ["git", "status", "--porcelain", "--", *targets],
            check=True, capture_output=True, text=True
        ).stdout.strip()
        if not status:
            print("No changes in HTML files to push.")
            return
        subprocess.run(["git", "commit", "-m", commit_msg, "--", *targets], check=True)
        subprocess.run(["git", "push"], check=True)
        print("Pushed portfolio HTML files to GitHub.")
    except subprocess.CalledProcessError as e:
        sys.exit(f"Git push failed (exit code {e.returncode}). Check your git remote, branch, and credentials.")

def main():
    ap = argparse.ArgumentParser(description="Build encrypted desktop and mobile portfolio HTML dashboards.")
    ap.add_argument("--xlsx", help="use this downloaded workbook instead of fetching from Google")
    ap.add_argument("--sheet-id", default=SHEET_ID, help="Google Sheet id")
    ap.add_argument("--password", help="passphrase to encrypt portfolio data (or set PORTFOLIO_PASS env var)")
    ap.add_argument("--no-encrypt", action="store_true", help="disable encryption and embed plain JSON")
    ap.add_argument("--out", default="portfolio.html", help="desktop output file (default: portfolio.html)")
    ap.add_argument("--mobile-out", default="portfolio-mobile.html", help="mobile output file (default: portfolio-mobile.html)")
    ap.add_argument("--open", action="store_true", help="open portfolio.html in browser afterwards")
    ap.add_argument("--push", action="store_true", help="stage, commit, and push generated HTML files to GitHub")
    args = ap.parse_args()    
    args = ap.parse_args()

    password = None
    if not args.no_encrypt:
        password = args.password or os.environ.get("PORTFOLIO_PASS")
        if not password:
            password = getpass.getpass("Set dashboard passphrase: ")
        if not password:
            sys.exit("Passphrase cannot be empty (use --no-encrypt if you want an unlocked file).")

    src = args.xlsx if args.xlsx else download_workbook(args.sheet_id)
    wb = openpyxl.load_workbook(src, data_only=True)
    print("Reading tabs:")
    data = build(wb)
    raw_json = json.dumps(data, ensure_ascii=False).replace("</", "<\/")

    if password:
        payload = json.dumps(encrypt_payload(raw_json, password))
        print("Encrypted portfolio data with AES-256-GCM.")
    else:
        payload = raw_json

    desktop_out = Path(args.out)
    desktop_out.write_text(DESKTOP_TEMPLATE.replace("/*DATA*/null", payload), encoding="utf-8")
    print(f"Done -> {desktop_out.resolve()}")

    mobile_out = Path(args.mobile_out)
    mobile_out.write_text(MOBILE_TEMPLATE.replace("/*DATA*/null", payload), encoding="utf-8")
    print(f"Done -> {mobile_out.resolve()}")

    if args.open:
        webbrowser.open(desktop_out.resolve().as_uri())
    
    if args.push:
        push_to_github([desktop_out, mobile_out], data["asOf"])    

# --------------------------------------------------------------------------- #
# HTML Templates
# --------------------------------------------------------------------------- #
SHARED_STYLES = r"""
:root{
  box-sizing:border-box;
  color-scheme:light;
  --bg:#f2f5f8; --surface:#ffffff; --surface-2:#f8fafc; --ink:#0f172a; --muted:#5a6877; --line:#e2e8f0;
  --gain:#0b7a5b; --gain-bg:#dcfce7; --loss:#b91c1c; --loss-bg:#fee2e2;
  --accent:#2563eb; --accent-bg:#eff6ff;
  --c1:#2563eb; --c2:#0d9488; --c3:#d97706; --c4:#7c3aed; --c5:#e11d48; --c6:#0284c7;
}
@media (prefers-color-scheme:dark){
  :root:not([data-theme="light"]){
    color-scheme:dark;
    --bg:#0b1117; --surface:#131c26; --surface-2:#182431; --ink:#f1f5f9; --muted:#94a3b8; --line:#243242;
    --gain:#34d399; --gain-bg:#064e3b; --loss:#f87171; --loss-bg:#450a0a;
    --accent:#60a5fa; --accent-bg:#172554;
    --c1:#60a5fa; --c2:#2dd4bf; --c3:#fbbf24; --c4:#a78bfa; --c5:#fb7185; --c6:#38bdf8;
  }
}
:root[data-theme="dark"]{
  color-scheme:dark;
  --bg:#0b1117; --surface:#131c26; --surface-2:#182431; --ink:#f1f5f9; --muted:#94a3b8; --line:#243242;
  --gain:#34d399; --gain-bg:#064e3b; --loss:#f87171; --loss-bg:#450a0a;
  --accent:#60a5fa; --accent-bg:#172554;
  --c1:#60a5fa; --c2:#2dd4bf; --c3:#fbbf24; --c4:#a78bfa; --c5:#fb7185; --c6:#38bdf8;
}
*,*::before,*::after{box-sizing:inherit}
body{margin:0;background:var(--bg);color:var(--ink);font:400 15px/1.5 "Hanken Grotesk",system-ui,-apple-system,sans-serif;font-variant-numeric:tabular-nums;-webkit-tap-highlight-color:transparent}
.pos{color:var(--gain)} .neg{color:var(--loss)}
.badge{display:inline-flex;align-items:center;padding:3px 10px;border-radius:999px;font-size:13px;font-weight:600}
.badge.pos{background:var(--gain-bg);color:var(--gain)}
.badge.neg{background:var(--loss-bg);color:var(--loss)}
.int-pill{display:inline-flex;align-items:center;gap:4px;margin-top:5px;padding:2px 8px;border-radius:6px;font-size:11.5px;font-weight:600;background:var(--accent-bg);color:var(--accent)}
.theme-btn{border:1px solid var(--line);background:var(--surface);color:var(--ink);font:inherit;font-size:13px;font-weight:500;padding:6px 12px;border-radius:999px;cursor:pointer}
.theme-btn:hover{border-color:var(--accent)}

.lock-wrap{max-width:380px;margin:72px auto;padding:0 16px}
.lock-card{background:var(--surface);border:1px solid var(--line);border-radius:18px;padding:24px;box-shadow:0 10px 30px rgba(0,0,0,.06)}
.lock-title{font-size:20px;font-weight:700;margin:0 0 6px;letter-spacing:-.015em}
.lock-sub{font-size:13px;color:var(--muted);margin:0 0 18px}
.lock-form{display:flex;flex-direction:column;gap:12px}
.lock-input{font:inherit;font-size:15px;padding:11px 14px;border-radius:12px;border:1px solid var(--line);background:var(--bg);color:var(--ink)}
.lock-input:focus{outline:2px solid var(--accent);border-color:transparent}
.lock-btn{font:inherit;font-size:15px;font-weight:600;padding:11px 16px;border-radius:12px;border:0;background:var(--accent);color:#fff;cursor:pointer}
.lock-btn:disabled{opacity:.6;cursor:wait}
.lock-err{font-size:13px;color:var(--loss);min-height:18px}
"""

SHARED_LOGIC = r"""
const PAYLOAD = /*DATA*/null;
let DATA = null;

const inr = new Intl.NumberFormat('en-IN',{maximumFractionDigits:0});
const inr2 = new Intl.NumberFormat('en-IN',{maximumFractionDigits:2});
const money = n => (n<0?'−':'')+'₹'+inr.format(Math.abs(Math.round(n)));
const signed = n => (n>0?'+':n<0?'−':'')+'₹'+inr.format(Math.abs(Math.round(n)));
function compact(n){
  const a=Math.abs(n), s=n<0?'−':'';
  if(a>=1e7) return s+'₹'+(a/1e7).toFixed(2)+' Cr';
  if(a>=1e5) return s+'₹'+(a/1e5).toFixed(2)+' L';
  return s+'₹'+inr.format(Math.round(a));
}
const pct = n => (n>0?'+':n<0?'−':'')+Math.abs(n*100).toFixed(1)+'%';
const cls = n => n>0?'pos':n<0?'neg':'';
const monthKey = s => s.slice(0,7);
const monthLabel = k => new Date(k+'-01T00:00:00').toLocaleDateString('en-IN',{month:'short',year:'2-digit'});
const TYPE_COLORS = {'Stock':'--c1','ETF':'--c3','Mutual fund':'--c2','Unlisted':'--c4','Digital gold':'--c5','Digital silver':'--c6','Digital gold/silver':'--c5'};

function calc5PctInterest(txList, asOfStr){
  const end = new Date(asOfStr + 'T00:00:00').getTime();
  const msPerYear = 365 * 86400 * 1000;
  let interest = 0;
  for(const t of txList){
    const start = new Date(t[0] + 'T00:00:00').getTime();
    const years = Math.max(0, (end - start) / msPerYear);
    interest += t[1] * (Math.pow(1.05, years) - 1);
  }
  return interest;
}

function initTheme(){
  const saved = localStorage.getItem('pf-theme');
  if(saved) document.documentElement.setAttribute('data-theme', saved);
  const btn = document.getElementById('themeBtn');
  if(!btn) return;
  const sync = () => {
    const cur = document.documentElement.getAttribute('data-theme');
    btn.textContent = cur === 'dark' ? '☀ Light' : cur === 'light' ? '☾ Dark' : '◐ Theme';
  };
  btn.onclick = () => {
    const isDark = getComputedStyle(document.body).backgroundColor === 'rgb(11, 17, 23)';
    const next = isDark ? 'light' : 'dark';
    document.documentElement.setAttribute('data-theme', next);
    localStorage.setItem('pf-theme', next);
    sync();
  };
  sync();
}

function mergeHoldings(list){
  const map = new Map();
  for(const h of list){
    const key = `${h.n}||${h.c}||${h.closed ? '1' : '0'}`;
    if(!map.has(key)){
      map.set(key, {
        ...h,
        _qSum: h.q != null ? h.q : 0,
        _bWeighted: (h.q != null && h.b != null) ? h.b * h.q : 0,
        _accounts: [h.a]
      });
    } else {
      const cur = map.get(key);
      cur.i += h.i;
      cur.v += h.v;
      if(h.q != null){
        cur._qSum += h.q;
        cur.q = cur._qSum;
        if(h.b != null) cur._bWeighted += h.b * h.q;
      }
      if(!cur._accounts.includes(h.a)) cur._accounts.push(h.a);
    }
  }
  return Array.from(map.values()).map(item => ({
    ...item,
    b: item.q > 0 && item._bWeighted > 0 ? item._bWeighted / item.q : item.b,
    mergedCount: item._accounts.length
  }));
}

async function decryptPayload(pkg, password){
  const b64 = s => Uint8Array.from(atob(s), c => c.charCodeAt(0));
  const salt = b64(pkg.salt);
  const iv = b64(pkg.iv);
  const ct = b64(pkg.ct);
  const baseKey = await crypto.subtle.importKey(
    'raw', new TextEncoder().encode(password), 'PBKDF2', false, ['deriveKey']
  );
  const aesKey = await crypto.subtle.deriveKey(
    { name: 'PBKDF2', salt, iterations: 250000, hash: 'SHA-256' },
    baseKey,
    { name: 'AES-GCM', length: 256 },
    false,
    ['decrypt']
  );
  const plainBuf = await crypto.subtle.decrypt({ name: 'AES-GCM', iv }, aesKey, ct);
  return JSON.parse(new TextDecoder().decode(plainBuf));
}

function lockDashboard(){
  sessionStorage.removeItem('pf-pass');
  location.reload();
}

async function boot(){
  initTheme();
  const lockWrap = document.getElementById('lockWrap');
  const appWrap = document.getElementById('appWrap');

  const unlockWith = async (pw) => {
    DATA = await decryptPayload(PAYLOAD, pw);
    sessionStorage.setItem('pf-pass', pw);
    if(lockWrap) lockWrap.remove();
    if(appWrap) appWrap.hidden = false;
    startApp();
  };

  if(!PAYLOAD || !PAYLOAD.encrypted){
    DATA = PAYLOAD;
    if(lockWrap) lockWrap.remove();
    if(appWrap) appWrap.hidden = false;
    startApp();
    return;
  }

  const cached = sessionStorage.getItem('pf-pass');
  if(cached){
    try {
      await unlockWith(cached);
      return;
    } catch {
      sessionStorage.removeItem('pf-pass');
    }
  }

  const form = document.getElementById('unlockForm');
  const input = document.getElementById('passInput');
  const btn = document.getElementById('unlockBtn');
  const errEl = document.getElementById('lockErr');
  input.focus();

  form.onsubmit = async (e) => {
    e.preventDefault();
    errEl.textContent = '';
    btn.disabled = true;
    btn.textContent = 'Decrypting…';
    try {
      await unlockWith(input.value);
    } catch {
      errEl.textContent = 'Incorrect passphrase. Please try again.';
      btn.disabled = false;
      btn.textContent = 'Unlock';
      input.select();
    }
  };
}
"""

LOCK_SCREEN_HTML = r"""
<div class="lock-wrap" id="lockWrap">
  <div class="lock-card">
    <h2 class="lock-title">🔒 Protected Portfolio</h2>
    <p class="lock-sub">Enter your passphrase to unlock your portfolio snapshot.</p>
    <form class="lock-form" id="unlockForm">
      <input class="lock-input" type="password" id="passInput" autocomplete="current-password" placeholder="Enter passphrase" required>
      <button class="lock-btn" id="unlockBtn" type="submit">Unlock</button>
      <div class="lock-err" id="lockErr"></div>
    </form>
  </div>
</div>
"""

DESKTOP_TEMPLATE = r"""<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1, viewport-fit=cover">
<title>My Portfolio</title>
<link rel="preconnect" href="https://fonts.googleapis.com">
<link rel="preconnect" href="https://fonts.gstatic.com" crossorigin>
<link href="https://fonts.googleapis.com/css2?family=Hanken+Grotesk:wght@400;500;600;700&display=swap" rel="stylesheet">
<style>
""" + SHARED_STYLES + r"""
.wrap{max-width:1180px;margin:0 auto;padding:24px 20px 64px}
header.top{display:flex;align-items:center;justify-content:space-between;gap:12px;flex-wrap:wrap}
.title-grp{display:flex;align-items:baseline;gap:12px;flex-wrap:wrap}
h1{font-size:24px;font-weight:700;margin:0;letter-spacing:-.02em}
.asof{color:var(--muted);font-size:13px}
.hdr-actions{display:flex;align-items:center;gap:8px}
.mob-link{font-size:13px;font-weight:500;color:var(--accent);text-decoration:none;padding:6px 12px;border-radius:999px;background:var(--accent-bg)}

nav.views{position:sticky;top:0;z-index:5;margin:16px -20px 0;padding:10px 20px;background:color-mix(in srgb,var(--bg) 88%,transparent);backdrop-filter:blur(10px);border-bottom:1px solid var(--line);display:flex;gap:8px;overflow-x:auto;scrollbar-width:none}
nav.views::-webkit-scrollbar{display:none}
.chip{flex:none;border:1px solid var(--line);background:var(--surface);color:var(--ink);font:inherit;font-size:14px;font-weight:500;padding:7px 14px;border-radius:999px;cursor:pointer;white-space:nowrap}
.chip small{color:var(--muted);margin-left:5px}
.chip[aria-pressed="true"]{background:var(--ink);border-color:var(--ink);color:var(--bg)}
.chip[aria-pressed="true"] small{color:inherit;opacity:.75}

.top-grid{display:grid;grid-template-columns:1.25fr 1fr;gap:16px;margin-top:22px}
.card{background:var(--surface);border:1px solid var(--line);border-radius:16px;padding:20px}
.hero-lbl{color:var(--muted);font-size:13px;font-weight:500}
.hero-val{font-size:clamp(32px,4.5vw,48px);font-weight:700;letter-spacing:-.025em;line-height:1.1;margin:6px 0 10px}
.hero-pl{display:flex;align-items:center;gap:10px;flex-wrap:wrap;font-weight:600}

.bar{display:flex;height:14px;border-radius:999px;overflow:hidden;background:var(--line);gap:2px;margin-top:8px}
.bar span{display:block;min-width:4px}
.legend{display:grid;grid-template-columns:repeat(2,minmax(0,1fr));gap:8px 14px;margin-top:14px;font-size:13px;color:var(--muted)}
.legend span{display:flex;align-items:center;justify-content:space-between;gap:8px}
.legend i{display:inline-block;width:10px;height:10px;border-radius:3px;margin-right:7px;flex:none}
.legend b{color:var(--ink);font-weight:600}

.stats{display:grid;grid-template-columns:repeat(auto-fit,minmax(210px,1fr));gap:12px;margin-top:16px}
.stat{background:var(--surface);border:1px solid var(--line);border-radius:14px;padding:16px}
.stat .k{color:var(--muted);font-size:13px}
.stat .v{font-size:21px;font-weight:700;margin-top:4px}
.stat .h{color:var(--muted);font-size:12px;margin-top:3px}

section{margin-top:28px}
.sec-head{display:flex;align-items:center;justify-content:space-between;gap:12px;flex-wrap:wrap;margin-bottom:12px}
h2{font-size:18px;font-weight:700;margin:0;letter-spacing:-.01em}
.tools{display:flex;gap:10px;flex-wrap:wrap;align-items:center}
input[type=search]{font:inherit;color:var(--ink);background:var(--surface);border:1px solid var(--line);border-radius:10px;padding:8px 12px;min-width:210px}
label.tg{display:flex;align-items:center;gap:6px;color:var(--muted);font-size:13px;cursor:pointer;background:var(--surface);border:1px solid var(--line);padding:8px 12px;border-radius:10px}

.panel{background:var(--surface);border:1px solid var(--line);border-radius:16px;overflow:hidden}
.scroll{overflow-x:auto}
table{border-collapse:collapse;width:100%;min-width:720px}
th,td{padding:11px 14px;text-align:right;white-space:nowrap;border-bottom:1px solid var(--line)}
th:first-child,td:first-child{text-align:left}
th.l,td.l{text-align:left}
tbody tr:hover{background:var(--surface-2)}
tbody tr:last-child td{border-bottom:0}
tfoot td{background:var(--surface-2);border-top:1px solid var(--line);border-bottom:0}
thead th{color:var(--muted);font-weight:600;font-size:12px;text-transform:uppercase;letter-spacing:.03em;background:var(--surface-2)}
.th{cursor:pointer;user-select:none}
.th[aria-sort="ascending"]::after{content:" ↑"}
.th[aria-sort="descending"]::after{content:" ↓"}
td .sub{display:block;color:var(--muted);font-size:12px}
.tag{font-size:12px;color:var(--muted);background:var(--surface-2);border:1px solid var(--line);padding:2px 8px;border-radius:999px}
.merge-pill{display:inline-block;margin-left:6px;font-size:11px;font-weight:600;color:var(--accent);background:var(--accent-bg);padding:1px 7px;border-radius:999px}
tr.closed td{opacity:.65}
.note{color:var(--muted);font-size:13px;margin-top:10px}
.empty{padding:28px 20px;text-align:center;color:var(--muted)}
button.more{font:inherit;font-size:13px;font-weight:500;background:var(--surface);border:1px solid var(--line);color:var(--ink);padding:8px 14px;border-radius:10px;cursor:pointer;margin-top:10px}
.chartbox{padding:16px}
.chartbox svg{display:block;width:100%;height:auto}
.axis{fill:var(--muted);font-size:11px}
.grid{stroke:var(--line);stroke-width:1}
footer{margin-top:44px;color:var(--muted);font-size:12px}

@media (max-width:860px){
  .top-grid{grid-template-columns:1fr}
}
@media (max-width:600px){
  .wrap{padding:16px 12px 56px}
  nav.views{margin:12px -12px 0;padding:8px 12px}
  input[type=search]{min-width:0;flex:1 1 160px}
}
</style>
</head>
<body>
""" + LOCK_SCREEN_HTML + r"""
<div class="wrap" id="appWrap" hidden>
  <header class="top">
    <div class="title-grp">
      <h1>My Portfolio</h1>
      <span class="asof" id="asof"></span>
    </div>
    <div class="hdr-actions">
      <a class="mob-link" href="portfolio-mobile.html">Mobile view →</a>
      <button class="theme-btn" id="themeBtn" type="button">Theme</button>
      <button class="theme-btn" id="lockBtn" type="button" title="Lock portfolio">Lock</button>
    </div>
  </header>
  <nav class="views" id="views" aria-label="Accounts"></nav>
  <main id="main"></main>
  <footer>Snapshot generated from your Google Sheet. Not live market quotes.</footer>
</div>
<script>
""" + SHARED_LOGIC + r"""
const fmtDate = s => new Date(s+'T00:00:00').toLocaleDateString('en-IN',{day:'numeric',month:'short',year:'numeric'});

function startApp(){
  const ACC = DATA.accounts;
  const ALL = {id:'all',label:'All accounts',sub:''};
  const VIEWS = [...ACC, ALL];
  const lockBtn = document.getElementById('lockBtn');
  if(!PAYLOAD.encrypted && lockBtn) lockBtn.remove();
  else if(lockBtn) lockBtn.onclick = lockDashboard;

  function accSummary(a){
    const hs = DATA.holdings.filter(h=>h.a===a.id && !h.closed);
    const invested=hs.reduce((s,h)=>s+h.i,0), value=hs.reduce((s,h)=>s+h.v,0);
    const net = a.tx.reduce((s,t)=>s+t[1],0);
    const int5 = calc5PctInterest(a.tx, DATA.asOf);
    return {invested,value,pl:value-invested,net,cashPl:value-net,int5,hasData:a.tx.length>0||hs.length>0,holdings:hs};
  }
  function viewModel(id){
    if(id==='all'){
      const parts = ACC.map(a=>({a,s:accSummary(a)}));
      const invested=parts.reduce((s,p)=>s+p.s.invested,0), value=parts.reduce((s,p)=>s+p.s.value,0);
      const net=parts.reduce((s,p)=>s+p.s.net,0);
      const int5=parts.reduce((s,p)=>s+p.s.int5,0);
      const mergedActive = mergeHoldings(DATA.holdings.filter(h=>!h.closed));
      const mergedClosed = mergeHoldings(DATA.holdings.filter(h=>h.closed));
      return {
        id,label:'All accounts',invested,value,pl:value-invested,
        net,cashPl:value-net,int5,
        holdings:mergedActive, closed:mergedClosed,
        tx:ACC.flatMap(a=>a.tx.map(t=>[...t,a.label+(a.sub?' ('+a.sub+')':'')])),
        parts,hasData:true,note:''
      };
    }
    const a=ACC.find(x=>x.id===id), s=accSummary(a);
    return {
      id,label:a.label+(a.sub?' ('+a.sub+')':''),...s,
      holdings:s.holdings, closed:DATA.holdings.filter(h=>h.a===id&&h.closed),
      tx:a.tx.map(t=>[...t]), note:a.note||'', parts:null
    };
  }

  let current = DATA.accounts[0].id, sortKey='v', sortDir=-1, query='', showClosed=false, showAllTx=false;

  function renderNav(){
    const nav=document.getElementById('views');
    nav.innerHTML = VIEWS.map(v=>`<button class="chip" data-id="${v.id}" aria-pressed="${v.id===current}">${v.label}${v.sub?`<small>${v.sub}</small>`:''}</button>`).join('');
    nav.querySelectorAll('.chip').forEach(b=>b.onclick=()=>{current=b.dataset.id;showAllTx=false;query='';showClosed=false;render();});
  }

  function allocCard(m){
    const by={};
    m.holdings.forEach(h=>{ if(h.v>0) by[h.c]=(by[h.c]||0)+h.v; });
    const tot=Object.values(by).reduce((a,b)=>a+b,0);
    if(!tot) return `<div class="card"><div class="hero-lbl">Allocation</div><p class="note">No active holdings with positive value.</p></div>`;
    const rows=Object.entries(by).sort((a,b)=>b[1]-a[1]);
    return `<div class="card">
      <div class="hero-lbl">Allocation by asset type</div>
      <div class="bar">${rows.map(([k,v])=>`<span title="${k}: ${compact(v)}" style="flex:${v} 1 0;background:var(${TYPE_COLORS[k]||'--c1'})"></span>`).join('')}</div>
      <div class="legend">${rows.map(([k,v])=>`<span><span><i style="background:var(${TYPE_COLORS[k]||'--c1'})"></i>${k}</span><b>${(v/tot*100).toFixed(1)}%</b></span>`).join('')}</div>
    </div>`;
  }

  function statsBlock(m){
    const cashSub = `${m.net?pct(m.cashPl/Math.abs(m.net)):'–'}` +
      (m.tx.length ? `<br><span class="int-pill" title="Compound interest at 5% p.a. from each cashflow date">5% p.a. benchmark: ${signed(m.int5)}</span>` : '');
    const items=[
      ['Invested',compact(m.invested),'Cost basis of active holdings'],
      ['Unrealised P/L',`<span class="${cls(m.pl)}">${signed(m.pl)}</span>`,m.invested?pct(m.pl/m.invested):'–'],
      ['Net deposited',compact(m.net),'Deposits minus withdrawals'],
      ['Cash vs Current Value P/L',`<span class="${cls(m.cashPl)}">${signed(m.cashPl)}</span>`,cashSub]
    ];
    return `<div class="stats">${items.map(([k,v,h])=>`<div class="stat"><div class="k">${k}</div><div class="v">${v}</div><div class="h">${h}</div></div>`).join('')}</div>`;
  }

  function holdingsBlock(m){
    let rows=[...m.holdings, ...(showClosed?m.closed:[])];
    if(query) rows=rows.filter(h=>h.n.toLowerCase().includes(query.toLowerCase())||h.c.toLowerCase().includes(query.toLowerCase()));
    rows=rows.map(h=>({...h,pl:h.v-h.i,plp:h.i?(h.v-h.i)/h.i:0}));
    rows.sort((a,b)=>{
      const x=a[sortKey], y=b[sortKey];
      if(typeof x==='string') return sortDir*x.localeCompare(y);
      return sortDir*((x??-Infinity)-(y??-Infinity));
    });
    const cols=[['n','Holding','l'],['c','Type','l'],['q','Qty'],['b','Avg buy'],['p','CMP'],['i','Invested'],['v','Value'],['pl','P/L'],['plp','P/L %']];
    const head=cols.map(([k,t,l])=>`<th class="th ${l||''}" tabindex="0" data-k="${k}" aria-sort="${k===sortKey?(sortDir>0?'ascending':'descending'):'none'}">${t}</th>`).join('');
    const body=rows.map(h=>`<tr class="${h.closed?'closed':''}">
      <td><b>${h.n}</b>${h.mergedCount>1?`<span class="merge-pill">${h.mergedCount} accts</span>`:''}${h.closed?'<span class="sub">Sold out</span>':''}</td>
      <td class="l"><span class="tag">${h.c}</span></td>
      <td>${h.q!=null?inr2.format(h.q):'–'}</td>
      <td>${h.b?inr2.format(h.b):'–'}</td>
      <td>${h.p?inr2.format(h.p):'–'}</td>
      <td>${money(h.i)}</td>
      <td><b>${money(h.v)}</b></td>
      <td class="${cls(h.pl)}">${signed(h.pl)}</td>
      <td class="${cls(h.plp)}">${h.i?pct(h.plp):'–'}</td></tr>`).join('');
    const tot=rows.reduce((s,h)=>({i:s.i+h.i,v:s.v+h.v}),{i:0,v:0});
    return `<section>
      <div class="sec-head">
        <h2>Holdings (${rows.length})</h2>
        <div class="tools">
          <input type="search" id="q" placeholder="Filter holding or type…" value="${query}">
          ${m.closed.length?`<label class="tg"><input type="checkbox" id="closed" ${showClosed?'checked':''}> Sold-out (${m.closed.length})</label>`:''}
        </div>
      </div>
      <div class="panel scroll"><table><thead><tr>${head}</tr></thead><tbody>${body||`<tr><td colspan="9" class="empty">No holdings match “${query}”.</td></tr>`}</tbody>
      <tfoot><tr><td colspan="5" class="l"><b>Total</b></td><td><b>${money(tot.i)}</b></td><td><b>${money(tot.v)}</b></td><td class="${cls(tot.v-tot.i)}"><b>${signed(tot.v-tot.i)}</b></td><td></td></tr></tfoot></table></div>
    </section>`;
  }

  function accountsBlock(m){
    const rows=m.parts.map(({a,s})=>`<tr><td><b>${a.label}</b>${a.sub?`<span class="sub">${a.sub}</span>`:''}</td>
      <td>${s.hasData?money(s.invested):'–'}</td><td><b>${s.hasData?money(s.value):'–'}</b></td>
      <td class="${cls(s.pl)}">${s.hasData?signed(s.pl):'–'}</td>
      <td>${s.hasData&&m.value?(s.value/m.value*100).toFixed(1)+'%':'–'}</td></tr>`).join('');
    return `<section><h2>By account</h2><div class="panel scroll"><table style="min-width:540px"><thead><tr><th>Account</th><th>Invested</th><th>Value</th><th>P/L</th><th>Share</th></tr></thead><tbody>${rows}</tbody></table></div></section>`;
  }

  function flowBlock(m){
    if(!m.tx.length) return '';
    const sorted=[...m.tx].sort((a,b)=>a[0].localeCompare(b[0]));
    const months={}; sorted.forEach(t=>{const k=monthKey(t[0]); months[k]=(months[k]||0)+t[1];});
    const keys=Object.keys(months).sort();
    const filled=[]; let [y,mo]=keys[0].split('-').map(Number); const [ey,em]=keys[keys.length-1].split('-').map(Number);
    while(y<ey||(y===ey&&mo<=em)){ filled.push(y+'-'+String(mo).padStart(2,'0')); mo++; if(mo>12){mo=1;y++;} }
    let cum=0; const pts=filled.map(k=>{cum+=months[k]||0; return {k,m:months[k]||0,c:cum};});
    const W=Math.max(320,(document.getElementById('main').clientWidth||900)-32), H=230, L=56, R=12, T=12, B=26;
    const maxV=Math.max(...pts.map(p=>Math.max(p.c,p.m,0)),1), minV=Math.min(...pts.map(p=>Math.min(p.c,p.m,0)),0);
    const y0=v=>T+(maxV-v)/(maxV-minV||1)*(H-T-B);
    const bw=(W-L-R)/pts.length, bx=i=>L+i*bw;
    const tv=Array.from({length:5},(_,i)=>minV+(maxV-minV)*i/4);
    const step=Math.max(1,Math.ceil(pts.length/(W<560?5:10)));
    const bars=pts.map((p,i)=>`<rect x="${bx(i)+bw*.18}" y="${Math.min(y0(p.m),y0(0))}" width="${Math.max(2,bw*.64)}" height="${Math.abs(y0(p.m)-y0(0))}" rx="2" fill="var(${p.m>=0?'--accent':'--loss'})" opacity=".82"><title>${monthLabel(p.k)}: ${signed(p.m)}</title></rect>`).join('');
    const line=pts.map((p,i)=>`${i?'L':'M'}${bx(i)+bw/2},${y0(p.c)}`).join('');
    return `<section><h2>Cash flow</h2><div class="panel chartbox">
      <svg viewBox="0 0 ${W} ${H}">
        ${tv.map(v=>`<line class="grid" x1="${L}" x2="${W-R}" y1="${y0(v)}" y2="${y0(v)}"/><text class="axis" x="${L-6}" y="${y0(v)+4}" text-anchor="end">${compact(v).replace('₹','')}</text>`).join('')}
        ${bars}
        <path d="${line}" fill="none" stroke="var(--gain)" stroke-width="2.2" stroke-linejoin="round"/>
        ${pts.map((p,i)=>i%step===0?`<text class="axis" x="${bx(i)+bw/2}" y="${H-8}" text-anchor="middle">${monthLabel(p.k)}</text>`:'').join('')}
      </svg>
    </div></section>`;
  }

  function txBlock(m){
    if(!m.tx.length) return '';
    const rows=[...m.tx].sort((a,b)=>b[0].localeCompare(a[0]));
    const shown=showAllTx?rows:rows.slice(0,10);
    const withAcc=m.id==='all';
    return `<section><h2>Transactions</h2><div class="panel scroll"><table style="min-width:460px"><thead><tr><th>Date</th>${withAcc?'<th class="l">Account</th>':''}<th>Amount</th><th class="l">Note</th></tr></thead><tbody>
    ${shown.map(t=>`<tr><td>${fmtDate(t[0])}</td>${withAcc?`<td class="l">${t[3]}</td>`:''}<td class="${t[1]<0?'neg':''}">${signed(t[1])}</td><td class="l"><span class="tag">${t[2]||(t[1]<0?'Withdrawal':'Deposit')}</span></td></tr>`).join('')}
    </tbody></table></div>
    ${rows.length>10?`<button class="more" id="moretx">${showAllTx?'Show less':'Show all ('+rows.length+')'}</button>`:''}</section>`;
  }

  function render(){
    renderNav();
    document.getElementById('asof').textContent='As of '+fmtDate(DATA.asOf);
    const m=viewModel(current), main=document.getElementById('main');
    if(!m.hasData){
      main.innerHTML=`<section><div class="panel empty"><b>${m.label} has no data yet</b><p class="note">${m.note}</p></div></section>`;
      return;
    }
    main.innerHTML=`
      <div class="top-grid">
        <div class="card">
          <div class="hero-lbl">${m.label} · Current Value</div>
          <div class="hero-val">${money(m.value)}</div>
          <div class="hero-pl"><span class="${cls(m.pl)}">${signed(m.pl)}</span><span class="badge ${cls(m.pl)}">${m.invested?pct(m.pl/m.invested):'–'}</span></div>
        </div>
        ${allocCard(m)}
      </div>
      ${statsBlock(m)}
      ${m.parts?accountsBlock(m):''}
      ${m.holdings.length?holdingsBlock(m):''}
      ${flowBlock(m)}
      ${txBlock(m)}`;
    const q=document.getElementById('q'); if(q){q.oninput=e=>{query=e.target.value;const p=e.target.selectionStart;render();const n=document.getElementById('q');n.focus();n.setSelectionRange(p,p);};}
    const c=document.getElementById('closed'); if(c) c.onchange=e=>{showClosed=e.target.checked;render();};
    const mt=document.getElementById('moretx'); if(mt) mt.onclick=()=>{showAllTx=!showAllTx;render();};
    main.querySelectorAll('.th').forEach(th=>{
      const act=()=>{const k=th.dataset.k; if(sortKey===k) sortDir*=-1; else {sortKey=k; sortDir=(k==='n'||k==='c')?1:-1;} render();};
      th.onclick=act; th.onkeydown=e=>{if(e.key==='Enter'||e.key===' '){e.preventDefault();act();}};
    });
  }
  render();
}
boot();
</script>
</body>
</html>
"""

MOBILE_TEMPLATE = r"""<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1, viewport-fit=cover">
<title>Portfolio (Mobile)</title>
<link rel="preconnect" href="https://fonts.googleapis.com">
<link rel="preconnect" href="https://fonts.gstatic.com" crossorigin>
<link href="https://fonts.googleapis.com/css2?family=Hanken+Grotesk:wght@400;500;600;700&display=swap" rel="stylesheet">
<style>
""" + SHARED_STYLES + r"""
.m-shell{max-width:520px;margin:0 auto;min-height:100dvh;background:var(--bg);padding-bottom:48px}
header.m-top{display:flex;align-items:center;justify-content:space-between;gap:8px;padding:14px 14px 8px}
h1{font-size:19px;font-weight:700;margin:0;letter-spacing:-.015em}
.asof{font-size:12px;color:var(--muted)}
.hdr-r{display:flex;align-items:center;gap:6px}
.desk-link{font-size:12px;font-weight:500;color:var(--accent);text-decoration:none;padding:6px 10px;border-radius:999px;background:var(--accent-bg)}

nav.m-views{position:sticky;top:0;z-index:10;display:flex;gap:6px;padding:8px 14px;overflow-x:auto;scrollbar-width:none;background:color-mix(in srgb,var(--bg) 92%,transparent);backdrop-filter:blur(10px);border-bottom:1px solid var(--line)}
nav.m-views::-webkit-scrollbar{display:none}
.m-chip{flex:none;border:1px solid var(--line);background:var(--surface);color:var(--ink);font:inherit;font-size:13px;font-weight:500;padding:7px 12px;border-radius:999px;cursor:pointer;white-space:nowrap}
.m-chip small{color:var(--muted);margin-left:4px;font-size:11px}
.m-chip[aria-pressed="true"]{background:var(--ink);border-color:var(--ink);color:var(--bg)}
.m-chip[aria-pressed="true"] small{color:inherit;opacity:.75}

.m-body{padding:12px 14px 0}
.hero-card{background:var(--surface);border:1px solid var(--line);border-radius:16px;padding:16px}
.hero-lbl{font-size:12px;color:var(--muted);font-weight:500}
.hero-val{font-size:34px;font-weight:700;letter-spacing:-.025em;line-height:1.1;margin:4px 0 8px}
.hero-row{display:flex;align-items:center;gap:8px;flex-wrap:wrap;font-size:15px;font-weight:600}

.m-alloc{background:var(--surface);border:1px solid var(--line);border-radius:14px;padding:12px 14px;margin-top:10px}
.m-bar{display:flex;height:10px;border-radius:999px;overflow:hidden;background:var(--line);gap:2px}
.m-bar span{display:block;min-width:3px}
.m-pills{display:flex;flex-wrap:wrap;gap:6px;margin-top:10px}
.m-pill{display:inline-flex;align-items:center;gap:5px;font-size:12px;background:var(--surface-2);border:1px solid var(--line);padding:4px 8px;border-radius:8px}
.m-pill i{width:8px;height:8px;border-radius:2px;flex:none}
.m-pill b{font-weight:600}

.m-grid2{display:grid;grid-template-columns:1fr 1fr;gap:8px;margin-top:10px}
.m-kpi{background:var(--surface);border:1px solid var(--line);border-radius:12px;padding:12px}
.m-kpi .k{font-size:12px;color:var(--muted)}
.m-kpi .v{font-size:17px;font-weight:700;margin-top:2px}

section.m-sec{margin-top:18px}
.m-sec-h{display:flex;align-items:center;justify-content:space-between;margin-bottom:8px}
h2{font-size:15px;font-weight:700;margin:0}
.m-count{font-size:12px;color:var(--muted)}

.m-controls{display:grid;grid-template-columns:1fr auto;gap:8px;margin-bottom:10px}
input[type=search],select{font:inherit;font-size:14px;color:var(--ink);background:var(--surface);border:1px solid var(--line);border-radius:10px;padding:9px 11px;min-width:0}
.m-closed-row{margin-bottom:10px}
label.m-tg{display:inline-flex;align-items:center;gap:6px;font-size:13px;color:var(--muted)}

.h-card{background:var(--surface);border:1px solid var(--line);border-radius:12px;padding:12px;margin-bottom:8px}
.h-card.closed{opacity:.65}
.h-top{display:flex;justify-content:space-between;align-items:flex-start;gap:10px}
.h-name{font-size:15px;font-weight:700}
.h-meta{font-size:12px;color:var(--muted);margin-top:2px}
.h-val{text-align:right;font-size:15px;font-weight:700}
.h-pl{text-align:right;font-size:12px;font-weight:600;margin-top:2px}
.h-foot{display:flex;justify-content:space-between;gap:8px;flex-wrap:wrap;margin-top:10px;padding-top:8px;border-top:1px solid var(--line);font-size:12px;color:var(--muted)}
.h-foot b{color:var(--ink);font-weight:600}

.tx-card{display:flex;justify-content:space-between;align-items:center;gap:10px;background:var(--surface);border:1px solid var(--line);border-radius:12px;padding:11px 12px;margin-bottom:8px}
.tx-main{font-size:14px;font-weight:600}
.tx-sub{font-size:12px;color:var(--muted);margin-top:2px}
.tx-amt{font-size:14px;font-weight:700;white-space:nowrap}

.m-chart{background:var(--surface);border:1px solid var(--line);border-radius:14px;padding:10px}
.m-chart svg{display:block;width:100%;height:auto}
.axis{fill:var(--muted);font-size:11px}
.grid{stroke:var(--line);stroke-width:1}
.m-more{width:100%;font:inherit;font-size:13px;font-weight:600;background:var(--surface);border:1px solid var(--line);color:var(--ink);padding:10px;border-radius:12px;cursor:pointer;margin-top:4px}
.empty{background:var(--surface);border:1px solid var(--line);border-radius:14px;padding:20px;text-align:center;color:var(--muted)}
</style>
</head>
<body>
""" + LOCK_SCREEN_HTML + r"""
<div class="m-shell" id="appWrap" hidden>
  <header class="m-top">
    <div>
      <h1>My Portfolio</h1>
      <div class="asof" id="asof"></div>
    </div>
    <div class="hdr-r">
      <a class="desk-link" href="portfolio.html">Full view</a>
      <button class="theme-btn" id="themeBtn" type="button">Theme</button>
      <button class="theme-btn" id="lockBtn" type="button">Lock</button>
    </div>
  </header>
  <nav class="m-views" id="views"></nav>
  <main class="m-body" id="main"></main>
</div>
<script>
""" + SHARED_LOGIC + r"""
const fmtDate = s => new Date(s+'T00:00:00').toLocaleDateString('en-IN',{day:'numeric',month:'short',year:'2-digit'});

function startApp(){
  const ACC = DATA.accounts;
  const ALL = {id:'all',label:'All',sub:''};
  const VIEWS = [...ACC, ALL];
  const lockBtn = document.getElementById('lockBtn');
  if(!PAYLOAD.encrypted && lockBtn) lockBtn.remove();
  else if(lockBtn) lockBtn.onclick = lockDashboard;

  function accSummary(a){
    const hs = DATA.holdings.filter(h=>h.a===a.id && !h.closed);
    const invested=hs.reduce((s,h)=>s+h.i,0), value=hs.reduce((s,h)=>s+h.v,0);
    const net = a.tx.reduce((s,t)=>s+t[1],0);
    const int5 = calc5PctInterest(a.tx, DATA.asOf);
    return {invested,value,pl:value-invested,net,cashPl:value-net,int5,hasData:a.tx.length>0||hs.length>0,holdings:hs};
  }
  function viewModel(id){
    if(id==='all'){
      const parts = ACC.map(a=>({a,s:accSummary(a)}));
      const invested=parts.reduce((s,p)=>s+p.s.invested,0), value=parts.reduce((s,p)=>s+p.s.value,0);
      const net=parts.reduce((s,p)=>s+p.s.net,0);
      const int5=parts.reduce((s,p)=>s+p.s.int5,0);
      const mergedActive = mergeHoldings(DATA.holdings.filter(h=>!h.closed));
      const mergedClosed = mergeHoldings(DATA.holdings.filter(h=>h.closed));
      return {
        id,label:'All accounts',invested,value,pl:value-invested,
        net,cashPl:value-net,int5,
        holdings:mergedActive, closed:mergedClosed,
        tx:ACC.flatMap(a=>a.tx.map(t=>[...t,a.label+(a.sub?' · '+a.sub:'')])),
        parts,hasData:true,note:''
      };
    }
    const a=ACC.find(x=>x.id===id), s=accSummary(a);
    return {
      id,label:a.label+(a.sub?' · '+a.sub:''),...s,
      holdings:s.holdings, closed:DATA.holdings.filter(h=>h.a===id&&h.closed),
      tx:a.tx.map(t=>[...t]), note:a.note||'', parts:null
    };
  }

  let current = DATA.accounts[0].id, sortKey='v', sortDir=-1, query='', showClosed=false, showAllTx=false;

  function renderNav(){
    const nav=document.getElementById('views');
    nav.innerHTML = VIEWS.map(v=>`<button class="m-chip" data-id="${v.id}" aria-pressed="${v.id===current}">${v.label}${v.sub?`<small>${v.sub}</small>`:''}</button>`).join('');
    nav.querySelectorAll('.m-chip').forEach(b=>b.onclick=()=>{current=b.dataset.id;showAllTx=false;query='';showClosed=false;render();});
  }

  function allocBlock(m){
    const by={};
    m.holdings.forEach(h=>{ if(h.v>0) by[h.c]=(by[h.c]||0)+h.v; });
    const tot=Object.values(by).reduce((a,b)=>a+b,0);
    if(!tot) return '';
    const rows=Object.entries(by).sort((a,b)=>b[1]-a[1]);
    return `<div class="m-alloc">
      <div class="m-bar">${rows.map(([k,v])=>`<span style="flex:${v} 1 0;background:var(${TYPE_COLORS[k]||'--c1'})"></span>`).join('')}</div>
      <div class="m-pills">${rows.map(([k,v])=>`<span class="m-pill"><i style="background:var(${TYPE_COLORS[k]||'--c1'})"></i>${k} <b>${(v/tot*100).toFixed(0)}%</b></span>`).join('')}</div>
    </div>`;
  }

  function kpiGrid(m){
    const intTag = m.tx.length ? `<div class="int-pill">5% int: ${signed(m.int5)}</div>` : '';
    const kpis=[
      ['Invested',compact(m.invested),'',''],
      ['P/L %',m.invested?pct(m.pl/m.invested):'–',cls(m.pl),''],
      ['Net Deposited',compact(m.net),'',''],
      ['Cash vs Value P/L',compact(m.cashPl),cls(m.cashPl),intTag]
    ];
    return `<div class="m-grid2">${kpis.map(([k,v,c,extra])=>`<div class="m-kpi"><div class="k">${k}</div><div class="v ${c}">${v}</div>${extra}</div>`).join('')}</div>`;
  }

  function holdingsBlock(m){
    let rows=[...m.holdings, ...(showClosed?m.closed:[])];
    if(query) rows=rows.filter(h=>h.n.toLowerCase().includes(query.toLowerCase())||h.c.toLowerCase().includes(query.toLowerCase()));
    rows=rows.map(h=>({...h,pl:h.v-h.i,plp:h.i?(h.v-h.i)/h.i:0}));
    rows.sort((a,b)=>{
      const x=a[sortKey], y=b[sortKey];
      if(typeof x==='string') return sortDir*x.localeCompare(y);
      return sortDir*((x??-Infinity)-(y??-Infinity));
    });
    const cards=rows.map(h=>`<div class="h-card ${h.closed?'closed':''}">
      <div class="h-top">
        <div>
          <div class="h-name">${h.n}</div>
          <div class="h-meta">${h.c}${h.mergedCount > 1 ? ' · ' + h.mergedCount + ' accounts' : ''}${h.closed ? ' · Sold out' : ''}</div>
        </div>
        <div>
          <div class="h-val">${money(h.v)}</div>
          <div class="h-pl ${cls(h.pl)}">${signed(h.pl)} (${h.i?pct(h.plp):'–'})</div>
        </div>
      </div>
      <div class="h-foot">
        <span>Inv <b>${compact(h.i)}</b></span>
        ${h.q!=null?`<span>Qty <b>${inr2.format(h.q)}</b></span>`:''}
        ${h.b?`<span>Avg <b>${inr2.format(h.b)}</b></span>`:''}
        ${h.p?`<span>CMP <b>${inr2.format(h.p)}</b></span>`:''}
      </div>
    </div>`).join('');

    return `<section class="m-sec">
      <div class="m-sec-h"><h2>Holdings</h2><span class="m-count">${rows.length} items</span></div>
      <div class="m-controls">
        <input type="search" id="q" placeholder="Search holding…" value="${query}">
        <select id="sortSel" aria-label="Sort holdings">
          <option value="v:-1" ${sortKey==='v'&&sortDir===-1?'selected':''}>Value ↓</option>
          <option value="pl:-1" ${sortKey==='pl'&&sortDir===-1?'selected':''}>P/L (₹) ↓</option>
          <option value="plp:-1" ${sortKey==='plp'&&sortDir===-1?'selected':''}>P/L (%) ↓</option>
          <option value="plp:1" ${sortKey==='plp'&&sortDir===1?'selected':''}>P/L (%) ↑</option>
          <option value="n:1" ${sortKey==='n'&&sortDir===1?'selected':''}>Name A–Z</option>
        </select>
      </div>
      ${m.closed.length?`<div class="m-closed-row"><label class="m-tg"><input type="checkbox" id="closed" ${showClosed?'checked':''}> Show sold-out (${m.closed.length})</label></div>`:''}
      ${cards||`<div class="empty">No holdings match “${query}”.</div>`}
    </section>`;
  }

  function accountsBlock(m){
    return `<section class="m-sec"><div class="m-sec-h"><h2>Accounts</h2></div>${m.parts.map(({a,s})=>`<div class="h-card">
      <div class="h-top">
        <div><div class="h-name">${a.label}${a.sub?' · '+a.sub:''}</div><div class="h-meta">${s.hasData&&m.value?(s.value/m.value*100).toFixed(1)+'% of total':'No data'}</div></div>
        <div><div class="h-val">${s.hasData?money(s.value):'–'}</div><div class="h-pl ${cls(s.pl)}">${s.hasData?signed(s.pl):'–'}</div></div>
      </div>
      <div class="h-foot"><span>Invested <b>${s.hasData?compact(s.invested):'–'}</b></span><span>Net flow <b>${s.hasData?compact(s.net):'–'}</b></span></div>
    </div>`).join('')}</section>`;
  }

  function flowBlock(m){
    if(!m.tx.length) return '';
    const sorted=[...m.tx].sort((a,b)=>a[0].localeCompare(b[0]));
    const months={}; sorted.forEach(t=>{const k=monthKey(t[0]); months[k]=(months[k]||0)+t[1];});
    const keys=Object.keys(months).sort();
    const filled=[]; let [y,mo]=keys[0].split('-').map(Number); const [ey,em]=keys[keys.length-1].split('-').map(Number);
    while(y<ey||(y===ey&&mo<=em)){ filled.push(y+'-'+String(mo).padStart(2,'0')); mo++; if(mo>12){mo=1;y++;} }
    let cum=0; const pts=filled.map(k=>{cum+=months[k]||0; return {k,m:months[k]||0,c:cum};});
    const W=460, H=190, L=50, R=8, T=10, B=24;
    const maxV=Math.max(...pts.map(p=>Math.max(p.c,p.m,0)),1), minV=Math.min(...pts.map(p=>Math.min(p.c,p.m,0)),0);
    const y0=v=>T+(maxV-v)/(maxV-minV||1)*(H-T-B);
    const bw=(W-L-R)/pts.length, bx=i=>L+i*bw;
    const tv=Array.from({length:4},(_,i)=>minV+(maxV-minV)*i/3);
    const step=Math.max(1,Math.ceil(pts.length/5));
    const bars=pts.map((p,i)=>`<rect x="${bx(i)+bw*.18}" y="${Math.min(y0(p.m),y0(0))}" width="${Math.max(2,bw*.64)}" height="${Math.abs(y0(p.m)-y0(0))}" rx="2" fill="var(${p.m>=0?'--accent':'--loss'})" opacity=".82"></rect>`).join('');
    const line=pts.map((p,i)=>`${i?'L':'M'}${bx(i)+bw/2},${y0(p.c)}`).join('');
    return `<section class="m-sec"><div class="m-sec-h"><h2>Monthly Cash Flow</h2></div><div class="m-chart">
      <svg viewBox="0 0 ${W} ${H}">
        ${tv.map(v=>`<line class="grid" x1="${L}" x2="${W-R}" y1="${y0(v)}" y2="${y0(v)}"/><text class="axis" x="${L-5}" y="${y0(v)+4}" text-anchor="end">${compact(v).replace('₹','')}</text>`).join('')}
        ${bars}
        <path d="${line}" fill="none" stroke="var(--gain)" stroke-width="2.2" stroke-linejoin="round"/>
        ${pts.map((p,i)=>i%step===0?`<text class="axis" x="${bx(i)+bw/2}" y="${H-6}" text-anchor="middle">${monthLabel(p.k)}</text>`:'').join('')}
      </svg>
    </div></section>`;
  }

  function txBlock(m){
    if(!m.tx.length) return '';
    const rows=[...m.tx].sort((a,b)=>b[0].localeCompare(a[0]));
    const shown=showAllTx?rows:rows.slice(0,8);
    const withAcc=m.id==='all';
    return `<section class="m-sec"><div class="m-sec-h"><h2>Recent Transactions</h2><span class="m-count">${rows.length} total</span></div>
      ${shown.map(t=>`<div class="tx-card">
        <div>
          <div class="tx-main">${t[2]||(t[1]<0?'Withdrawal':'Deposit')}</div>
          <div class="tx-sub">${fmtDate(t[0])}${withAcc?' · '+t[3]:''}</div>
        </div>
        <div class="tx-amt ${t[1]<0?'neg':'pos'}">${signed(t[1])}</div>
      </div>`).join('')}
      ${rows.length>8?`<button class="m-more" id="moretx">${showAllTx?'Show less':'Show all '+rows.length+' transactions'}</button>`:''}
    </section>`;
  }

  function render(){
    renderNav();
    document.getElementById('asof').textContent='Snapshot '+fmtDate(DATA.asOf);
    const m=viewModel(current), main=document.getElementById('main');
    if(!m.hasData){
      main.innerHTML=`<div class="empty"><b>${m.label} has no data yet</b><p class="asof" style="margin-top:6px">${m.note}</p></div>`;
      return;
    }
    main.innerHTML=`
      <div class="hero-card">
        <div class="hero-lbl">${m.label} · Current Value</div>
        <div class="hero-val">${money(m.value)}</div>
        <div class="hero-row">
          <span class="${cls(m.pl)}">${signed(m.pl)}</span>
          <span class="badge ${cls(m.pl)}">${m.invested?pct(m.pl/m.invested):'–'}</span>
        </div>
      </div>
      ${allocBlock(m)}
      ${kpiGrid(m)}
      ${m.parts?accountsBlock(m):''}
      ${m.holdings.length?holdingsBlock(m):''}
      ${flowBlock(m)}
      ${txBlock(m)}`;
    const q=document.getElementById('q'); if(q){q.oninput=e=>{query=e.target.value;const p=e.target.selectionStart;render();const n=document.getElementById('q');n.focus();n.setSelectionRange(p,p);};}
    const s=document.getElementById('sortSel'); if(s) s.onchange=e=>{const [k,d]=e.target.value.split(':');sortKey=k;sortDir=Number(d);render();};
    const c=document.getElementById('closed'); if(c) c.onchange=e=>{showClosed=e.target.checked;render();};
    const mt=document.getElementById('moretx'); if(mt) mt.onclick=()=>{showAllTx=!showAllTx;render();};
  }
  render();
}
boot();
</script>
</body>
</html>
"""

if __name__ == "__main__":
    main()