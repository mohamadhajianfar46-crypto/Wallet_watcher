import os
import time
import requests
from datetime import datetime, timezone

ADDRESS = os.getenv("WATCH_ADDRESS", "0x7fdafde5cfb5465924316eced2d3715494c517d1").lower()
BOT_TOKEN = os.getenv("BOT_TOKEN")
CHAT_ID = os.getenv("CHAT_ID")
ARBISCAN_KEY = os.getenv("ARBISCAN_API_KEY")

POLL_SECONDS = int(os.getenv("POLL_SECONDS", "20"))  # بدون فیلتر، ولی سریع
ARBISCAN_BASE = "https://api.arbiscan.io/api"
ARBISCAN_TX_URL = "https://arbiscan.io/tx/"

def tg_send(text: str):
    url = f"https://api.telegram.org/bot{BOT_TOKEN}/sendMessage"
    r = requests.post(url, json={"chat_id": CHAT_ID, "text": text, "disable_web_page_preview": True}, timeout=30)
    r.raise_for_status()

def arbiscan(params: dict):
    params = dict(params)
    params["apikey"] = ARBISCAN_KEY
    r = requests.get(ARBISCAN_BASE, params=params, timeout=30)
    r.raise_for_status()
    data = r.json()
    # Etherscan-like APIs sometimes return status "0" but still valid empty results
    return data

def fmt_ts(ts: str) -> str:
    try:
        dt = datetime.fromtimestamp(int(ts), tz=timezone.utc)
        return dt.strftime("%Y-%m-%d %H:%M:%S UTC")
    except Exception:
        return str(ts)

def short(addr: str) -> str:
    if not addr:
        return ""
    addr = addr.lower()
    return addr[:6] + "…" + addr[-4:]

def tx_message(kind: str, item: dict) -> str:
    # kind: NORMAL | ERC20 | ERC721 | ERC1155
    h = item.get("hash") or item.get("transactionHash") or ""
    from_a = (item.get("from") or "").lower()
    to_a = (item.get("to") or "").lower()

    direction = "IN" if to_a == ADDRESS else ("OUT" if from_a == ADDRESS else "OTHER")
    when = fmt_ts(item.get("timeStamp") or item.get("timestamp") or "")
    link = f"{ARBISCAN_TX_URL}{h}" if h else ""

    lines = [f"🔔 {kind} | {direction}", f"Wallet: {short(ADDRESS)}", f"Time: {when}"]

    # NORMAL tx value is in wei (ETH on Arbitrum)
    if kind == "NORMAL":
        try:
            val = int(item.get("value", "0"))
            eth = val / 10**18
            gas = item.get("gasUsed", item.get("gas", ""))
            lines.append(f"From: {short(from_a)}")
            lines.append(f"To:   {short(to_a)}")
            lines.append(f"Value: {eth:.18f} ETH".rstrip("0").rstrip("."))
            if gas:
                lines.append(f"Gas: {gas}")
        except Exception:
            pass

    # Token transfers include tokenSymbol/tokenDecimal/value
    if kind in ("ERC20", "ERC721", "ERC1155"):
        sym = item.get("tokenSymbol") or item.get("tokenName") or ""
        token = (item.get("contractAddress") or "").lower()
        lines.append(f"Token: {sym} ({short(token)})")
        lines.append(f"From:  {short(from_a)}")
        lines.append(f"To:    {short(to_a)}")

        if kind == "ERC20":
            try:
                dec = int(item.get("tokenDecimal", "0") or "0")
                raw = int(item.get("value", "0"))
                amt = raw / (10**dec) if dec >= 0 else raw
                lines.append(f"Amount: {amt}")
            except Exception:
                lines.append(f"Raw: {item.get('value')}")
        else:
            # NFTs
            tid = item.get("tokenID") or item.get("tokenId") or ""
            val = item.get("value")  # for 1155 can be amount
            if tid:
                lines.append(f"TokenID: {tid}")
            if kind == "ERC1155" and val is not None:
                lines.append(f"Amount: {val}")

    if link:
        lines.append(f"Tx: {link}")
    return "\n".join(lines)

def fetch_normal(page=1, offset=50):
    return arbiscan({
        "module": "account",
        "action": "txlist",
        "address": ADDRESS,
        "startblock": 0,
        "endblock": 99999999,
        "page": page,
        "offset": offset,
        "sort": "desc"
    })

def fetch_erc20(page=1, offset=50):
    return arbiscan({
        "module": "account",
        "action": "tokentx",
        "address": ADDRESS,
        "page": page,
        "offset": offset,
        "sort": "desc"
    })

def fetch_erc721(page=1, offset=50):
    return arbiscan({
        "module": "account",
        "action": "tokennfttx",
        "address": ADDRESS,
        "page": page,
        "offset": offset,
        "sort": "desc"
    })

def fetch_erc1155(page=1, offset=50):
    return arbiscan({
        "module": "account",
        "action": "token1155tx",
        "address": ADDRESS,
        "page": page,
        "offset": offset,
        "sort": "desc"
    })

def load_state():
    # ذخیره آخرین آیتم‌هایی که ارسال شده‌اند
    return {"seen": set()}

def key_for(kind: str, item: dict) -> str:
    # برای dedupe: txhash + logIndex (اگر بود)
    h = item.get("hash") or item.get("transactionHash") or ""
    li = item.get("logIndex") or item.get("transactionIndex") or ""
    return f"{kind}:{h}:{li}"

def main():
    if not (BOT_TOKEN and CHAT_ID and ARBISCAN_KEY):
        raise SystemExit("Missing env vars: BOT_TOKEN, CHAT_ID, ARBISCAN_API_KEY")

    state = load_state()

    tg_send(f"✅ Watcher started (Arbitrum)\nAddress: {ADDRESS}\nMode: NO FILTER (ALL TX)")

    # یک بار اولیه: آخرین 10 آیتم رو seen کن تا از قدیمی‌ها اسپم نشه
    def prime(fetch_fn, kind):
        data = fetch_fn(page=1, offset=10)
        res = data.get("result") or []
        if isinstance(res, list):
            for it in res:
                state["seen"].add(key_for(kind, it))

    prime(fetch_normal, "NORMAL")
    prime(fetch_erc20, "ERC20")
    prime(fetch_erc721, "ERC721")
    prime(fetch_erc1155, "ERC1155")
import time

while True:
    try:
        batches = [
            ("NORMAL", fetch_normal),
            ("ERC20", fetch_erc20),
            ("ERC721", fetch_erc721),
            ("ERC1155", fetch_erc1155),
        ]

        for kind, fn in batches:
            data = fn(page=1, offset=25)
            res = data.get("result") or []

            if not isinstance(res, list) or len(res) == 0:
                continue

            new_items = []
            for it in res:
                k = key_for(kind, it)
                if k not in state["seen"]:
                    new_items.append(it)

            # ارسال از قدیمی به جدید برای ترتیب درست
            for it in reversed(new_items):
                state["seen"].add(key_for(kind, it))
                # اگر خواستی پیام بفرستی اینو فعال کن:
                # send_alert(kind, it)

        time.sleep(20)

    except Exception as e:
        print("ERROR:", e)
        time.sleep(5)
