import os, sys, time, re, json
from datetime import datetime, timezone, timedelta
import requests
import yfinance as yf

# --- Env / Discord ---
BOT_TOKEN = os.getenv("DISCORD_BOT_TOKEN")
CHANNEL_ID = os.getenv("DISCORD_CHANNEL_ID")
WEBHOOK = os.getenv("DISCORD_WEBHOOK_URL")  # optional: for summary embeds
DEFAULT_CASH = float(os.getenv("DEFAULT_CASH", "100000"))

if not (BOT_TOKEN and CHANNEL_ID):
    print("Missing DISCORD_BOT_TOKEN or DISCORD_CHANNEL_ID", file=sys.stderr)
    sys.exit(1)

API = "https://discord.com/api/v10"
H = {"Authorization": f"Bot {BOT_TOKEN}"}

# --- Files ---
PTF_FILE = "data/portfolio.json"
LEDGER_FILE = "data/ledger.csv"
ALLOW_FILE = "data/symbols_allowlist.txt"

OK_EXTS = (".jpg",".jpeg",".png",".gif",".webp")

# --- Helpers ---
def now_iso():
    return datetime.now(timezone.utc).isoformat(timespec="seconds")

def load_portfolio():
    try:
        with open(PTF_FILE, "r") as f: return json.load(f)
    except FileNotFoundError:
        ptf = {"cash": DEFAULT_CASH, "positions": {}, "last_mark": None}
        with open(PTF_FILE, "w") as f: json.dump(ptf, f, indent=2)
        return ptf

def save_portfolio(p):
    with open(PTF_FILE, "w") as f: json.dump(p, f, indent=2)

def append_ledger(side, ticker, qty, px):
    with open(LEDGER_FILE, "a") as f:
        f.write(f"{now_iso()},{side},{ticker},{qty},{px},{qty*px}\n")

def load_allowlist():
    try:
        with open(ALLOW_FILE) as f:
            return set(s.strip() for s in f if s.strip())
    except FileNotFoundError:
        return set()

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
                if not hist.empty:
                    px = float(hist["Close"].iloc[-1])
            if px is not None:
                out[t] = float(px)
        except Exception as e:
            print(f"quote error {t}: {e}", file=sys.stderr)
    return out

def post_channel(content=None, embed=None):
    payload = {}
    if content: payload["content"] = content
    if embed: payload["embeds"] = [embed]
    r = requests.post(f"{API}/channels/{CHANNEL_ID}/messages", headers=H, json=payload, timeout=20)
    r.raise_for_status()

def add_reaction(message_id, emoji="✅"):
    url = f"{API}/channels/{CHANNEL_ID}/messages/{message_id}/reactions/{requests.utils.quote(emoji, safe='')}/@me"
    requests.put(url, headers=H, timeout=15)

def get_recent_messages(limit=30):
    r = requests.get(f"{API}/channels/{CHANNEL_ID}/messages", headers=H, params={"limit": limit}, timeout=15)
    r.raise_for_status()
    return r.json()  # newest first

def already_processed(msg):
    for rct in msg.get("reactions", []) or []:
        if rct.get("emoji", {}).get("name") == "✅":
            return True
    return False

def is_recent(ts_iso, minutes=2):
    t = datetime.fromisoformat(ts_iso.replace("Z","+00:00"))
    return datetime.now(timezone.utc) - t <= timedelta(minutes=minutes)

# --- Command parsing ---
ORDER_RE = re.compile(r"^!(buy|sell)\s+([A-Za-z0-9\.-]{1,10})\s+(\d+)$", re.I)
PRICE_RE = re.compile(r"^!price\s+([A-Za-z0-9\.-]{1,10})$", re.I)

def parse(msg_content):
    s = (msg_content or "").strip()
    m = ORDER_RE.match(s)
    if m:
        return ("order", m.group(1).lower(), m.group(2).upper(), int(m.group(3)))
    m = PRICE_RE.match(s)
    if m:
        return ("price", m.group(1).upper())
    if s.lower().startswith("!portfolio"):
        return ("portfolio",)
    return None

# --- Trading ops ---
def do_order(side, sym, qty, ptf, allow):
    if sym not in allow:
        post_channel(f"❌ `{sym}` not allowed. Add it to `data/symbols_allowlist.txt`.")
        return

    quotes = fetch_quotes([sym])
    px = quotes.get(sym)
    if px is None:
        post_channel(f"❌ No price for `{sym}` right now.")
        return

    if side == "buy":
        cost = qty * px
        if cost > ptf["cash"]:
            post_channel(f"❌ Need ${cost:,.2f}, only have ${ptf['cash']:,.2f}.")
            return
        ptf["cash"] = round(ptf["cash"] - cost, 2)
        ptf["positions"][sym] = ptf["positions"].get(sym, 0) + qty
        append_ledger("BUY", sym, qty, px)
        post_channel(embed={"title": "🟢 Filled BUY", "fields":[
            {"name": sym, "value": f"qty: {qty}\nfill: {px:.2f}\ncost: ${cost:,.2f}"}
        ]})
    else:  # sell
        pos = ptf["positions"].get(sym, 0)
        if qty > pos:
            post_channel(f"❌ You only have {pos} {sym}.")
            return
        proceeds = qty * px
        ptf["cash"] = round(ptf["cash"] + proceeds, 2)
        newq = pos - qty
        if newq: ptf["positions"][sym] = newq
        else: ptf["positions"].pop(sym, None)
        append_ledger("SELL", sym, qty, px)
        post_channel(embed={"title": "🔴 Filled SELL", "fields":[
            {"name": sym, "value": f"qty: {qty}\nfill: {px:.2f}\nproceeds: ${proceeds:,.2f}"}
        ]})

def do_price(sym):
    quotes = fetch_quotes([sym])
    px = quotes.get(sym)
    if px is None:
        post_channel(f"❌ No price for `{sym}`.")
    else:
        post_channel(f"📈 `{sym}` = {px:.2f}")

def do_portfolio(ptf):
    if not ptf["positions"]:
        post_channel(embed={"title": f"💼 Cash ${ptf['cash']:,.2f}", "description": "_No positions_"})
        return
    quotes = fetch_quotes(list(ptf["positions"].keys()))
    fields=[]
    total_equity = 0.0
    for sym, q in ptf["positions"].items():
        px = quotes.get(sym)
        if px is None: continue
        val = q * px
        total_equity += val
        fields.append({"name": sym, "value": f"qty: {q}\npx: {px:.2f}\nval: ${val:,.2f}", "inline": True})
    nav = ptf["cash"] + total_equity
    post_channel(embed={"title": f"💼 NAV ${nav:,.2f} | Cash ${ptf['cash']:,.2f}", "fields": fields[:25]})

def main():
    ptf = load_portfolio()
    allow = load_allowlist()

    msgs = get_recent_messages(limit=40)
    # process oldest first so replies are in order
    for m in reversed(msgs):
        if not is_recent(m["timestamp"], minutes=2): 
            continue
        if already_processed(m):
            continue
        author = m.get("author", {}).get("bot")
        if author:  # skip bots (incl. ourselves)
            continue

        parsed = parse(m.get("content",""))
        if not parsed:
            continue

        kind = parsed[0]
        try:
            if kind == "order":
                _, side, sym, qty = parsed
                do_order(side, sym, qty, ptf, allow)
                save_portfolio(ptf)
            elif kind == "price":
                _, sym = parsed
                do_price(sym)
            elif kind == "portfolio":
                do_portfolio(ptf)
            else:
                post_channel("❓ Unknown command.")
        finally:
            try:
                add_reaction(m["id"], "✅")
            except Exception:
                pass

if __name__ == "__main__":
    main()
