import os, sys, json, time, re, io
from datetime import datetime, timezone
import pandas as pd
import yfinance as yf
import requests

REPO = os.getenv("GITHUB_REPOSITORY")  # owner/repo
GH_TOKEN = os.getenv("GITHUB_TOKEN")   # provided by Actions
WEBHOOK = os.getenv("DISCORD_WEBHOOK_URL")
ISSUE_LABEL = os.getenv("ORDER_LABEL", "paper-trade")
DEFAULT_CASH_FMT = "${:,.2f}"

GH = {
    "Accept": "application/vnd.github+json",
    "Authorization": f"Bearer {GH_TOKEN}"
}

def gh(url, method="GET", **kwargs):
    headers = kwargs.pop("headers", {})
    headers.update(GH)
    r = requests.request(method, url, headers=headers, timeout=20, **kwargs)
    r.raise_for_status()
    return r

def gh_api(path, method="GET", **kwargs):
    base = "https://api.github.com"
    return gh(f"{base}{path}", method, **kwargs)

def load_allowlist():
    with open("data/symbols_allowlist.txt") as f:
        syms = [s.strip() for s in f if s.strip()]
    return set(syms)

def load_portfolio():
    with open("data/portfolio.json") as f:
        return json.load(f)

def save_portfolio(p):
    with open("data/portfolio.json", "w") as f:
        json.dump(p, f, indent=2)

def append_ledger(ts, side, ticker, qty, px):
    line = f'{ts},{side},{ticker},{qty},{px},{qty*px}\n'
    with open("data/ledger.csv", "a") as f:
        f.write(line)

def fetch_quotes(tickers):
    # yfinance fetches multiple symbols efficiently
    if not tickers: return {}
    infos = {}
    data = yf.Tickers(" ".join(tickers))
    for t in tickers:
        info = data.tickers[t].info
        px = info.get("regularMarketPrice") or info.get("currentPrice")
        if px is None:
            # fallback to last close
            hist = data.tickers[t].history(period="2d", interval="1d")
            if not hist.empty:
                px = float(hist["Close"].iloc[-1])
        if px is not None:
            infos[t] = float(px)
    return infos

ORDER_RE = re.compile(r"^/(buy|sell)\s+([\w\.-]{1,10})\s+(\d+)\s*(?:@mkt)?$", re.I)

def parse_order(s):
    """
    /buy AAPL 10
    /sell TSLA 5
    """
    m = ORDER_RE.match(s.strip())
    if not m: return None
    side, ticker, qty = m.group(1).lower(), m.group(2).upper(), int(m.group(3))
    return side, ticker, qty

def get_new_orders():
    # open issues with label
    r = gh_api(f"/repos/{REPO}/issues?state=open&labels={ISSUE_LABEL}&per_page=50")
    issues = r.json()
    orders = []
    for it in issues:
        title = it.get("title","")
        body  = it.get("body","") or ""
        text = title if title.startswith("/") else body
        parsed = parse_order(text)
        if parsed:
            orders.append((it["number"], parsed))
        else:
            # mark invalid
            gh_api(f"/repos/{REPO}/issues/{it['number']}/comments", method="POST",
                   json={"body":"❓ Use `/buy TICKER QTY` or `/sell TICKER QTY`"})
            gh_api(f"/repos/{REPO}/issues/{it['number']}", method="PATCH",
                   json={"labels":[l["name"] for l in it["labels"] if l["name"]!=ISSUE_LABEL]+[f"{ISSUE_LABEL}:invalid"]})
    return orders

def discord_embed(title, description=None, fields=None):
    e = {"title": title}
    if description: e["description"] = description
    if fields: e["fields"] = fields
    return {"username":"Paper Trader","embeds":[e]}

def post_discord(embed):
    if not WEBHOOK: return
    r = requests.post(WEBHOOK, json=embed, timeout=20)
    if r.status_code not in (200,204):
        print("Discord error:", r.status_code, r.text, file=sys.stderr)

def mark_issue_done(num, msg):
    gh_api(f"/repos/{REPO}/issues/{num}/comments", method="POST", json={"body": msg})
    gh_api(f"/repos/{REPO}/issues/{num}", method="PATCH",
           json={"state":"closed"})

def fill_orders():
    allow = load_allowlist()
    ptf = load_portfolio()
    cash = ptf["cash"]
    positions = ptf["positions"]

    # collect and validate orders
    raw = get_new_orders()
    if not raw:
        print("No new orders.")
        return

    tickers = list({ord[1][1] for ord in raw})  # unique symbols
    for t in tickers:
        if t not in allow:
            # reject orders for unknown symbols
            for num, (side, sym, qty) in raw:
                if sym == t:
                    mark_issue_done(num, f"❌ Ticker `{sym}` not allowed. Add it to `data/symbols_allowlist.txt`.")
            # remove rejected from processing
    raw = [o for o in raw if o[1][1] in allow]
    tickers = list({ord[1][1] for ord in raw})

    quotes = fetch_quotes(tickers)
    now = datetime.now(timezone.utc).isoformat(timespec="seconds")

    fills = []
    for num, (side, sym, qty) in raw:
        px = quotes.get(sym)
        if px is None:
            mark_issue_done(num, f"❌ No price for `{sym}` right now.")
            continue

        if side == "buy":
            cost = qty * px
            if cost > cash + 1e-9:
                mark_issue_done(num, f"❌ Insufficient cash. Need {DEFAULT_CASH_FMT.format(cost)}; have {DEFAULT_CASH_FMT.format(cash)}.")
                continue
            cash -= cost
            positions[sym] = positions.get(sym, 0) + qty
            append_ledger(now, "BUY", sym, qty, px)
            fills.append((side, sym, qty, px))
            mark_issue_done(num, f"✅ Bought **{qty} {sym}** @ {px:.2f}  (cost {DEFAULT_CASH_FMT.format(cost)})")

        elif side == "sell":
            pos = positions.get(sym, 0)
            if qty > pos:
                mark_issue_done(num, f"❌ Not enough shares of `{sym}`. You have {pos}.")
                continue
            value = qty * px
            cash += value
            positions[sym] = pos - qty
            if positions[sym] == 0:
                positions.pop(sym, None)
            append_ledger(now, "SELL", sym, qty, px)
            fills.append((side, sym, qty, px))
            mark_issue_done(num, f"✅ Sold **{qty} {sym}** @ {px:.2f}  (proceeds {DEFAULT_CASH_FMT.format(value)})")

    # Save portfolio
    ptf["cash"] = round(cash, 2)
    ptf["positions"] = positions
    save_portfolio(ptf)

    # Post a compact fill summary
    if fills:
        fields = [{"name": f"{side.upper()} {sym}", "value": f"qty: {qty}\nfill: {px:.2f}"} for (side,sym,qty,px) in fills]
        post_discord(discord_embed("🧾 Order Fills", fields=fields))

def mark_to_market():
    ptf = load_portfolio()
    pos = ptf["positions"]
    tickers = list(pos.keys())
    quotes = fetch_quotes(tickers)
    eq = 0.0
    fields=[]
    for t, q in pos.items():
        px = quotes.get(t)
        if px is None: continue
        val = px * q
        eq += val
        fields.append({"name": t, "value": f"qty: {q}\npx: {px:.2f}\nval: {DEFAULT_CASH_FMT.format(val)}", "inline": True})
    nav = ptf["cash"] + eq
    title = f"📈 Daily Mark — NAV {DEFAULT_CASH_FMT.format(nav)} | Cash {DEFAULT_CASH_FMT.format(ptf['cash'])}"
    post_discord(discord_embed(title, fields=fields[:25]))  # Discord limit

if __name__ == "__main__":
    mode = os.getenv("MODE", "both")  # "fills" | "mark" | "both"
    if mode in ("fills","both"):
        fill_orders()
    if mode in ("mark","both"):
        # Only mark once per day in the morning (UTC 08:15) by default; you can call with MODE=mark
        pass
