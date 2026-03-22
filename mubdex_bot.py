"""
⚡ MUB DEX Bot v2.0 — Trojan-style Solana Trading Bot
======================================================
Features:
  ✅ Inline keyboard buttons (no typing commands)
  ✅ Paste contract → instant token info + buy/sell
  ✅ Auto TP/SL with notifications
  ✅ Sniper bot (buy on new listing)
  ✅ Limit orders
  ✅ Fee transparency
  ✅ Feedback system
  ✅ No private key required for viewing

SETUP:
  pip install pyTelegramBotAPI requests solders base58
  Set BOT_TOKEN below or env var BOT_TOKEN
  python mubdex_bot.py
"""

import os, json, time, base64, struct, hashlib, threading, logging
import requests
from datetime import datetime

import telebot
from telebot import types  # for InlineKeyboardMarkup

logging.basicConfig(level=logging.INFO,
    format="%(asctime)s %(levelname)s %(message)s")

# ══════════════════════════════════════════════════════════
#  CONFIG
# ══════════════════════════════════════════════════════════
import os as _os
BOT_TOKEN  = os.environ.get("BOT_TOKEN", " ")
ADMIN_ID   = int(os.environ.get("ADMIN_ID", " "))
PROXY_URL  = os.environ.get("PROXY_URL", "")

RPC_URL                  = os.environ.get("RPC_URL",
    "https://mainnet.helius-rpc.com/?api-key=92d43c65-101f-4053-a457-615a230bfd64")
JUPITER_REFERRAL_ACCOUNT = "EqwndckH8GvXoWT1vp5nTqD7KbJPzCEGWnC9XrfqW41x"
JUPITER_FEE_BPS          = 50
PRIORITY_FEE             = 100_000   # lamports
SOL_MINT = "So11111111111111111111111111111111111111112"
SPL_PROG = "TokenkegQfeZyiNwAJbNbGKPFXCWuBvf9Ss623VQ5DA"

JUPITER_PAIRS = [
    {"q":"https://lite-api.jup.ag/swap/v1/quote",
     "s":"https://lite-api.jup.ag/swap/v1/swap",   "n":"lite"},
    {"q":"https://public.jupiterapi.com/quote",
     "s":"https://public.jupiterapi.com/swap",      "n":"public"},
    {"q":"https://quote-api.jup.ag/v6/quote",
     "s":"https://quote-api.jup.ag/v6/swap",        "n":"v6"},
]
ULTRA_Q = "https://lite-api.jup.ag/ultra/v1/order"
ULTRA_S = "https://lite-api.jup.ag/ultra/v1/execute"
DEX_URL = "https://api.dexscreener.com/latest/dex/tokens/{}"

KNOWN = {
    "SOL" : SOL_MINT,
    "USDC": "EPjFWdd5AufqSSqeM2qN1xzybapC8G4wEGGkZwyTDt1v",
    "USDT": "Es9vMFrzaCERmJfrF4H2FYD4KCoNkY11McCe8BenwNYB",
    "BONK": "DezXAZ8z7PnrnRJjz3wXBoRgixCa6xjnB7YaB1pPB263",
    "WIF" : "EKpQGSJtjMFqKZ9KQanSqYXRcF8fBopzLHYxdM65zcjm",
    "JUP" : "JUPyiwrYJFskUPiHa7hkeR8VUtAeFoSYbKedZNsDvCN",
}

DATA_FILE     = "mubdex_users.json"
FEEDBACK_FILE = "mubdex_feedback.json"

# ── Solders ────────────────────────────────────────────────
try:
    from solders.keypair        import Keypair
    from solders.transaction    import VersionedTransaction
    from solders.pubkey         import Pubkey
    from solders.system_program import transfer, TransferParams
    from solders.message        import Message
    from solders.hash           import Hash
    from solders.transaction    import Transaction as LegacyTx
    import base58
    SOLDERS_OK = True
except ImportError:
    SOLDERS_OK = False
    logging.warning("solders not installed — trading disabled")

# ══════════════════════════════════════════════════════════
#  USER STATE
# ══════════════════════════════════════════════════════════
"""
User state dict:
{
  "keypair"  : Keypair | None,
  "pubkey"   : str | None,
  "pk_b58"   : str | None,
  "state"    : str,        # idle | awaiting_pk | awaiting_contract |
                           #        awaiting_buy_amt | awaiting_sell_pct |
                           #        awaiting_limit | awaiting_sniper |
                           #        awaiting_feedback
  "ctx"      : dict,       # temporary context for multi-step flows
  "settings" : dict,       # default_buy, slippage, auto_tp, auto_sl
}
"""

_users   = {}   # { uid: state_dict }
_traders = {}   # { uid: AutoTrader }
_snipers = {}   # { uid: SniperTask }
_limits  = {}   # { uid: [LimitOrder, ...] }

def _xor(d, k): return bytes(b ^ k[i%len(k)] for i,b in enumerate(d))

def save_users():
    out = {}
    for uid, u in _users.items():
        entry = {"settings": u.get("settings", {})}
        if u.get("pk_b58"):
            k   = hashlib.sha256(str(uid).encode()).digest()
            enc = base64.b64encode(_xor(u["pk_b58"].encode(), k)).decode()
            entry["enc"] = enc
        if u.get("view_pub"):
            entry["view_pub"] = u["view_pub"]  # public key — no encryption needed
        if entry.get("enc") or entry.get("view_pub"):
            out[str(uid)] = entry
    json.dump(out, open(DATA_FILE,"w"))

def load_users():
    if not os.path.exists(DATA_FILE): return
    try:
        data = json.load(open(DATA_FILE))
        for uid_s, d in data.items():
            uid = int(uid_s)
            k   = hashlib.sha256(str(uid).encode()).digest()
            pk  = _xor(base64.b64decode(d["enc"]), k).decode()
            try:
                kp = Keypair.from_bytes(base58.b58decode(pk))
                _users[uid] = _new_user(kp, str(kp.pubkey()), pk)
                _users[uid]["settings"] = d.get("settings", _default_settings())
                if d.get("view_pub"):
                    _users[uid]["view_pub"] = d["view_pub"]
            except Exception:
                # Maybe it's a view-only user (no pk)
                pass
            if d.get("view_pub") and uid not in _users:
                _users[uid] = _new_user()
                _users[uid]["view_pub"]  = d["view_pub"]
                _users[uid]["settings"]  = d.get("settings", _default_settings())
    except Exception:
        pass

def _default_settings():
    return {
        "default_buy" : 0.1,   # SOL
        "slippage"    : 1.0,   # %
        "auto_tp"     : 20,    # %
        "auto_sl"     : 10,    # %
        "engine"      : "ultra",  # ultra | normal
    }

def _new_user(kp=None, pub=None, pk=None):
    return {
        "keypair"   : kp,       # Keypair — only if trade mode
        "pubkey"    : pub,      # trade wallet pubkey
        "view_pub"  : None,     # view-only wallet pubkey (any wallet address)
        "pk_b58"    : pk,
        "state"     : "idle",
        "ctx"       : {},
        "settings"  : _default_settings(),
    }

def u(uid):
    if uid not in _users:
        _users[uid] = _new_user()
    return _users[uid]

def has_wallet(uid):
    """Has trade wallet (keypair)."""
    return _users.get(uid,{}).get("keypair") is not None

def has_view(uid):
    """Has any wallet connected (view or trade)."""
    usr = _users.get(uid,{})
    return usr.get("keypair") is not None or usr.get("view_pub") is not None

def active_pub(uid):
    """Return the best available public key for reading balance etc."""
    usr = _users.get(uid,{})
    return usr.get("pubkey") or usr.get("view_pub")

# ══════════════════════════════════════════════════════════
#  RPC / CHAIN
# ══════════════════════════════════════════════════════════
def rpc(method, params):
    r = requests.post(RPC_URL,
        json={"jsonrpc":"2.0","id":1,"method":method,"params":params},
        timeout=12)
    return r.json()

def sol_bal(pub):
    try: return rpc("getBalance",[pub,{"commitment":"confirmed"}])["result"]["value"]/1e9
    except: return 0.0

def token_accs(pub):
    try:
        res = rpc("getTokenAccountsByOwner",
            [pub,{"programId":SPL_PROG},{"encoding":"jsonParsed"}])
        out=[]
        for a in res["result"]["value"]:
            inf=a["account"]["data"]["parsed"]["info"]
            amt=inf["tokenAmount"]
            if float(amt.get("uiAmount") or 0)>0:
                out.append({"mint":inf["mint"],"amount":amt["uiAmountString"],
                            "raw":amt["amount"],"decimals":amt["decimals"]})
        return out
    except: return []

def tok_dec(mint):
    try: return rpc("getTokenSupply",[mint])["result"]["value"]["decimals"]
    except: return 6

def blockhash():
    return rpc("getLatestBlockhash",
        [{"commitment":"processed"}])["result"]["value"]["blockhash"]

def send_raw(raw_b64):
    r = rpc("sendTransaction",[raw_b64,{
        "encoding":"base64","skipPreflight":True,
        "preflightCommitment":"processed","maxRetries":5}])
    if "error" in r: raise RuntimeError(str(r["error"]))
    return r["result"]

def sign_send(tx_bytes, kp):
    for attempt in range(3):
        try:
            tx  = VersionedTransaction.from_bytes(tx_bytes)
            sgn = VersionedTransaction(tx.message, [kp])
            return send_raw(base64.b64encode(bytes(sgn)).decode())
        except RuntimeError as e:
            if "BlockhashNotFound" in str(e) and attempt<2:
                time.sleep(0.8); continue
            raise

def sol_price_usd():
    try:
        r = requests.get(
            "https://api.coingecko.com/api/v3/simple/price"
            "?ids=solana&vs_currencies=usd", timeout=6)
        return float(r.json()["solana"]["usd"])
    except: return 0.0

# ══════════════════════════════════════════════════════════
#  DEXSCREENER — Token Info
# ══════════════════════════════════════════════════════════
def token_info(mint):
    """Returns rich token info dict from DexScreener."""
    res = {"found":False,"name":"Unknown","sym":"?","price_usd":None,
           "mc":None,"liq":0,"vol24":0,"chg1h":0,"chg24":0,
           "risk":"UNKNOWN","url":"","holders":None,"mint":mint,
           "buys24":0,"sells24":0}
    try:
        r = requests.get(DEX_URL.format(mint), timeout=8)
        if r.status_code != 200: return res
        pairs = r.json().get("pairs") or []
        if not pairs: return res
        p = max(pairs, key=lambda x: float(x.get("liquidity",{}).get("usd",0) or 0))
        res["found"]    = True
        res["name"]     = p.get("baseToken",{}).get("name","Unknown")
        res["sym"]      = p.get("baseToken",{}).get("symbol","?")
        res["price_usd"]= p.get("priceUsd")
        res["mc"]       = p.get("marketCap") or p.get("fdv")
        res["liq"]      = float(p.get("liquidity",{}).get("usd",0) or 0)
        res["vol24"]    = float(p.get("volume",{}).get("h24",0) or 0)
        res["chg1h"]    = float(p.get("priceChange",{}).get("h1",0) or 0)
        res["chg24"]    = float(p.get("priceChange",{}).get("h24",0) or 0)
        res["url"]      = p.get("url","")
        txns = p.get("txns",{}).get("h24",{})
        res["buys24"]   = txns.get("buys",0)
        res["sells24"]  = txns.get("sells",0)
        # Risk assessment
        w = []
        if res["liq"] < 1000:   w.append("🚨 Very low liquidity")
        elif res["liq"] < 5000: w.append("⚠️ Low liquidity")
        if res["liq"] == 0:     w.append("🚨 No liquidity — possible rug!")
        if res["buys24"]+res["sells24"] < 10: w.append("⚠️ Very few transactions")
        sells = res["sells24"]
        buys  = res["buys24"]
        if buys > 0 and sells == 0: w.append("⚠️ No sells — possible honeypot!")
        res["warnings"] = w
        if any("🚨" in x for x in w): res["risk"] = "HIGH"
        elif any("⚠️" in x for x in w): res["risk"] = "MEDIUM"
        else: res["risk"] = "LOW"
    except Exception as e:
        res["error"] = str(e)
    return res

def fmt_token_card(info, sol_usd=0):
    """Format a beautiful token info card for Telegram."""
    sym  = info["sym"]
    name = info["name"]
    risk_icon = {"LOW":"✅","MEDIUM":"⚠️","HIGH":"🚨","UNKNOWN":"❓"}.get(info["risk"],"❓")
    chg1h  = info.get("chg1h",0) or 0
    chg24  = info.get("chg24",0) or 0
    chg1h_str  = f"{'🟢' if chg1h>=0 else '🔴'} {chg1h:+.2f}%"
    chg24_str  = f"{'🟢' if chg24>=0 else '🔴'} {chg24:+.2f}%"

    price_str = f"${float(info['price_usd']):.8f}" if info.get("price_usd") else "N/A"
    mc_str    = f"${float(info['mc'])/1e6:.2f}M"  if info.get("mc") and float(info.get("mc",0))>=1e6 \
                else f"${float(info['mc'])/1e3:.1f}K" if info.get("mc") else "N/A"
    liq_str   = f"${info['liq']:,.0f}"
    vol_str   = f"${info['vol24']:,.0f}"

    lines = [
        f"⚡ *{name}* (${sym})",
        f"`{info['mint'][:20]}…`",
        "",
        f"💲 Price:    `{price_str}`",
        f"📊 Mkt Cap:  `{mc_str}`",
        f"💧 Liq:      `{liq_str}`",
        f"📈 Vol 24h:  `{vol_str}`",
        "",
        f"⏱ 1h:  {chg1h_str}",
        f"📅 24h: {chg24_str}",
        "",
        f"🔁 Buys: {info['buys24']}  Sells: {info['sells24']}",
        f"{risk_icon} Risk: *{info['risk']}*",
    ]
    if info.get("warnings"):
        lines.append("")
        for w in info["warnings"]:
            lines.append(w)
    if info.get("url"):
        lines.append(f"\n[📊 DexScreener]({info['url']})")
    return "\n".join(lines)

# ══════════════════════════════════════════════════════════
#  SWAP ENGINE
# ══════════════════════════════════════════════════════════
def do_swap(kp, fm, tm, raw_in):
    """Ultra → Normal Jupiter → Raydium. Returns (txid, out_ui, gasless)."""
    pub = str(kp.pubkey())

    # Ultra
    try:
        params = {"inputMint":fm,"outputMint":tm,"amount":raw_in,"taker":pub}
        if JUPITER_REFERRAL_ACCOUNT:
            params["referralAccount"] = JUPITER_REFERRAL_ACCOUNT
            params["referralFeeBps"]  = JUPITER_FEE_BPS
        r = requests.get(ULTRA_Q, params=params, timeout=12)
        if r.status_code == 200:
            order = r.json()
            if "transaction" in order:
                txid = sign_send(base64.b64decode(order["transaction"]), kp)
                gasless = order.get("gasless", False)
                out_raw = int(order.get("outAmount",0))
                out_dec = 9 if tm==SOL_MINT else tok_dec(tm)
                return txid, out_raw/(10**out_dec), gasless
    except Exception:
        pass

    # Normal Jupiter — slippage ladder 100→300→500→1000 bps
    last_err = "Unknown"
    for slip in [100, 300, 500, 1000]:
        try:
            for pair in JUPITER_PAIRS:
                r = requests.get(pair["q"], params={
                    "inputMint":fm,"outputMint":tm,
                    "amount":raw_in,"slippageBps":slip}, timeout=12)
                if r.status_code != 200: continue
                q = r.json()
                if "error" in q or "errorCode" in q: continue
                payload = {
                    "quoteResponse":q,"userPublicKey":pub,
                    "wrapAndUnwrapSol":True,"prioritizationFeeLamports":PRIORITY_FEE,
                    "dynamicComputeUnitLimit":True,"skipUserAccountsCheck":True,
                }
                if JUPITER_REFERRAL_ACCOUNT:
                    payload["feeAccount"] = JUPITER_REFERRAL_ACCOUNT
                r2 = requests.post(pair["s"], json=payload, timeout=25)
                if r2.status_code != 200: continue
                d = r2.json()
                if "swapTransaction" not in d: continue
                txid = sign_send(base64.b64decode(d["swapTransaction"]), kp)
                # ── Verify TX on-chain ─────────────────────────
                time.sleep(3)
                try:
                    res = rpc("getSignatureStatuses",
                        [[txid],{"searchTransactionHistory":True}])
                    val = res["result"]["value"][0]
                    if val and val.get("err"):
                        err_info = val["err"]
                        # Custom error 6014 = slippage — retry with higher
                        if "6014" in str(err_info) or "Custom" in str(err_info):
                            last_err = f"Slippage {slip/100:.1f}% too low (err:{err_info})"
                            break  # try higher slippage
                        raise RuntimeError(f"TX failed on-chain: {err_info}")
                except RuntimeError:
                    raise
                except Exception:
                    pass  # can't verify but assume OK
                out_raw = int(q.get("outAmount",0))
                out_dec = 9 if tm==SOL_MINT else tok_dec(tm)
                return txid, out_raw/(10**out_dec), False
        except RuntimeError:
            raise
        except Exception as e:
            last_err = str(e)
            if "custom': 1" in last_err.lower() or "6014" in last_err:
                continue  # retry with higher slippage
            break
    raise RuntimeError(f"Swap failed after all retries: {last_err}")

# ══════════════════════════════════════════════════════════
#  KEYBOARDS — The heart of the UX
# ══════════════════════════════════════════════════════════

def kb_main():
    """Main menu keyboard."""
    k = types.InlineKeyboardMarkup(row_width=2)
    k.add(
        types.InlineKeyboardButton("💼 Wallet",    callback_data="menu_wallet"),
        types.InlineKeyboardButton("💱 Buy / Sell", callback_data="menu_trade"),
    )
    k.add(
        types.InlineKeyboardButton("🤖 Auto-Trader",  callback_data="menu_auto"),
        types.InlineKeyboardButton("🎯 Sniper Bot",   callback_data="menu_sniper"),
    )
    k.add(
        types.InlineKeyboardButton("📋 Limit Orders", callback_data="menu_limits"),
        types.InlineKeyboardButton("⚙️ Settings",     callback_data="menu_settings"),
    )
    k.add(
        types.InlineKeyboardButton("💬 Feedback",     callback_data="menu_feedback"),
        types.InlineKeyboardButton("❓ Help",          callback_data="menu_help"),
    )
    return k

def kb_wallet(uid):
    k = types.InlineKeyboardMarkup(row_width=2)
    if has_view(uid) or has_wallet(uid):
        k.add(
            types.InlineKeyboardButton("💰 Balance",     callback_data="w_balance"),
            types.InlineKeyboardButton("📋 Address",     callback_data="w_address"),
        )
        if has_wallet(uid):
            k.add(
                types.InlineKeyboardButton("📤 Send SOL",    callback_data="w_send"),
                types.InlineKeyboardButton("⚡ Trade Wallet", callback_data="w_trade_info"),
            )
        else:
            # View-only — nudge to add trade wallet
            k.add(types.InlineKeyboardButton(
                "⚡ Add Trade Wallet (for buying)", callback_data="w_add_trade"))
        k.add(types.InlineKeyboardButton("🔌 Change Wallet", callback_data="w_connect"))
    else:
        # Not connected at all — show both options
        k.add(types.InlineKeyboardButton(
            "👁 View Wallet (just share address)", callback_data="w_view_only"))
        k.add(types.InlineKeyboardButton(
            "⚡ Trade Wallet (import private key)", callback_data="w_add_trade"))
    k.add(types.InlineKeyboardButton("🔙 Back", callback_data="menu_main"))
    return k

def kb_trade(info, uid):
    """Buy/Sell keyboard shown after pasting contract."""
    sym = info["sym"]
    settings = u(uid)["settings"]
    default_buy = settings.get("default_buy", 0.1)
    k = types.InlineKeyboardMarkup(row_width=3)
    # Buy buttons
    for amt in [0.01, 0.05, default_buy]:
        k.add(types.InlineKeyboardButton(
            f"🟢 Buy {amt} SOL",
            callback_data=f"buy_{amt}_{info['mint']}"
        ))
    k.add(
        types.InlineKeyboardButton("🟢 Buy custom", callback_data=f"buy_custom_{info['mint']}"),
        types.InlineKeyboardButton("🔴 Sell 50%",   callback_data=f"sell_50_{info['mint']}"),
        types.InlineKeyboardButton("🔴 Sell 100%",  callback_data=f"sell_100_{info['mint']}"),
    )
    k.add(
        types.InlineKeyboardButton("📋 Limit Order", callback_data=f"limit_{info['mint']}"),
        types.InlineKeyboardButton("🔔 Set Alert",   callback_data=f"alert_{info['mint']}"),
    )
    k.add(
        types.InlineKeyboardButton("🛡️ Safety Check", callback_data=f"safety_{info['mint']}"),
        types.InlineKeyboardButton("📊 DexScreener",  url=info.get("url","https://dexscreener.com")),
    )
    k.add(types.InlineKeyboardButton("🔙 Main Menu", callback_data="menu_main"))
    return k

def kb_auto(uid):
    running = uid in _traders and _traders[uid].running
    k = types.InlineKeyboardMarkup(row_width=2)
    if running:
        t = _traders[uid]
        k.add(types.InlineKeyboardButton("📊 Status", callback_data="auto_status"))
        k.add(
            types.InlineKeyboardButton("⏹ Stop + Sell", callback_data="auto_stop_sell"),
            types.InlineKeyboardButton("⏹ Stop Only",   callback_data="auto_stop_only"),
        )
    else:
        k.add(types.InlineKeyboardButton("▶️ Start Auto-Trader", callback_data="auto_start"))
    k.add(types.InlineKeyboardButton("🔙 Back", callback_data="menu_main"))
    return k

def kb_settings(uid):
    s = u(uid)["settings"]
    k = types.InlineKeyboardMarkup(row_width=2)
    k.add(
        types.InlineKeyboardButton(
            f"💰 Default Buy: {s['default_buy']} SOL",
            callback_data="set_default_buy"),
        types.InlineKeyboardButton(
            f"📉 Slippage: {s['slippage']}%",
            callback_data="set_slippage"),
    )
    k.add(
        types.InlineKeyboardButton(
            f"🎯 Auto TP: {s['auto_tp']}%",
            callback_data="set_tp"),
        types.InlineKeyboardButton(
            f"🛑 Auto SL: {s['auto_sl']}%",
            callback_data="set_sl"),
    )
    eng = s.get("engine","ultra")
    k.add(types.InlineKeyboardButton(
        f"⚡ Engine: {'Ultra ✅' if eng=='ultra' else 'Normal'}",
        callback_data="set_engine_ultra"),
    )
    k.add(types.InlineKeyboardButton(
        f"🔄 Engine: {'Normal ✅' if eng=='normal' else 'Normal'}",
        callback_data="set_engine_normal"),
    )
    k.add(types.InlineKeyboardButton("🔙 Back", callback_data="menu_main"))
    return k

def kb_back_main():
    k = types.InlineKeyboardMarkup()
    k.add(types.InlineKeyboardButton("🔙 Main Menu", callback_data="menu_main"))
    return k

# ══════════════════════════════════════════════════════════
#  AUTO TRADER
# ══════════════════════════════════════════════════════════
class AutoTrader:
    def __init__(self, uid, bot):
        self.uid = uid; self.bot = bot
        self.running = False; self.thread = None
        self.cfg = {}; self.entry = None; self._bought = False

    def notify(self, msg, kb=None):
        try:
            self.bot.send_message(self.uid, msg,
                parse_mode="Markdown", reply_markup=kb)
        except Exception: pass

    def start(self, cfg):
        if self.running: return
        self.cfg = cfg; self.running = True
        self.thread = threading.Thread(target=self._loop, daemon=True)
        self.thread.start()
        self.notify(
            f"🤖 *Auto-Trader Started!*\n\n"
            f"🪙 Token: `{cfg['mint'][:20]}…`\n"
            f"💰 Invest: `{cfg['sol']} SOL`\n"
            f"🎯 Take-Profit: `+{cfg['tp']}%`\n"
            f"🛑 Stop-Loss: `-{cfg['sl']}%`\n"
            f"⏱ Check every: `{cfg['interval']}s`\n\n"
            f"_I'll notify you on every action. Use buttons to stop._",
            kb=kb_auto(self.uid)
        )

    def stop(self, sell=True):
        self.running = False
        if sell and self._bought:
            self.notify("🔴 *Stopping — selling tokens now…*")
            threading.Thread(target=self._sell, daemon=True).start()
        else:
            self.notify("⏹ *Auto-Trader stopped.*", kb=kb_main())
            self._bought = False

    def _price(self):
        try:
            mint = self.cfg["mint"]
            dec  = tok_dec(mint)
            r    = requests.get(JUPITER_PAIRS[0]["q"], params={
                "inputMint":mint,"outputMint":SOL_MINT,
                "amount":int(10**dec),"slippageBps":100}, timeout=10)
            q = r.json()
            return int(q["outAmount"])/1e9 if "outAmount" in q else None
        except: return None

    def _sell(self):
        try:
            kp   = _users[self.uid]["keypair"]
            pub  = _users[self.uid]["pubkey"]
            toks = token_accs(pub)
            t    = next((x for x in toks if x["mint"]==self.cfg["mint"]),None)
            if not t or int(t["raw"])==0:
                self.notify("⚠️ Balance is 0 — nothing to sell"); return
            txid, out, _ = do_swap(kp, self.cfg["mint"], SOL_MINT, int(t["raw"]))
            self._bought = False
            self.notify(
                f"✅ *Sold!*\nGot: `{out:.5f} SOL`\n"
                f"[View TX](https://solscan.io/tx/{txid})",
                kb=kb_main()
            )
        except Exception as e:
            self.notify(f"❌ Sell error: {e}", kb=kb_main())

    def _loop(self):
        mint = self.cfg["mint"]
        bought = False
        while self.running:
            try:
                p = self._price()
                if p is None:
                    time.sleep(self.cfg["interval"]); continue
                if not bought:
                    kp  = _users[self.uid]["keypair"]
                    raw = int(float(self.cfg["sol"])*1e9)
                    txid, out_ui, gasless = do_swap(kp, SOL_MINT, mint, raw)
                    self.entry = p; bought = True; self._bought = True
                    self.notify(
                        f"✅ *Bought!*"
                        f"{'  ⚡ GASLESS!' if gasless else ''}\n"
                        f"Price: `{p:.8f} SOL`\n"
                        f"Got: `{out_ui:.4f}` tokens\n"
                        f"[TX](https://solscan.io/tx/{txid})"
                    )
                else:
                    chg = ((p-self.entry)/self.entry)*100
                    icon = "📈" if chg>=0 else "📉"
                    if chg >= float(self.cfg["tp"]):
                        self.notify(f"🎯 *Take-Profit hit! +{chg:.2f}%*\nSelling…")
                        self._sell(); bought=False; self.entry=None
                    elif chg <= -float(self.cfg["sl"]):
                        self.notify(f"🛑 *Stop-Loss hit! {chg:.2f}%*\nSelling…")
                        self._sell(); bought=False; self.entry=None
                time.sleep(self.cfg["interval"])
            except Exception as e:
                self.notify(f"❌ Loop error: {e}")
                time.sleep(self.cfg["interval"])

# ══════════════════════════════════════════════════════════
#  SNIPER BOT
# ══════════════════════════════════════════════════════════
class Sniper:
    def __init__(self, uid, bot):
        self.uid = uid; self.bot = bot
        self.running = False; self.thread = None; self.cfg = {}

    def notify(self, msg):
        try: self.bot.send_message(self.uid, msg, parse_mode="Markdown")
        except: pass

    def start(self, mint, sol_amount, max_mc=None):
        self.cfg = {"mint":mint,"sol":sol_amount,"max_mc":max_mc}
        self.running = True
        self.thread  = threading.Thread(target=self._watch, daemon=True)
        self.thread.start()
        self.notify(
            f"🎯 *Sniper Armed!*\n\n"
            f"Target: `{mint[:20]}…`\n"
            f"Amount: `{sol_amount} SOL`\n"
            f"Max MC: `{'$'+str(max_mc) if max_mc else 'Any'}`\n\n"
            f"_I'm watching for liquidity. Will buy instantly when detected!_"
        )

    def stop(self):
        self.running = False
        self.notify("🎯 Sniper stopped.")

    def _watch(self):
        """Poll DexScreener until liquidity appears, then buy."""
        mint = self.cfg["mint"]
        self.notify(f"👁 Watching `{mint[:16]}…` for liquidity…")
        while self.running:
            try:
                info = token_info(mint)
                if info["found"] and info["liq"] > 500:
                    mc = float(info.get("mc") or 0)
                    max_mc = self.cfg.get("max_mc")
                    if max_mc and mc > max_mc*1000:
                        self.notify(f"⚠️ MC ${mc/1000:.1f}K > max ${max_mc}K — skip")
                        self.running = False; break
                    # BUY!
                    self.notify(
                        f"🎯 *SNIPE! Liquidity detected!*\n"
                        f"Liq: ${info['liq']:,.0f}  MC: ${mc/1000:.1f}K\n"
                        f"Buying {self.cfg['sol']} SOL…"
                    )
                    kp  = _users[self.uid]["keypair"]
                    raw = int(self.cfg["sol"]*1e9)
                    txid, out, gasless = do_swap(kp, SOL_MINT, mint, raw)
                    self.notify(
                        f"✅ *Sniped!*{'  ⚡ GASLESS!' if gasless else ''}\n"
                        f"Got: `{out:.4f}` tokens\n"
                        f"[TX](https://solscan.io/tx/{txid})"
                    )
                    self.running = False; break
                time.sleep(2)
            except Exception as e:
                time.sleep(3)

# ══════════════════════════════════════════════════════════
#  LIMIT ORDERS  (polled every 15s)
# ══════════════════════════════════════════════════════════
class LimitOrder:
    def __init__(self, uid, mint, direction, target_price, sol_amount, bot):
        self.uid = uid; self.mint = mint
        self.direction    = direction    # "above" | "below"
        self.target_price = target_price # USD
        self.sol_amount   = sol_amount
        self.bot          = bot
        self.active       = True
        self.thread       = threading.Thread(target=self._watch, daemon=True)
        self.thread.start()

    def _price_usd(self):
        try:
            info = token_info(self.mint)
            return float(info["price_usd"]) if info.get("price_usd") else None
        except: return None

    def _watch(self):
        while self.active:
            try:
                p = self._price_usd()
                if p:
                    triggered = (self.direction=="above" and p >= self.target_price) or \
                                (self.direction=="below" and p <= self.target_price)
                    if triggered:
                        self.active = False
                        try:
                            self.bot.send_message(
                                self.uid,
                                f"📋 *Limit Order Triggered!*\n"
                                f"Price: `${p:.8f}`\n"
                                f"Target: `${self.target_price:.8f}`\n"
                                f"Executing buy of {self.sol_amount} SOL…",
                                parse_mode="Markdown"
                            )
                            kp   = _users[self.uid]["keypair"]
                            raw  = int(self.sol_amount*1e9)
                            txid, out, gasless = do_swap(kp, SOL_MINT, self.mint, raw)
                            self.bot.send_message(
                                self.uid,
                                f"✅ *Limit Order Filled!*\n"
                                f"Got: `{out:.4f}` tokens\n"
                                f"[TX](https://solscan.io/tx/{txid})",
                                parse_mode="Markdown"
                            )
                        except Exception as e:
                            self.bot.send_message(self.uid, f"❌ Limit order failed: {e}")
                        break
                time.sleep(15)
            except Exception:
                time.sleep(15)

# ══════════════════════════════════════════════════════════
#  BOT INSTANCE
# ══════════════════════════════════════════════════════════
if PROXY_URL:
    import telebot.apihelper as _ah
    _ah.proxy = {"https":PROXY_URL,"http":PROXY_URL}

bot = telebot.TeleBot(BOT_TOKEN, parse_mode=None)

# ══════════════════════════════════════════════════════════
#  HELPERS
# ══════════════════════════════════════════════════════════
MAIN_MENU_TEXT = (
    "⚡ *MUB DEX Bot*\n"
    "_Low-fee Solana Trading — Gas as low as $0.009_\n\n"
    "Choose an option:"
)

def show_main(uid, chat_id, msg_id=None):
    if msg_id:
        try:
            bot.edit_message_text(MAIN_MENU_TEXT, chat_id, msg_id,
                parse_mode="Markdown", reply_markup=kb_main())
        except Exception:
            bot.send_message(chat_id, MAIN_MENU_TEXT,
                parse_mode="Markdown", reply_markup=kb_main())
    else:
        bot.send_message(chat_id, MAIN_MENU_TEXT,
            parse_mode="Markdown", reply_markup=kb_main())

def edit_or_send(call, text, kb=None):
    """Edit message with retry on timeout."""
    for attempt in range(3):
        try:
            bot.edit_message_text(text, call.message.chat.id,
                call.message.message_id,
                parse_mode="Markdown", reply_markup=kb)
            return
        except Exception as e:
            err = str(e)
            if "ConnectTimeout" in err or "ReadTimeout" in err or "timed out" in err.lower():
                time.sleep(2)
                if attempt == 2:
                    # Last resort — send new message
                    try:
                        bot.send_message(call.message.chat.id, text,
                            parse_mode="Markdown", reply_markup=kb)
                    except Exception:
                        pass
                continue
            # Non-timeout error — try send instead
            try:
                bot.send_message(call.message.chat.id, text,
                    parse_mode="Markdown", reply_markup=kb)
            except Exception:
                pass
            return

def is_mint(text):
    t = text.strip()
    return 32 <= len(t) <= 44 and all(c in
        "123456789ABCDEFGHJKLMNPQRSTUVWXYZabcdefghijkmnopqrstuvwxyz" for c in t)

# ══════════════════════════════════════════════════════════
#  /start
# ══════════════════════════════════════════════════════════
@bot.message_handler(commands=["start"])
def cmd_start(msg):
    uid = msg.from_user.id
    u(uid)  # init
    show_main(uid, msg.chat.id)

# ══════════════════════════════════════════════════════════
#  TEXT HANDLER — catches contract paste + multi-step inputs
# ══════════════════════════════════════════════════════════
@bot.message_handler(func=lambda m: True, content_types=["text"])
def handle_text(msg):
    uid  = msg.from_user.id
    text = msg.text.strip()
    usr  = u(uid)
    state = usr["state"]

    # ── Awaiting view-only address ───────────────────────
    if state == "awaiting_view_address":
        # Just a public address — completely safe to store
        addr = text.strip()
        # Validate: Solana address is 32-44 base58 chars
        valid = 32 <= len(addr) <= 44 and all(
            c in "123456789ABCDEFGHJKLMNPQRSTUVWXYZabcdefghijkmnopqrstuvwxyz"
            for c in addr)
        if valid:
            usr["view_pub"] = addr
            usr["state"]    = "idle"
            save_users()
            def _check_bal():
                sol = sol_bal(addr)
                toks = token_accs(addr)
                tok_count = len(toks)
                bot.send_message(msg.chat.id,
                    "✅ *View Wallet Connected!*\n\n"
                    f"Address: `{addr}`\n"
                    f"Balance: `{sol:.4f} SOL`\n"
                    f"Tokens: `{tok_count}`\n\n"
                    "_You can check balance and token info.\n"
                    "To buy/sell, add a Trade Wallet (⚡)._",
                )
            threading.Thread(target=_check_bal, daemon=True).start()
        else:
            bot.send_message(msg.chat.id,
                "❌ Invalid address. Solana address is ~44 chars.\n\n"
                "Example: `HBZHcJw8LQfZ6QFptkevMbkpgPWYg2LnaMkwb2zcPhYi`\n\n"
                "Try again:",
                parse_mode="Markdown"
            )
        return

    # ── Awaiting private key ──────────────────────────────
    if state == "awaiting_pk":
        try:
            kp  = Keypair.from_bytes(base58.b58decode(text))
            pub = str(kp.pubkey())
            usr.update({"keypair":kp,"pubkey":pub,"pk_b58":text,"state":"idle"})
            save_users()
            # Delete the message containing the key for security
            try: bot.delete_message(msg.chat.id, msg.message_id)
            except: pass
            sol = sol_bal(pub)
            bot.send_message(msg.chat.id,
                f"✅ *Wallet Connected!*\n\n"
                f"Address: `{pub}`\n"
                f"Balance: `{sol:.4f} SOL`\n\n"
                f"_Your key is encrypted. For security, the message with your key was deleted._",
                parse_mode="Markdown", reply_markup=kb_main()
            )
        except Exception as e:
            bot.send_message(msg.chat.id,
                f"❌ Invalid key: `{e}`\n\nTry again or press Back.",
                parse_mode="Markdown", reply_markup=kb_back_main()
            )
            usr["state"] = "idle"
        return

    # ── Awaiting custom buy amount ────────────────────────
    if state == "awaiting_buy_amt":
        try:
            amt  = float(text)
            mint = usr["ctx"]["mint"]
            usr["state"] = "idle"
            _execute_buy(uid, msg.chat.id, mint, amt)
        except ValueError:
            bot.send_message(msg.chat.id, "❌ Enter a valid number. e.g. `0.05`",
                parse_mode="Markdown")
        return

    # ── Awaiting sniper contract ──────────────────────────
    if state == "awaiting_sniper_contract":
        if is_mint(text):
            usr["ctx"]["mint"] = text
            usr["state"] = "awaiting_sniper_amount"
            bot.send_message(msg.chat.id,
                f"✅ Target: `{text[:20]}…`\n\n"
                "How much SOL to spend when sniped?\n"
                "e.g. `0.1`",
                parse_mode="Markdown"
            )
        else:
            bot.send_message(msg.chat.id, "❌ Invalid contract address. Try again.")
        return

    if state == "awaiting_sniper_amount":
        try:
            amt  = float(text)
            mint = usr["ctx"]["mint"]
            usr["state"] = "idle"
            if uid not in _snipers or not _snipers[uid].running:
                _snipers[uid] = Sniper(uid, bot)
            _snipers[uid].start(mint, amt)
        except ValueError:
            bot.send_message(msg.chat.id, "❌ Enter a valid SOL amount.")
        return

    # ── Awaiting limit order price ────────────────────────
    if state == "awaiting_limit_price":
        try:
            price = float(text)
            usr["ctx"]["target_price"] = price
            usr["state"] = "awaiting_limit_amount"
            direction = usr["ctx"].get("direction","below")
            bot.send_message(msg.chat.id,
                f"✅ Target price: `${price:.8f}`\n\n"
                "How much SOL to buy when triggered?\ne.g. `0.05`",
                parse_mode="Markdown"
            )
        except ValueError:
            bot.send_message(msg.chat.id, "❌ Enter a valid price. e.g. `0.0001234`")
        return

    if state == "awaiting_limit_amount":
        try:
            amt   = float(text)
            ctx   = usr["ctx"]
            mint  = ctx["mint"]
            price = ctx["target_price"]
            direction = ctx.get("direction","below")
            usr["state"] = "idle"
            order = LimitOrder(uid, mint, direction, price, amt, bot)
            if uid not in _limits: _limits[uid] = []
            _limits[uid].append(order)
            bot.send_message(msg.chat.id,
                f"📋 *Limit Order Set!*\n\n"
                f"Buy `{amt} SOL` of `{mint[:16]}…`\n"
                f"When price goes {'above' if direction=='above' else 'below'} `${price:.8f}`\n\n"
                "_Checking every 15 seconds…_",
                parse_mode="Markdown", reply_markup=kb_main()
            )
        except ValueError:
            bot.send_message(msg.chat.id, "❌ Enter a valid SOL amount.")
        return

    # ── Awaiting auto-trader contract ─────────────────────
    if state == "awaiting_auto_contract":
        if is_mint(text):
            usr["ctx"]["mint"] = text
            usr["state"] = "awaiting_auto_amount"
            s = usr["settings"]
            bot.send_message(msg.chat.id,
                f"✅ Token: `{text[:20]}…`\n\n"
                f"How much SOL to invest?\n"
                f"_(Default: {s['default_buy']} SOL — just send amount)_",
                parse_mode="Markdown"
            )
        else:
            bot.send_message(msg.chat.id, "❌ Invalid contract address. Try again.")
        return

    if state == "awaiting_auto_amount":
        try:
            amt = float(text)
            usr["ctx"]["sol"] = amt
            usr["state"] = "idle"
            s   = usr["settings"]
            cfg = {
                "mint"    : usr["ctx"]["mint"],
                "sol"     : amt,
                "tp"      : s["auto_tp"],
                "sl"      : s["auto_sl"],
                "interval": 30,
            }
            if uid not in _traders or not _traders[uid].running:
                _traders[uid] = AutoTrader(uid, bot)
            _traders[uid].start(cfg)
        except ValueError:
            bot.send_message(msg.chat.id, "❌ Enter a valid amount.")
        return

    # ── Settings inputs ───────────────────────────────────
    if state in ("set_default_buy","set_slippage","set_tp","set_sl"):
        try:
            val = float(text)
            key_map = {
                "set_default_buy": "default_buy",
                "set_slippage"   : "slippage",
                "set_tp"         : "auto_tp",
                "set_sl"         : "auto_sl",
            }
            key = key_map[state]
            usr["settings"][key] = val
            usr["state"] = "idle"
            save_users()
            bot.send_message(msg.chat.id,
                f"✅ Updated! `{key}` = `{val}`",
                parse_mode="Markdown",
                reply_markup=kb_settings(uid)
            )
        except ValueError:
            bot.send_message(msg.chat.id, "❌ Invalid number. Try again.")
        return

    # ── Feedback ──────────────────────────────────────────
    if state == "awaiting_feedback":
        usr["state"] = "idle"
        fb = json.load(open(FEEDBACK_FILE)) if os.path.exists(FEEDBACK_FILE) else []
        fb.append({
            "uid" : uid,
            "name": msg.from_user.first_name,
            "text": text,
            "time": datetime.now().isoformat(),
        })
        json.dump(fb, open(FEEDBACK_FILE,"w"))
        # Forward to admin
        if ADMIN_ID:
            try:
                bot.send_message(ADMIN_ID,
                    f"💬 *New Feedback*\n"
                    f"From: {msg.from_user.first_name} (ID:{uid})\n\n"
                    f"_{text}_", parse_mode="Markdown")
            except: pass
        bot.send_message(msg.chat.id,
            "✅ *Thank you for your feedback!*\n"
            "We'll use it to improve MUB DEX. 🙏",
            parse_mode="Markdown", reply_markup=kb_main()
        )
        return

    # ── Contract address paste (main flow) ───────────────
    if is_mint(text):
        _handle_contract(uid, msg.chat.id, text)
        return

    # ── Fallback ──────────────────────────────────────────
    show_main(uid, msg.chat.id)

def _handle_contract(uid, chat_id, mint):
    """User pasted a contract address — show token info + trade buttons."""
    sent = bot.send_message(chat_id, "⏳ Loading token info…")
    def _r():
        info = token_info(mint)
        if not info["found"]:
            for attempt in range(3):
                try:
                    bot.edit_message_text(
                        f"⚠️ *Token not found on DexScreener*\n"
                        f"`{mint[:20]}…`\n\n"
                        f"May be very new. You can still try to buy.",
                        chat_id, sent.message_id,
                        parse_mode="Markdown",
                        reply_markup=kb_trade(
                            {"mint":mint,"sym":"?","url":"https://dexscreener.com"},
                            uid
                        )
                    )
                    break
                except Exception as e:
                    if "timed out" in str(e).lower() and attempt < 2:
                        time.sleep(2); continue
                    try:
                        bot.send_message(chat_id,
                            f"⚠️ Token `{mint[:16]}…` not found but you can still try to buy.",
                            parse_mode="Markdown",
                            reply_markup=kb_trade(
                                {"mint":mint,"sym":"?","url":"https://dexscreener.com"}, uid))
                    except Exception: pass
                    break
            return
        text = fmt_token_card(info)
        for attempt in range(3):
            try:
                bot.edit_message_text(text, chat_id, sent.message_id,
                    parse_mode="Markdown",
                    reply_markup=kb_trade(info, uid),
                    disable_web_page_preview=True
                )
                break
            except Exception as e:
                if "timed out" in str(e).lower() and attempt < 2:
                    time.sleep(2); continue
                # Fallback: send new message
                try:
                    bot.send_message(chat_id, text,
                        parse_mode="Markdown",
                        reply_markup=kb_trade(info, uid),
                        disable_web_page_preview=True)
                except Exception:
                    pass
                break
    threading.Thread(target=_r, daemon=True).start()

def _execute_buy(uid, chat_id, mint, sol_amount):
    if not has_wallet(uid):
        bot.send_message(chat_id,
            "❌ No wallet connected.\nUse Wallet → Import Private Key first.",
            reply_markup=kb_wallet(uid))
        return
    sent = bot.send_message(chat_id,
        f"⚡ Buying with `{sol_amount} SOL`…",
        parse_mode="Markdown")
    def _r():
        try:
            kp   = _users[uid]["keypair"]
            raw  = int(sol_amount*1e9)
            # Show progress
            for attempt_msg in ["⚡ Getting quote…", "⚡ Signing transaction…"]:
                try:
                    bot.edit_message_text(attempt_msg, chat_id, sent.message_id)
                except Exception: pass
                time.sleep(0.5)
            txid, out, gasless = do_swap(kp, SOL_MINT, mint, raw)
            sym  = token_info(mint).get("sym","?")
            fee_sol = sol_amount * 0.003
            fee_usd = fee_sol * (sol_price_usd() or 0)
            # Verify on-chain
            try:
                bot.edit_message_text("⏳ Confirming on-chain…", chat_id, sent.message_id)
            except Exception: pass
            confirmed = False
            for _ in range(5):
                time.sleep(3)
                try:
                    res = rpc("getSignatureStatuses",
                        [[txid],{"searchTransactionHistory":True}])
                    val = res["result"]["value"][0]
                    if val:
                        if val.get("err"):
                            raise RuntimeError(f"TX failed on-chain: {val['err']}")
                        if val.get("confirmationStatus") in ("confirmed","finalized"):
                            confirmed = True; break
                except RuntimeError: raise
                except Exception: pass
            status = "✅ *Buy Confirmed!*" if confirmed else "✅ *Buy Sent!* _(confirming…)_"
            for attempt in range(3):
                try:
                    bot.edit_message_text(
                        f"{status}"
                        f"{'  ⚡ GASLESS!' if gasless else ''}\n\n"
                        f"💰 Spent: `{sol_amount} SOL`\n"
                        f"📥 Got: `{out:.4f} {sym}`\n\n"
                        f"📊 *Fee Breakdown:*\n"
                        f"  Gas: `{'$0.00 (gasless)' if gasless else '~$0.009'}`\n"
                        f"  Protocol: `{fee_sol:.5f} SOL (~${fee_usd:.3f})`\n\n"
                        f"[🔗 View TX](https://solscan.io/tx/{txid})",
                        chat_id, sent.message_id,
                        parse_mode="Markdown",
                        reply_markup=kb_back_main()
                    )
                    break
                except Exception:
                    if attempt < 2: time.sleep(2)
        except Exception as e:
            for attempt in range(3):
                try:
                    bot.edit_message_text(
                        f"❌ *Buy Failed*\n\n`{str(e)[:200]}`",
                        chat_id, sent.message_id,
                        parse_mode="Markdown", reply_markup=kb_back_main())
                    break
                except Exception:
                    if attempt < 2: time.sleep(2)
    threading.Thread(target=_r, daemon=True).start()

def _execute_sell(uid, chat_id, mint, pct):
    if not has_wallet(uid):
        bot.send_message(chat_id, "❌ No wallet connected.", reply_markup=kb_wallet(uid))
        return
    sent = bot.send_message(chat_id, f"⚡ Selling {pct}% of tokens…")
    def _r():
        try:
            kp   = _users[uid]["keypair"]
            pub  = _users[uid]["pubkey"]
            toks = token_accs(pub)
            t    = next((x for x in toks if x["mint"]==mint),None)
            if not t or int(t["raw"])==0:
                bot.edit_message_text("⚠️ Balance is 0 — nothing to sell.",
                    chat_id, sent.message_id); return
            raw = int(int(t["raw"]) * pct/100)
            txid, out, gasless = do_swap(kp, mint, SOL_MINT, raw)
            bot.edit_message_text(
                f"✅ *Sold {pct}%!*\n\n"
                f"📥 Got: `{out:.5f} SOL`\n"
                f"[🔗 View TX](https://solscan.io/tx/{txid})",
                chat_id, sent.message_id,
                parse_mode="Markdown", reply_markup=kb_back_main()
            )
        except Exception as e:
            bot.edit_message_text(f"❌ Sell failed: `{str(e)[:200]}`",
                chat_id, sent.message_id,
                parse_mode="Markdown", reply_markup=kb_back_main())
    threading.Thread(target=_r, daemon=True).start()

# ══════════════════════════════════════════════════════════
#  CALLBACK HANDLER — all button clicks
# ══════════════════════════════════════════════════════════
@bot.callback_query_handler(func=lambda c: True)
def handle_cb(call):
    uid  = call.from_user.id
    data = call.data
    usr  = u(uid)
    bot.answer_callback_query(call.id)  # dismiss loading spinner

    # ── Main menu ─────────────────────────────────────────
    if data == "menu_main":
        usr["state"] = "idle"
        show_main(uid, call.message.chat.id, call.message.message_id)

    # ── Wallet menu ───────────────────────────────────────
    elif data == "menu_wallet":
        usr_data  = _users.get(uid, {})
        view_pub  = usr_data.get("view_pub")
        trade_pub = usr_data.get("pubkey")
        if has_wallet(uid):
            status = f"⚡ Trade wallet: `{trade_pub[:10]}…{trade_pub[-4:]}`"
        elif view_pub:
            status = f"👁 View only: `{view_pub[:10]}…{view_pub[-4:]}`"
        else:
            status = "❌ No wallet connected"
        edit_or_send(call,
            f"💼 *Wallet*\n\n{status}\n\n"
            "Choose connection type:\n\n"
            "👁 *View Only* — Share your address (safe, no private key)\n"
            "  Check balance & tokens\n\n"
            "⚡ *Trade Wallet* — Small dedicated wallet\n"
            "  For buying/selling tokens",
            kb=kb_wallet(uid)
        )

    elif data in ("w_view_only", "w_connect"):
        usr["state"] = "awaiting_view_address"
        edit_or_send(call,
            "👁 *Connect View Wallet*\n\n"
            "Share your wallet address (public key).\n"
            "This is completely SAFE — no private key needed.\n\n"
            "📱 How to get it:\n"
            "• Phantom: tap address at top → Copy\n"
            "• Solflare: tap wallet name → Copy address\n\n"
            "_Send your address now (~44 chars):_",
            kb=kb_back_main()
        )

    elif data == "w_add_trade":
        usr["state"] = "awaiting_pk"
        edit_or_send(call,
            "⚡ *Add Trade Wallet*\n\n"
            "⚠️ Use a NEW dedicated wallet — never your main!\n\n"
            "Steps:\n"
            "1. Phantom → Add Wallet → Create New\n"
            "2. Settings → Security → Export Private Key\n"
            "3. Send only your TRADING BUDGET to it\n\n"
            "_Send private key (auto-deleted after import):_",
            kb=kb_back_main()
        )

    elif data == "w_trade_info":
        pub = usr.get("pubkey", "")
        sol = sol_bal(pub) if pub else 0
        edit_or_send(call,
            f"⚡ *Trade Wallet*\n\n"
            f"Address: `{pub}`\n"
            f"Balance: `{sol:.5f} SOL`\n\n"
            "_Only keep your trading budget here._",
            kb=kb_wallet(uid)
        )

    elif data in ("w_import",):
        usr["state"] = "awaiting_pk"
        edit_or_send(call,
            "⚡ *Trade Wallet — Import Key*\n\n"
            "_Send your trading wallet private key:_",
            kb=kb_back_main()
        )

    elif data == "w_balance":
        def _bal():
            pub  = active_pub(uid)
            if not pub: bot.send_message(call.message.chat.id,'❌ No wallet connected.',reply_markup=kb_wallet(uid)); return
            sol  = sol_bal(pub)
            toks = token_accs(pub)
            sol_usd = sol * (sol_price_usd() or 0)
            lines = [
                f"💼 *Wallet Balance*\n",
                f"◎ SOL: `{sol:.5f}` _(${sol_usd:.2f})_\n"
            ]
            if toks:
                lines.append("*Tokens:*")
                for t in toks:
                    sym = next((k for k,v in KNOWN.items() if v==t["mint"]),"")
                    name = sym or (t["mint"][:8]+"…"+t["mint"][-4:])
                    lines.append(f"• {name}: `{t['amount']}`")
            else:
                lines.append("_No tokens_")
            bot.send_message(call.message.chat.id,
                "\n".join(lines),
                parse_mode="Markdown", reply_markup=kb_wallet(uid))
        threading.Thread(target=_bal, daemon=True).start()

    elif data == "w_address":
        pub = usr.get("pubkey","Not connected")
        edit_or_send(call,
            f"📋 *Your Address*\n\n`{pub}`\n\n_Tap to copy_",
            kb=kb_wallet(uid)
        )

    # ── Trade menu ────────────────────────────────────────
    elif data == "menu_trade":
        edit_or_send(call,
            "💱 *Buy / Sell*\n\n"
            "Paste a token contract address\n"
            "to see price, safety info, and trade buttons.\n\n"
            "_Just send the contract address in chat!_",
            kb=kb_back_main()
        )

    # ── Buy callbacks ─────────────────────────────────────
    elif data.startswith("buy_"):
        parts = data.split("_", 2)
        if parts[1] == "custom":
            mint = parts[2]
            usr["state"] = "awaiting_buy_amt"
            usr["ctx"]["mint"] = mint
            bot.send_message(call.message.chat.id,
                "Enter SOL amount to buy:\ne.g. `0.05`",
                parse_mode="Markdown"
            )
        else:
            amt  = float(parts[1])
            mint = parts[2]
            _execute_buy(uid, call.message.chat.id, mint, amt)

    # ── Sell callbacks ────────────────────────────────────
    elif data.startswith("sell_"):
        parts = data.split("_", 2)
        pct   = float(parts[1])
        mint  = parts[2]
        _execute_sell(uid, call.message.chat.id, mint, pct)

    # ── Safety ───────────────────────────────────────────
    elif data.startswith("safety_"):
        mint = data.replace("safety_","")
        def _safe():
            info = token_info(mint)
            text = fmt_token_card(info)
            bot.send_message(call.message.chat.id, text,
                parse_mode="Markdown", disable_web_page_preview=True)
        threading.Thread(target=_safe, daemon=True).start()

    # ── Auto-Trader ───────────────────────────────────────
    elif data == "menu_auto":
        running = uid in _traders and _traders[uid].running
        edit_or_send(call,
            "🤖 *Auto-Trader*\n\n"
            + ("✅ *Running!*\nBot is monitoring and trading." if running
               else "Set a token and the bot trades for you automatically.\n\n"
               "• Buys the token\n• Sells at Take-Profit or Stop-Loss\n"
               "• Notifies you on every action"),
            kb=kb_auto(uid)
        )

    elif data == "auto_start":
        usr["state"] = "awaiting_auto_contract"
        edit_or_send(call,
            "🤖 *Auto-Trader Setup*\n\n"
            "Paste the token contract address you want to trade:",
            kb=kb_back_main()
        )

    elif data == "auto_status":
        if uid in _traders and _traders[uid].running:
            t = _traders[uid]
            chg_str = ""
            if t.entry and t._bought:
                p = t._price()
                if p:
                    chg = ((p-t.entry)/t.entry)*100
                    chg_str = f"\nP&L: `{chg:+.2f}%`"
            edit_or_send(call,
                f"📊 *Auto-Trader Status*\n\n"
                f"Token: `{t.cfg['mint'][:16]}…`\n"
                f"Invest: `{t.cfg['sol']} SOL`\n"
                f"TP: `+{t.cfg['tp']}%`  SL: `-{t.cfg['sl']}%`\n"
                f"Holding: `{'Yes ✅' if t._bought else 'Waiting to buy ⏳'}`"
                + chg_str,
                kb=kb_auto(uid)
            )

    elif data == "auto_stop_sell":
        if uid in _traders: _traders[uid].stop(sell=True)

    elif data == "auto_stop_only":
        if uid in _traders: _traders[uid].stop(sell=False)

    # ── Sniper ────────────────────────────────────────────
    elif data == "menu_sniper":
        running = uid in _snipers and _snipers[uid].running
        k = types.InlineKeyboardMarkup(row_width=1)
        if running:
            k.add(types.InlineKeyboardButton("🔴 Stop Sniper", callback_data="sniper_stop"))
        else:
            k.add(types.InlineKeyboardButton("🎯 Arm Sniper",  callback_data="sniper_arm"))
        k.add(types.InlineKeyboardButton("🔙 Back", callback_data="menu_main"))
        edit_or_send(call,
            "🎯 *Sniper Bot*\n\n"
            + ("✅ *Armed and watching!*" if running else
               "Watches a token contract 24/7.\n"
               "Buys instantly when liquidity appears.\n\n"
               "_Perfect for new token launches!_"),
            kb=k
        )

    elif data == "sniper_arm":
        usr["state"] = "awaiting_sniper_contract"
        edit_or_send(call,
            "🎯 *Arm Sniper*\n\nPaste the token contract to snipe:",
            kb=kb_back_main()
        )

    elif data == "sniper_stop":
        if uid in _snipers: _snipers[uid].stop()
        show_main(uid, call.message.chat.id, call.message.message_id)

    # ── Limit Orders ──────────────────────────────────────
    elif data == "menu_limits":
        orders = _limits.get(uid, [])
        active = [o for o in orders if o.active]
        k = types.InlineKeyboardMarkup(row_width=1)
        k.add(types.InlineKeyboardButton("➕ New Limit Order", callback_data="limit_new"))
        k.add(types.InlineKeyboardButton("🔙 Back", callback_data="menu_main"))
        edit_or_send(call,
            f"📋 *Limit Orders*\n\n"
            f"Active orders: `{len(active)}`\n\n"
            "_Set a price target — bot buys automatically when reached._",
            kb=k
        )

    elif data == "limit_new" or data.startswith("limit_"):
        mint = data.replace("limit_","") if data != "limit_new" else None
        if mint and len(mint) > 5:
            usr["ctx"]["mint"] = mint
        usr["state"] = "awaiting_limit_price"
        usr["ctx"]["direction"] = "below"
        edit_or_send(call,
            "📋 *New Limit Order*\n\n"
            "Enter target price in USD.\n"
            "Bot will buy when price drops to this level.\n\n"
            "e.g. `0.0001234`",
            kb=kb_back_main()
        )

    # ── Settings ──────────────────────────────────────────
    elif data == "menu_settings":
        edit_or_send(call,
            "⚙️ *Settings*\n\nTap to change each setting:",
            kb=kb_settings(uid)
        )

    elif data == "set_default_buy":
        usr["state"] = "set_default_buy"
        bot.send_message(call.message.chat.id,
            "Enter default buy amount in SOL:\ne.g. `0.1`",
            parse_mode="Markdown")

    elif data == "set_slippage":
        usr["state"] = "set_slippage"
        bot.send_message(call.message.chat.id,
            "Enter slippage % (0.5 to 10):\ne.g. `1`",
            parse_mode="Markdown")

    elif data == "set_tp":
        usr["state"] = "set_tp"
        bot.send_message(call.message.chat.id,
            "Enter Auto Take-Profit %:\ne.g. `20`",
            parse_mode="Markdown")

    elif data == "set_sl":
        usr["state"] = "set_sl"
        bot.send_message(call.message.chat.id,
            "Enter Auto Stop-Loss %:\ne.g. `10`",
            parse_mode="Markdown")

    elif data == "set_engine_ultra":
        usr["settings"]["engine"] = "ultra"
        save_users()
        edit_or_send(call, "⚙️ *Settings*", kb=kb_settings(uid))

    elif data == "set_engine_normal":
        usr["settings"]["engine"] = "normal"
        save_users()
        edit_or_send(call, "⚙️ *Settings*", kb=kb_settings(uid))

    # ── Feedback ──────────────────────────────────────────
    elif data == "menu_feedback":
        usr["state"] = "awaiting_feedback"
        edit_or_send(call,
            "💬 *Send Feedback*\n\n"
            "Tell us what you think!\n"
            "• What do you like?\n"
            "• What should we improve?\n"
            "• Any bugs?\n\n"
            "_Type your message:_",
            kb=kb_back_main()
        )

    # ── Help ──────────────────────────────────────────────
    elif data == "menu_help":
        edit_or_send(call,
            "❓ *How to use MUB DEX Bot*\n\n"
            "1️⃣ *Connect Wallet*\n"
            "   → Wallet → Import Private Key\n\n"
            "2️⃣ *Buy a Token*\n"
            "   → Paste contract address in chat\n"
            "   → See token info + click Buy\n\n"
            "3️⃣ *Sell*\n"
            "   → Paste same contract\n"
            "   → Click Sell 50% or 100%\n\n"
            "4️⃣ *Auto-Trader*\n"
            "   → Auto → Start\n"
            "   → Bot trades for you 24/7\n\n"
            "5️⃣ *Sniper*\n"
            "   → Sniper → Arm\n"
            "   → Buys instantly on launch\n\n"
            "⚡ *Fees: ~$0.009* (vs $0.40 elsewhere)\n"
            "💰 *Save 97% on gas!*",
            kb=kb_back_main()
        )

# ══════════════════════════════════════════════════════════
#  STARTUP
# ══════════════════════════════════════════════════════════
if __name__ == "__main__":
    if not BOT_TOKEN:
        print("❌  Set BOT_TOKEN!")
        print("    Edit line 33 OR set env: BOT_TOKEN=your_token")
        exit(1)

    if not SOLDERS_OK:
        print("⚠️  pip install solders base58  (trading disabled without them)")

    load_users()
    print("⚡  MUB DEX Bot v2.0 starting…")
    print(f"    Referral: {JUPITER_REFERRAL_ACCOUNT[:20]}…")
    print(f"    Users: {len(_users)}")
    print("    Polling…\n")

    bot.infinity_polling(
        timeout=60,
        long_polling_timeout=30,
    )
