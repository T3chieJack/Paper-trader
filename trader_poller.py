import os, sys, json, re
from datetime import datetime, timezone
import requests
import yfinance as yf

# ===== Config =====
BOT_TOKEN = os.getenv("DISCORD_BOT_TOKEN")
CHANNEL_ID = os.getenv("DISCORD_CHANNEL_ID")
DEFAULT_CASH = float(os.getenv("DEFAULT_CASH", "100000"))
if not BOT_TOKEN or not CHANNEL_ID:
    print("CONFIG ERROR: Missing DISCORD_BOT_TOKEN or DISCORD_CHANNEL_ID", file=sys.stderr); sys.exit(1)

API = "https://discord.com/api/v10"
H = {"Authorization": f"Bot {BOT_TOKEN}"}

# ===== Files =====
PTF_FILE = "data/portfolio.json"
LEDGER_FILE = "data/ledger.csv"
ALLOW_FILE = "data/symbols_allowlist.txt"
STATE_FILE = "data/state.json"

# ===== Commands =====
ORDER_RE = re.compile(r"^!(buy|sell)\s+([A-Za-z0-9\.-]{1,10})\s+(\d+)$", re.I)
PRICE_RE = re.compile(r"^!price\s+([A-Za-z0-9\.-]{1,10})$", re.I)

# ===== Helpers =====
def load_json(path, default):
    try:
        with open(path, "r") as f: return json.load(f)
    except FileNotFoundError:
        with open(path, "w") as f: json.dump(default, f, indent=2)
        return default

def save_json(path, obj):
    with open(path, "w") as f: json.dump(obj, f, indent=2)

def load_portfolio():
    return load_json(PTF_FILE, {"cash": DEFAULT_CASH, "positions": {}, "last_mark": None})

def save_portfolio(p): save_json(PTF_FILE, p)

def ensure_ledger_header():
    try:
        with open(LEDGER_FILE, "x") as f:
            f.write("timestamp,side,ticker,qty,fill_price,value\n")
    except FileExistsError:
        pass

def append_ledger(side, ticker, qty, px):
    ensure_ledger_header()
    with open(LEDGER_FILE, "a") as f:
        f.write(f"{datetime.now(timezone.utc).isoformat(timespec='seconds')},{side},{ticker},{qty},{px},{qty*px}\n")

def load_allowlist():
    try:
        with open(ALLOW_FILE) as f:
            return set(s.strip() for s in f if s.strip())
    except FileNotFoundError:
        return set()

def discord_get_messages(limit=50, after=None):
    params = {"limit": limit}
    if after: params["after"] = after
    r = requests.get(f"{API}/channels/{CHANNEL_ID}/messages", headers=H, params=params, timeout=20)
    if r.status_code == 403:
        raise SystemExit("PERMISSION ERROR: Need View Channel + Read Message History")
    r.raise_for_status()
    return r.json()  # newest first

def discord_post(content=None, embed=None):
    payload = {}
    if content: payload["content"] = content
    if embed: payload["embeds"] = [embed]
    r = requests.post(f"{API}/channels/{CHANNEL_ID}/messages", headers=H, json=payload, timeout=20)
    if r.status_code == 403:
        raise SystemExit("PERMISSION ERROR: Need Send Messages + Embed Links")
    r.raise_for_status()

def add_reaction(message_id, emoji="✅"):
    url = f"{API}/channels/{CHANNEL_ID}/messages/{message_id}/reactions/{requests.utils.quote(emoji, safe='')}/@me"
    requests.put(url, headers=H, timeout=15)

def fetch_quotes(tickers):
    if not tickers: return {}
    out = {}
    data = yf.Tickers(" ".join(tickers))
    for t in tickers:
        try:
            info = data.tickers[t].info
            px = info.get("regularMarketPrice") or info.get("currentPrice")
            if px is None:
                hist = data.tickers[t].history(period="2d", interval="1d")
                if not hist.empty: px = float(hist["Close"].iloc[-1])
            if px is not None: out[t] = float(px)
        except Exception as e:
            print(f"quote error {t}: {e}", file=sys.stderr)
    return out

def parse_command(text):
    s = (text or "").strip()
    m = ORDER_RE.match(s)
    if m: return ("order", m.group(1).lower(), m.group(2).upper(), int(m.group(3)))
    m = PRICE_RE.match(s)
    if m: return ("price", m.group(1).upper())
    if s.lower().startswith("!portfolio"): return ("portfolio",)
    return None

# ===== State (dedupe) =====
def load_state():
    return load_json(STATE_FILE, {"last_message_id": None, "processed_ids": []})

def save_state(st):
    st["processed_ids"] = (st.get("processed_ids") or [])[-500:]  # cap size
    save_json(STATE_FILE, st)

# ===== Actions =====
def do_order(side, sym, qty, ptf, allow):
    if sym not in allow:
        discord_post(f"❌ `{sym}` not allowed. Add it to `data/symbols_allowlist.txt`."); return
    quotes = fetch_quotes([sym]); px = quotes.get(sym)
    if px is None:
        discord_post(f"❌ No price for `{sym}` right now."); return
    if side == "buy":
        cost = qty * px
        if cost > ptf["cash"]:
            discord_post(f"❌ Need ${cost:,.2f}, only have ${ptf['cash']:,.2f}."); return
        ptf["cash"] = round(ptf["cash"] - cost, 2)
        ptf["positions"][sym] = ptf["positions"].get(sym, 0) + qty
        append_ledger("BUY", sym, qty, px)
        discord_post(embed={"title":"🟢 Filled BUY","fields":[{"name":sym,"value":f"qty: {qty}\nfill: {px:.2f}\ncost: ${cost:,.2f}"}]})
    else:
        pos = ptf["positions"].get(sym, 0)
        if qty > pos:
            discord_post(f"❌ You only have {pos} {sym}."); return
        proceeds = qty * px
        ptf["cash"] = round(ptf["cash"] + proceeds, 2)
        newq = pos - qty
        if newq: ptf["positions"][sym] = newq
        else: ptf["positions"].pop(sym, None)
        append_ledger("SELL", sym, qty, px)
        discord_post(embed={"title":"🔴 Filled SELL","fields":[{"name":sym,"value":f"qty: {qty}\nfill: {px:.2f}\nproceeds: ${proceeds:,.2f}"}]})

def do_price(sym):
    quotes = fetch_quotes([sym]); px = quotes.get(sym)
    if px is None: discord_post(f"❌ No price for `{sym}`.")
    else: discord_post(f"📈 `{sym}` = {px:.2f}")

def do_portfolio(ptf):
    if not ptf["positions"]:
        discord_post(embed={"title": f"💼 Cash ${ptf['cash']:,.2f}", "description": "_No positions_"}); return
    quotes = fetch_quotes(list(ptf["positions"].keys()))
    fields=[]; eq=0.0
    for sym, q in ptf["positions"].items():
        px = quotes.get(sym)
        if px is None: continue
        val = q * px; eq += val
        fields.append({"name":sym,"value":f"qty: {q}\npx: {px:.2f}\nval: ${val:,.2f}","inline":True})
    nav = ptf["cash"] + eq
    discord_post(embed={"title": f"💼 NAV ${nav:,.2f} | Cash ${ptf['cash']:,.2f}", "fields": fields[:25]})

# ===== Main =====
def main():
    state = load_state()
    last_id = state.get("last_message_id")
    processed = set(state.get("processed_ids") or [])

    ptf = load_portfolio()
    allow = load_allowlist()

    msgs = discord_get_messages(limit=50, after=last_id)  # newest first
    new_last_id = last_id

    for m in reversed(msgs):  # oldest first for natural order
        mid = m["id"]
        new_last_id = mid

        # skip bots (incl. ourselves)
        if (m.get("author") or {}).get("bot"):
            continue

        # hard dedupe
        if mid in processed:
            continue

        content = (m.get("content") or "").strip()
        parsed = parse_command(content)
        if not parsed:
            continue

        try:
            kind = parsed[0]
            if kind == "order":
                _, side, sym, qty = parsed
                do_order(side, sym, qty, ptf, allow)
                save_portfolio(ptf)
            elif kind == "price":
                _, sym = parsed
                do_price(sym)
            elif kind == "portfolio":
                do_portfolio(ptf)
        finally:
            processed.add(mid)
            try: add_reaction(mid, "✅")
            except Exception: pass

    # persist window + dedupe set
    state["last_message_id"] = new_last_id
    state["processed_ids"] = list(processed)
    save_state(state)

if __name__ == "__main__":
    try:
        main()
    except requests.HTTPError as e:
        print(f"HTTP ERROR: {e} {getattr(e, 'response', None) and e.response.text}", file=sys.stderr)
        sys.exit(1)
