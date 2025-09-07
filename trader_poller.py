import os, sys, json, time, re
from datetime import datetime, timezone
import requests
import yfinance as yf

BOT_TOKEN = os.getenv("DISCORD_BOT_TOKEN")
CHANNEL_ID = os.getenv("DISCORD_CHANNEL_ID")
DEFAULT_CASH = float(os.getenv("DEFAULT_CASH", "100000"))
WEBHOOK = os.getenv("DISCORD_WEBHOOK_URL")  # optional

if not BOT_TOKEN or not CHANNEL_ID:
    print("CONFIG ERROR: Missing DISCORD_BOT_TOKEN or DISCORD_CHANNEL_ID", file=sys.stderr)
    sys.exit(1)

API = "https://discord.com/api/v10"
H = {"Authorization": f"Bot {BOT_TOKEN}"}

PTF_FILE = "data/portfolio.json"
LEDGER_FILE = "data/ledger.csv"
ALLOW_FILE = "data/symbols_allowlist.txt"
STATE_FILE = "data/state.json"

ORDER_RE = re.compile(r"^!(buy|sell)\s+([A-Za-z0-9\.-]{1,10})\s+(\d+)$", re.I)
PRICE_RE = re.compile(r"^!price\s+([A-Za-z0-9\.-]{1,10})$", re.I)

def log(msg): print(f"[{datetime.now(timezone.utc).isoformat(timespec='seconds')}] {msg}")

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

def append_ledger(side, ticker, qty, px):
    try:
        open(LEDGER_FILE, "x").write("timestamp,side,ticker,qty,fill_price,value\n")
    except FileExistsError:
        pass
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
    if after: params["after"] = after  # only messages AFTER this snowflake
    r = requests.get(f"{API}/channels/{CHANNEL_ID}/messages", headers=H, params=params, timeout=20)
    if r.status_code == 403:
        raise SystemExit("PERMISSION ERROR: bot needs View Channel + Read Message History on this channel")
    r.raise_for_status()
    return r.json()  # newest first

def discord_post(content=None, embed=None):
    payload = {}
    if content: payload["content"] = content
    if embed: payload["embeds"] = [embed]
    r = requests.post(f"{API}/channels/{CHANNEL_ID}/messages", headers=H, json=payload, timeout=20)
    if r.status_code == 403:
        raise SystemExit("PERMISSION ERROR: bot needs Send Messages + Embed Links in this channel")
    r.raise_for_status()

def add_reaction(message_id, emoji="✅"):
    url = f"{API}/channels/{CHANNEL_ID}/messages/{message_id}/reactions/{requests.utils.quote(emoji, safe='')}/@me"
    r = requests.put(url, headers=H, timeout=15)
    # 204 on success; ignore errors

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
            log(f"quote error {t}: {e}")
    return out

def parse_command(text):
    s = (text or "").strip()
    m = ORDER_RE.match(s)
    if m: return ("order", m.group(1).lower(), m.group(2).upper(), int(m.group(3)))
    m = PRICE_RE.match(s)
    if m: return ("price", m.group(1).upper())
    if s.lower().startswith("!portfolio"): return ("portfolio",)
    return None

def do_order(side, sym, qty, ptf, allow):
    if sym not in allow:
        discord_post(f"❌ `{sym}` not allowed. Add it to `data/symbols_allowlist.txt`.")
        return
    quotes = fetch_quotes([sym])
    px = quotes.get(sym)
    if px is None:
        discord_post(f"❌ No price for `{sym}` right now.")
        return
    if side == "buy":
        cost = qty * px
        if cost > ptf["cash"]:
            discord_post(f"❌ Need ${cost:,.2f}, only have ${ptf['cash']:,.2f}.")
            return
        ptf["cash"] = round(ptf["cash"] - cost, 2)
        ptf["positions"][sym] = ptf["positions"].get(sym, 0) + qty
        append_ledger("BUY", sym, qty, px)
        discord_post(embed={"title":"🟢 Filled BUY","fields":[{"name":sym,"value":f"qty: {qty}\nfill: {px:.2f}\ncost: ${cost:,.2f}"}]})
    else:
        pos = ptf["positions"].get(sym, 0)
        if qty > pos:
            discord_post(f"❌ You only have {pos} {sym}.")
            return
        proceeds = qty * px
        ptf["cash"] = round(ptf["cash"] + proceeds, 2)
        newq = pos - qty
        if newq: ptf["positions"][sym] = newq
        else: ptf["positions"].pop(sym, None)
        append_ledger("SELL", sym, qty, px)
        discord_post(embed={"title":"🔴 Filled SELL","fields":[{"name":sym,"value":f"qty: {qty}\nfill: {px:.2f}\nproceeds: ${proceeds:,.2f}"}]})

def do_price(sym):
    quotes = fetch_quotes([sym])
    px = quotes.get(sym)
    if px is None: discord_post(f"❌ No price for `{sym}`.")
    else: discord_post(f"📈 `{sym}` = {px:.2f}")

def do_portfolio(ptf):
    if not ptf["positions"]:
        discord_post(embed={"title": f"💼 Cash ${ptf['cash']:,.2f}", "description": "_No positions_"})
        return
    quotes = fetch_quotes(list(ptf["positions"].keys()))
    fields=[]; eq=0.0
    for sym, q in ptf["positions"].items():
        px = quotes.get(sym)
        if px is None: continue
        val = q * px; eq += val
        fields.append({"name":sym,"value":f"qty: {q}\npx: {px:.2f}\nval: ${val:,.2f}","inline":True})
    nav = ptf["cash"] + eq
    discord_post(embed={"title": f"💼 NAV ${nav:,.2f} | Cash ${ptf['cash']:,.2f}", "fields": fields[:25]})

def main():
    state = load_json(STATE_FILE, {"last_message_id": None})
    last_id = state.get("last_message_id")
    ptf = load_portfolio()
    allow = load_allowlist()

    # Fetch messages after the last processed ID
    msgs = discord_get_messages(limit=50, after=last_id)
    log(f"Fetched {len(msgs)} messages after={last_id}")

    # Discord returns newest first; process oldest first
    processed_any = False
    new_last_id = last_id
    for m in reversed(msgs):
        mid = m["id"]
        author = m.get("author", {})
        if author.get("bot"):  # skip bot messages (incl. our own)
            new_last_id = mid
            continue

        content = (m.get("content") or "").strip()
        if not content:
            new_last_id = mid
            continue

        parsed = parse_command(content)
        log(f"msg {mid} content='{content}' parsed={parsed}")
        if parsed:
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
                processed_any = True
                add_reaction(mid, "✅")
            except Exception as e:
                discord_post(f"❌ Error: `{e}`")

        new_last_id = mid

    if processed_any:
        save_json(STATE_FILE, {"last_message_id": new_last_id})
        log(f"Processed messages. Updated last_message_id={new_last_id}")
    else:
        # still bump last_message_id to avoid re-reading ancient history once
        if last_id != new_last_id and new_last_id is not None:
            save_json(STATE_FILE, {"last_message_id": new_last_id})
            log(f"No actionable commands, advanced last_message_id={new_last_id}")
        else:
            log("No actionable commands, state unchanged")

if __name__ == "__main__":
    try:
        main()
    except requests.HTTPError as e:
        print(f"HTTP ERROR: {e} {getattr(e, 'response', None) and e.response.text}", file=sys.stderr)
        sys.exit(1)
