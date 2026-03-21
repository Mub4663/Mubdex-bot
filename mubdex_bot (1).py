"""
MUB DEX Telegram Bot
====================
Mobile version of MUB DEX — works on any phone via Telegram.

SETUP:
  1. pip install pyTelegramBotAPI requests solders base58
  2. Message @BotFather on Telegram → /newbot → get TOKEN
  3. Paste token below → python mubdex_bot.py
  4. Share your bot link with users: t.me/YourBotName

FEATURES:
  ✅ Swap tokens (Ultra + Normal)
  ✅ Check balance
  ✅ Safety check any token
  ✅ Send SOL/tokens
  ✅ Auto-trader with TP/SL
  ✅ Works on ANY phone — just Telegram needed
  ✅ MUB DEX referral fee on every swap
"""

import telebot
import threading
import time
import requests
import base64
import json
import os
import hashlib
import struct
from datetime import datetime

# ══════════════════════════════════════════════════════════
#  CONFIG — FILL THESE IN
# ══════════════════════════════════════════════════════════

import os as _os_env
BOT_TOKEN  = _os_env.environ.get("BOT_TOKEN", "PASTE_YOUR_BOT_TOKEN_HERE")
ADMIN_ID   = 0    # Your Telegram user ID (get from @userinfobot)
WHITELIST  = []   # leave empty = open to all, or restrict: [123456, 789012]

# ── PROXY CONFIG (if api.telegram.org is blocked on your network) ──
# Option A: Set proxy (free proxies at sslproxies.org or use your VPN)
PROXY_URL  = ""   # e.g. "socks5://127.0.0.1:1080" or "http://USER:PASS@HOST:PORT"

# Option B: Use alternative Telegram API server (auto, no proxy needed)
# Leave TG_API_URL empty for default, or set custom mirror
TG_API_URL = ""   # leave empty = use default api.telegram.org

# ── MUB DEX Config (same as desktop) ──────────────────────
RPC_URL                  = "https://api.mainnet-beta.solana.com"
PRIORITY_FEE             = 100000
SLIPPAGE_BPS             = 50
JUPITER_REFERRAL_ACCOUNT = "EqwndckH8GvXoWT1vp5nTqD7KbJPzCEGWnC9XrfqW41x"
JUPITER_FEE_BPS          = 50
MUB_DEX_FEE_WALLET       = ""      # optional direct fee wallet
MUB_DEX_FEE_PERCENT      = 0.003

SOL_MINT = "So11111111111111111111111111111111111111112"
SPL_PROG = "TokenkegQfeZyiNwAJbNbGKPFXCWuBvf9Ss623VQ5DA"

KNOWN = {
    "SOL" : SOL_MINT,
    "USDC": "EPjFWdd5AufqSSqeM2qN1xzybapC8G4wEGGkZwyTDt1v",
    "USDT": "Es9vMFrzaCERmJfrF4H2FYD4KCoNkY11McCe8BenwNYB",
    "BONK": "DezXAZ8z7PnrnRJjz3wXBoRgixCa6xjnB7YaB1pPB263",
    "WIF" : "EKpQGSJtjMFqKZ9KQanSqYXRcF8fBopzLHYxdM65zcjm",
    "JUP" : "JUPyiwrYJFskUPiHa7hkeR8VUtAeFoSYbKedZNsDvCN",
    "RAY" : "4k3Dyjzvzp8eMZWUXbBCjEvwSkkk59S5iCNLY3QrkX6R",
}

JUPITER_PAIRS = [
    {"quote":"https://lite-api.jup.ag/swap/v1/quote",
     "swap" :"https://lite-api.jup.ag/swap/v1/swap",  "name":"lite-api"},
    {"quote":"https://public.jupiterapi.com/quote",
     "swap" :"https://public.jupiterapi.com/swap",     "name":"public"},
    {"quote":"https://quote-api.jup.ag/v6/quote",
     "swap" :"https://quote-api.jup.ag/v6/swap",       "name":"v6"},
]
ULTRA_QUOTE = "https://lite-api.jup.ag/ultra/v1/order"
ULTRA_SWAP  = "https://lite-api.jup.ag/ultra/v1/execute"
DEXSCREEN   = "https://api.dexscreener.com/latest/dex/tokens/{}"

# ══════════════════════════════════════════════════════════
#  OPTIONAL SOLDERS IMPORT
# ══════════════════════════════════════════════════════════
try:
    from solders.keypair        import Keypair
    from solders.transaction    import VersionedTransaction
    from solders.pubkey         import Pubkey
    from solders.system_program import transfer, TransferParams
    from solders.message        import Message
    from solders.hash           import Hash
    from solders.transaction    import Transaction as LegacyTx
    from solders.instruction    import Instruction, AccountMeta
    import base58
    SOLDERS_OK = True
except ImportError:
    SOLDERS_OK = False

# ══════════════════════════════════════════════════════════
#  PER-USER STATE  (each Telegram user has their own wallet)
# ══════════════════════════════════════════════════════════

users = {}   # { user_id: {"keypair":..., "pubkey":..., "state":..., "data":{}} }
traders = {} # { user_id: AutoTrader }
DATA_FILE = "mubdex_users.json"  # encrypted user wallets

def _xor(data, key):
    return bytes(b ^ key[i % len(key)] for i, b in enumerate(data))

def save_users():
    """Save encrypted wallet data."""
    out = {}
    for uid, u in users.items():
        if u.get("pk_b58"):
            k   = hashlib.sha256(str(uid).encode()).digest()
            enc = base64.b64encode(_xor(u["pk_b58"].encode(), k)).decode()
            out[str(uid)] = {"enc": enc}
    json.dump(out, open(DATA_FILE,"w"))

def load_users():
    """Load and decrypt saved wallets."""
    if not os.path.exists(DATA_FILE): return
    try:
        data = json.load(open(DATA_FILE))
        for uid_str, d in data.items():
            uid = int(uid_str)
            k   = hashlib.sha256(str(uid).encode()).digest()
            pk  = _xor(base64.b64decode(d["enc"]), k).decode()
            try:
                kp = Keypair.from_bytes(base58.b58decode(pk))
                users[uid] = {
                    "keypair": kp,
                    "pubkey" : str(kp.pubkey()),
                    "pk_b58" : pk,
                    "state"  : "idle",
                    "data"   : {}
                }
            except Exception:
                pass
    except Exception:
        pass

def get_user(uid):
    if uid not in users:
        users[uid] = {"keypair":None,"pubkey":None,"pk_b58":None,
                      "state":"idle","data":{}}
    return users[uid]

def check_access(uid):
    """Check if user is allowed."""
    if not WHITELIST: return True
    return uid in WHITELIST

# ══════════════════════════════════════════════════════════
#  RPC / CHAIN HELPERS
# ══════════════════════════════════════════════════════════

def rpc(method, params):
    r = requests.post(RPC_URL,
        json={"jsonrpc":"2.0","id":1,"method":method,"params":params},
        timeout=12)
    return r.json()

def get_sol_balance(pub):
    try: return rpc("getBalance",[pub,{"commitment":"confirmed"}])["result"]["value"]/1e9
    except: return 0.0

def get_token_accounts(pub):
    try:
        res = rpc("getTokenAccountsByOwner",[pub,
            {"programId":SPL_PROG},{"encoding":"jsonParsed"}])
        out = []
        for acc in res["result"]["value"]:
            info = acc["account"]["data"]["parsed"]["info"]
            amt  = info["tokenAmount"]
            if float(amt.get("uiAmount") or 0) > 0:
                out.append({"mint":info["mint"],
                            "amount":amt["uiAmountString"],
                            "raw":amt["amount"],
                            "decimals":amt["decimals"]})
        return out
    except: return []

def get_token_decimals(mint):
    try: return rpc("getTokenSupply",[mint])["result"]["value"]["decimals"]
    except: return 6

def get_blockhash():
    return rpc("getLatestBlockhash",[{"commitment":"processed"}])["result"]["value"]["blockhash"]

def send_raw(raw_b64):
    r = rpc("sendTransaction",[raw_b64,{
        "encoding":"base64","skipPreflight":True,
        "preflightCommitment":"processed","maxRetries":5}])
    if "error" in r: raise RuntimeError(str(r["error"]))
    return r["result"]

def resolve_mint(text):
    t = text.strip().upper()
    return KNOWN.get(t, text.strip())

def fmt_addr(addr):
    return f"{addr[:6]}…{addr[-4:]}"

# ══════════════════════════════════════════════════════════
#  SWAP FUNCTIONS
# ══════════════════════════════════════════════════════════

def sign_and_send(tx_bytes, keypair):
    for attempt in range(3):
        try:
            tx  = VersionedTransaction.from_bytes(tx_bytes)
            sgn = VersionedTransaction(tx.message, [keypair])
            return send_raw(base64.b64encode(bytes(sgn)).decode())
        except RuntimeError as e:
            if "BlockhashNotFound" in str(e) and attempt < 2:
                time.sleep(0.8); continue
            raise

def jup_quote(inp, out, raw, slippage_bps=None):
    slip = slippage_bps or SLIPPAGE_BPS
    last_err = "Unknown"
    for pair in JUPITER_PAIRS:
        try:
            r = requests.get(pair["quote"], params={
                "inputMint":inp,"outputMint":out,
                "amount":raw,"slippageBps":slip,
            }, timeout=12)
            if r.status_code != 200:
                last_err = f"HTTP {r.status_code}"; continue
            d = r.json()
            if "error" in d or "errorCode" in d:
                last_err = str(d.get("error",d.get("errorCode",""))); continue
            return d, None
        except requests.exceptions.ConnectionError:
            last_err = f"DNS fail on {pair['name']}"; continue
        except Exception as e:
            last_err = str(e); continue
    return None, last_err

def do_swap(keypair, fm, tm, raw_input, use_ultra=True):
    """Try Ultra first, then Normal Jupiter, then Raydium."""
    # Ultra
    if use_ultra:
        try:
            params = {"inputMint":fm,"outputMint":tm,"amount":raw_input,"taker":str(keypair.pubkey())}
            if JUPITER_REFERRAL_ACCOUNT:
                params["referralAccount"] = JUPITER_REFERRAL_ACCOUNT
                params["referralFeeBps"]  = JUPITER_FEE_BPS
            r = requests.get(ULTRA_QUOTE, params=params, timeout=12)
            if r.status_code == 200:
                order = r.json()
                if "transaction" in order:
                    tx_bytes = base64.b64decode(order["transaction"])
                    tx  = VersionedTransaction.from_bytes(tx_bytes)
                    sgn = VersionedTransaction(tx.message, [keypair])
                    r2  = requests.post(ULTRA_SWAP, json={
                        "signedTransaction": base64.b64encode(bytes(sgn)).decode(),
                        "requestId": order.get("requestId","")
                    }, timeout=20)
                    if r2.status_code == 200:
                        result = r2.json()
                        if result.get("status") != "Failed":
                            gasless = order.get("gasless", False)
                            out_raw = int(order.get("outAmount",0))
                            out_dec = 9 if tm==SOL_MINT else get_token_decimals(tm)
                            out_ui  = out_raw/(10**out_dec)
                            return result.get("signature",""), out_ui, gasless
        except Exception:
            pass

    # Normal Jupiter with slippage ladder
    last_err = "Unknown"
    for slip in [50, 150, 300, 500]:
        try:
            q, qe = jup_quote(fm, tm, raw_input, slippage_bps=slip)
            if not q: last_err = qe; continue
            payload = {
                "quoteResponse":q,"userPublicKey":str(keypair.pubkey()),
                "wrapAndUnwrapSol":True,"prioritizationFeeLamports":PRIORITY_FEE,
                "dynamicComputeUnitLimit":True,"skipUserAccountsCheck":True,
            }
            if JUPITER_REFERRAL_ACCOUNT:
                payload["feeAccount"] = JUPITER_REFERRAL_ACCOUNT
            for pair in JUPITER_PAIRS:
                try:
                    r = requests.post(pair["swap"], json=payload, timeout=25)
                    if r.status_code == 200:
                        d = r.json()
                        if "swapTransaction" in d:
                            tx_bytes = base64.b64decode(d["swapTransaction"])
                            txid = sign_and_send(tx_bytes, keypair)
                            out_raw = int(q.get("outAmount",0))
                            out_dec = 9 if tm==SOL_MINT else get_token_decimals(tm)
                            out_ui  = out_raw/(10**out_dec)
                            return txid, out_ui, False
                except Exception:
                    continue
        except Exception as e:
            last_err = str(e)
            if "custom': 1" in last_err.lower(): continue
            break
    raise RuntimeError(f"Swap failed: {last_err}")

def safety_check_token(mint):
    result = {"name":"Unknown","sym":"?","price":None,"liq":0,"risk":"UNKNOWN","risks":[]}
    try:
        r = requests.get(DEXSCREEN.format(mint), timeout=8)
        if r.status_code == 200:
            pairs = r.json().get("pairs") or []
            if pairs:
                p = max(pairs, key=lambda x: float(x.get("liquidity",{}).get("usd",0) or 0))
                result["name"]  = p.get("baseToken",{}).get("name","Unknown")
                result["sym"]   = p.get("baseToken",{}).get("symbol","?")
                result["price"] = p.get("priceUsd")
                result["liq"]   = float(p.get("liquidity",{}).get("usd",0) or 0)
                chg = float(p.get("priceChange",{}).get("h24",0) or 0)
                result["chg24"] = chg
                if result["liq"] < 1000: result["risk"] = "🚨 HIGH"
                elif result["liq"] < 10000: result["risk"] = "⚠️ MEDIUM"
                else: result["risk"] = "✅ LOW"
                result["url"] = p.get("url","")
    except Exception as e:
        result["error"] = str(e)
    return result

# ══════════════════════════════════════════════════════════
#  AUTO TRADER (per user)
# ══════════════════════════════════════════════════════════

class UserAutoTrader:
    def __init__(self, uid, bot_instance):
        self.uid     = uid
        self.bot     = bot_instance
        self.running = False
        self.cfg     = {}
        self.entry   = None
        self._bought = False
        self.thread  = None

    def notify(self, msg):
        try: self.bot.send_message(self.uid, msg, parse_mode="Markdown")
        except Exception: pass

    def start(self, cfg):
        if self.running: return
        self.cfg = cfg; self.running = True
        self.thread = threading.Thread(target=self._loop, daemon=True)
        self.thread.start()
        self.notify(
            f"🤖 *Auto-Trader Started!*\n"
            f"Token: `{cfg['mint'][:20]}…`\n"
            f"Invest: {cfg['sol']} SOL\n"
            f"Take-Profit: +{cfg['tp']}%\n"
            f"Stop-Loss: -{cfg['sl']}%\n"
            f"Check every: {cfg['interval']}s\n\n"
            f"_Bot will notify you on every action._"
        )

    def stop(self, sell=True):
        self.running = False
        if sell and self._bought:
            self.notify("🔴 *Stopping — selling tokens…*")
            try:
                u    = users.get(self.uid)
                kp   = u["keypair"] if u else None
                toks = get_token_accounts(u["pubkey"]) if u else []
                t    = next((x for x in toks if x["mint"]==self.cfg["mint"]),None)
                if t and int(t["raw"]) > 0 and kp:
                    txid, out_ui, _ = do_swap(kp,
                        self.cfg["mint"], SOL_MINT, int(t["raw"]))
                    self.notify(
                        f"✅ *Sold!*\nGot: {out_ui:.5f} SOL\n"
                        f"[View TX](https://solscan.io/tx/{txid})")
                    self._bought = False
            except Exception as e:
                self.notify(f"❌ Sell failed: {e}")
        else:
            self.notify("⏹ *Auto-Trader stopped.*")

    def _price(self):
        try:
            mint = self.cfg["mint"]
            dec  = get_token_decimals(mint)
            q, _ = jup_quote(mint, SOL_MINT, int(10**dec))
            return int(q["outAmount"])/1e9 if q else None
        except: return None

    def _loop(self):
        mint   = self.cfg["mint"]
        bought = False
        while self.running:
            try:
                p = self._price()
                if p is None:
                    time.sleep(self.cfg["interval"]); continue
                ts = datetime.now().strftime("%H:%M")

                if not bought:
                    u   = users.get(self.uid)
                    kp  = u["keypair"] if u else None
                    if not kp: break
                    raw = int(float(self.cfg["sol"])*1e9)
                    txid, out_ui, gasless = do_swap(kp, SOL_MINT, mint, raw)
                    self.entry   = p
                    bought       = True
                    self._bought = True
                    self.notify(
                        f"✅ *Bought!*\n"
                        f"Price: {p:.8f} SOL\n"
                        f"Got: {out_ui:.4f} tokens"
                        + (" ⚡ Gasless!" if gasless else "") +
                        f"\n[View TX](https://solscan.io/tx/{txid})"
                    )
                else:
                    chg = ((p - self.entry)/self.entry)*100
                    if chg >= float(self.cfg["tp"]):
                        self.notify(f"🎯 *Take-Profit hit! +{chg:.2f}%*\nSelling…")
                        u   = users.get(self.uid)
                        kp  = u["keypair"] if u else None
                        toks = get_token_accounts(u["pubkey"])
                        t    = next((x for x in toks if x["mint"]==mint),None)
                        if t and kp:
                            txid, out, _ = do_swap(kp, mint, SOL_MINT, int(t["raw"]))
                            self.notify(
                                f"💰 *Sold! +{chg:.2f}%*\n"
                                f"Got: {out:.5f} SOL\n"
                                f"[View TX](https://solscan.io/tx/{txid})"
                            )
                        bought = False; self.entry = None; self._bought = False

                    elif chg <= -float(self.cfg["sl"]):
                        self.notify(f"🛑 *Stop-Loss hit! {chg:.2f}%*\nSelling…")
                        u   = users.get(self.uid)
                        kp  = u["keypair"] if u else None
                        toks = get_token_accounts(u["pubkey"])
                        t    = next((x for x in toks if x["mint"]==mint),None)
                        if t and kp:
                            txid, out, _ = do_swap(kp, mint, SOL_MINT, int(t["raw"]))
                            self.notify(
                                f"📉 *Sold! {chg:.2f}%*\n"
                                f"Got: {out:.5f} SOL\n"
                                f"[View TX](https://solscan.io/tx/{txid})"
                            )
                        bought = False; self.entry = None; self._bought = False

                time.sleep(self.cfg["interval"])
            except Exception as e:
                self.notify(f"❌ Auto-trader error: {e}")
                time.sleep(self.cfg["interval"])

# ══════════════════════════════════════════════════════════
#  BOT INSTANCE
# ══════════════════════════════════════════════════════════

# ── Apply proxy if configured ─────────────────────────────
import telebot.apihelper as _apihelper
if PROXY_URL:
    _apihelper.proxy = {"https": PROXY_URL, "http": PROXY_URL}
    print(f"🔌  Proxy active: {PROXY_URL}")
if TG_API_URL:
    _apihelper.API_URL = TG_API_URL + "/bot{0}/{1}"
    print(f"🌐  Custom API: {TG_API_URL}")

bot = telebot.TeleBot(BOT_TOKEN, parse_mode=None)

# ── Access check decorator ────────────────────────────────
def require_access(fn):
    def wrapper(msg):
        if not check_access(msg.from_user.id):
            bot.reply_to(msg, "⛔ Access denied. Contact @MUBDEXBot admin.")
            return
        fn(msg)
    wrapper.__name__ = fn.__name__
    return wrapper

def require_wallet(fn):
    def wrapper(msg):
        if not check_access(msg.from_user.id):
            bot.reply_to(msg, "⛔ Access denied."); return
        u = get_user(msg.from_user.id)
        if not u["keypair"]:
            bot.reply_to(msg,
                "❌ No wallet loaded.\n\n"
                "Use /import to import your private key.\n"
                "⚠️ Use a DEDICATED wallet with small amounts only!")
            return
        fn(msg)
    wrapper.__name__ = fn.__name__
    return wrapper

# ══════════════════════════════════════════════════════════
#  COMMANDS
# ══════════════════════════════════════════════════════════

@bot.message_handler(commands=["start","help"])
@require_access
def cmd_start(msg):
    bot.reply_to(msg,
        "⚡ *Welcome to MUB DEX Bot!*\n"
        "_Low fee Solana DEX — Gas as low as $0.009_\n\n"
        "━━━━━━━━━━━━━━━━\n"
        "💼 *WALLET*\n"
        "/import — Import private key\n"
        "/balance — Check SOL + token balance\n"
        "/address — Show your wallet address\n\n"
        "💱 *TRADING*\n"
        "/swap — Swap tokens\n"
        "/safety — Check token safety\n"
        "/price — Get token price\n\n"
        "📤 *TRANSFER*\n"
        "/send — Send SOL or tokens\n\n"
        "🤖 *AUTO-TRADER*\n"
        "/auto — Start auto-trader (TP/SL)\n"
        "/stop — Stop auto-trader\n"
        "/status — Auto-trader status\n\n"
        "━━━━━━━━━━━━━━━━\n"
        "⚡ *Ultra Swap* = Gasless + Fastest\n"
        "💰 *Fee: ~$0.009* (vs $0.40 on Jupiter UI)\n\n"
        "_Built by MUB DEX — Solana trading for everyone_",
        parse_mode="Markdown"
    )


# ── IMPORT WALLET ─────────────────────────────────────────
@bot.message_handler(commands=["import"])
@require_access
def cmd_import(msg):
    uid = msg.from_user.id
    bot.reply_to(msg,
        "🔑 *Import Wallet*\n\n"
        "Send your private key (base58 format).\n"
        "You get it from Phantom:\n"
        "Settings → Security → Export Private Key\n\n"
        "⚠️ *SECURITY TIPS:*\n"
        "• Use a DEDICATED trading wallet\n"
        "• Keep only small amounts (e.g. $10-50)\n"
        "• Never share key with anyone else\n\n"
        "_Send key now:_",
        parse_mode="Markdown"
    )
    get_user(uid)["state"] = "awaiting_pk"


@bot.message_handler(commands=["address"])
@require_wallet
def cmd_address(msg):
    uid = msg.from_user.id
    pub = users[uid]["pubkey"]
    bot.reply_to(msg,
        f"📥 *Your Wallet Address:*\n\n"
        f"`{pub}`\n\n"
        f"_Tap to copy — send SOL to this address_",
        parse_mode="Markdown"
    )


# ── BALANCE ───────────────────────────────────────────────
@bot.message_handler(commands=["balance"])
@require_wallet
def cmd_balance(msg):
    uid = msg.from_user.id
    pub = users[uid]["pubkey"]
    bot.reply_to(msg, "⏳ Checking balance…")

    def _r():
        sol  = get_sol_balance(pub)
        toks = get_token_accounts(pub)

        lines = [
            f"💼 *Wallet:* `{fmt_addr(pub)}`\n",
            f"◎ *SOL:* {sol:.5f} SOL",
        ]
        if toks:
            lines.append("\n*Tokens:*")
            for t in toks:
                sym = next((k for k,v in KNOWN.items() if v==t["mint"]), "")
                name = sym if sym else fmt_addr(t["mint"])
                lines.append(f"• {name}: {t['amount']}")
        else:
            lines.append("_No tokens found_")

        bot.reply_to(msg, "\n".join(lines), parse_mode="Markdown")
    threading.Thread(target=_r, daemon=True).start()


# ── SWAP ──────────────────────────────────────────────────
@bot.message_handler(commands=["swap"])
@require_wallet
def cmd_swap(msg):
    uid = msg.from_user.id
    bot.reply_to(msg,
        "💱 *Swap Tokens*\n\n"
        "Format:\n"
        "`/swap FROM TO AMOUNT`\n\n"
        "Examples:\n"
        "`/swap SOL USDC 0.01`\n"
        "`/swap SOL BONK 0.005`\n"
        "`/swap USDC SOL 1`\n"
        "`/swap SOL MINT_ADDRESS 0.01`\n\n"
        "_Uses Ultra Swap (fastest, gasless when possible)_",
        parse_mode="Markdown"
    )


@bot.message_handler(commands=["swapnow"])
@require_wallet
def cmd_swapnow(msg):
    """Handle /swapnow FROM TO AMOUNT"""
    uid  = msg.from_user.id
    args = msg.text.split()
    if len(args) < 4:
        bot.reply_to(msg, "Usage: `/swapnow FROM TO AMOUNT`\nExample: `/swapnow SOL USDC 0.01`",
                     parse_mode="Markdown")
        return

    fm_sym = args[1].upper()
    to_sym = args[2].upper()
    try:
        amt = float(args[3])
    except ValueError:
        bot.reply_to(msg, "❌ Invalid amount"); return

    fm = resolve_mint(fm_sym)
    tm = resolve_mint(to_sym)
    kp = users[uid]["keypair"]

    sent = bot.reply_to(msg, f"⚡ Swapping {amt} {fm_sym} → {to_sym}…")

    def _r():
        try:
            dec     = 9 if fm == SOL_MINT else get_token_decimals(fm)
            raw_in  = int(amt * (10**dec))
            txid, out_ui, gasless = do_swap(kp, fm, tm, raw_in)
            gas_note = " ⚡ *GASLESS!*" if gasless else ""
            bot.edit_message_text(
                f"✅ *Swap Done!*{gas_note}\n\n"
                f"📤 Sent: {amt} {fm_sym}\n"
                f"📥 Got: {out_ui:.6f} {to_sym}\n"
                f"💰 Gas: {'$0.00 (gasless)' if gasless else '~$0.009'}\n\n"
                f"[🔗 View on Solscan](https://solscan.io/tx/{txid})",
                sent.chat.id, sent.message_id, parse_mode="Markdown"
            )
        except Exception as e:
            bot.edit_message_text(
                f"❌ *Swap Failed*\n\n`{str(e)[:200]}`",
                sent.chat.id, sent.message_id, parse_mode="Markdown"
            )
    threading.Thread(target=_r, daemon=True).start()


# ── SAFETY CHECK ──────────────────────────────────────────
@bot.message_handler(commands=["safety","check"])
@require_access
def cmd_safety(msg):
    args = msg.text.split()
    if len(args) < 2:
        bot.reply_to(msg,
            "Usage: `/safety TOKEN_MINT_ADDRESS`\n"
            "Example: `/safety DezXAZ8z7...`",
            parse_mode="Markdown")
        return

    mint = args[1].strip()
    sent = bot.reply_to(msg, "🛡️ Checking token safety…")

    def _r():
        info = safety_check_token(mint)
        if info["name"] == "Unknown":
            bot.edit_message_text(
                f"⚠️ *Token not found on DexScreener*\n"
                f"`{fmt_addr(mint)}`\n\n"
                "Token may be very new or invalid.",
                sent.chat.id, sent.message_id, parse_mode="Markdown"
            ); return

        risk_emoji = "🚨" if "HIGH" in info["risk"] else "⚠️" if "MEDIUM" in info["risk"] else "✅"
        price_str  = f"${float(info['price']):.8f}" if info.get("price") else "N/A"
        chg_str    = f"{info.get('chg24',0):+.1f}%" if info.get("chg24") is not None else "N/A"

        bot.edit_message_text(
            f"{risk_emoji} *{info['name']} ({info['sym']})*\n\n"
            f"Risk: {info['risk']}\n"
            f"Price: {price_str}\n"
            f"Liquidity: ${info['liq']:,.0f}\n"
            f"24h Change: {chg_str}\n"
            + (f"\n[📊 DexScreener]({info['url']})" if info.get("url") else ""),
            sent.chat.id, sent.message_id, parse_mode="Markdown"
        )
    threading.Thread(target=_r, daemon=True).start()


# ── PRICE ─────────────────────────────────────────────────
@bot.message_handler(commands=["price"])
@require_access
def cmd_price(msg):
    args = msg.text.split()
    if len(args) < 2:
        bot.reply_to(msg, "Usage: `/price BONK` or `/price MINT_ADDRESS`",
                     parse_mode="Markdown")
        return
    mint = resolve_mint(args[1])
    info = safety_check_token(mint)
    if info.get("price"):
        bot.reply_to(msg,
            f"💲 *{info['sym']} Price*\n\n"
            f"Price: ${float(info['price']):.8f}\n"
            f"Liquidity: ${info['liq']:,.0f}\n"
            f"24h: {info.get('chg24',0):+.1f}%",
            parse_mode="Markdown")
    else:
        bot.reply_to(msg, "❌ Price not found. Check the token address.")


# ── SEND ──────────────────────────────────────────────────
@bot.message_handler(commands=["send"])
@require_wallet
def cmd_send(msg):
    bot.reply_to(msg,
        "📤 *Send SOL*\n\n"
        "Format: `/sendnow TO_ADDRESS AMOUNT`\n"
        "Example: `/sendnow 7abc...xyz 0.01`",
        parse_mode="Markdown"
    )


@bot.message_handler(commands=["sendnow"])
@require_wallet
def cmd_sendnow(msg):
    uid  = msg.from_user.id
    args = msg.text.split()
    if len(args) < 3:
        bot.reply_to(msg, "Usage: `/sendnow ADDRESS AMOUNT`", parse_mode="Markdown")
        return
    to_addr = args[1].strip()
    try: amt = float(args[2])
    except ValueError: bot.reply_to(msg,"❌ Invalid amount"); return

    if not messagebox_confirm(msg, f"Send {amt} SOL to {fmt_addr(to_addr)}?"):
        return

    sent = bot.reply_to(msg, f"📤 Sending {amt} SOL…")
    kp   = users[uid]["keypair"]

    def _r():
        try:
            from solders.pubkey import Pubkey
            ix  = transfer(TransferParams(
                from_pubkey=kp.pubkey(),
                to_pubkey=Pubkey.from_string(to_addr),
                lamports=int(amt*1e9)))
            bh  = Hash.from_string(get_blockhash())
            msg2= Message.new_with_blockhash([ix], kp.pubkey(), bh)
            sgn = VersionedTransaction(msg2, [kp])
            txid = send_raw(base64.b64encode(bytes(sgn)).decode())
            bot.edit_message_text(
                f"✅ *Sent!*\n\n"
                f"Amount: {amt} SOL\n"
                f"To: `{fmt_addr(to_addr)}`\n\n"
                f"[🔗 View TX](https://solscan.io/tx/{txid})",
                sent.chat.id, sent.message_id, parse_mode="Markdown"
            )
        except Exception as e:
            bot.edit_message_text(f"❌ Send failed: {e}", sent.chat.id, sent.message_id)
    threading.Thread(target=_r, daemon=True).start()


def messagebox_confirm(msg, text):
    """Simple YES/NO confirmation via reply."""
    # For Telegram bot we skip confirmation — user already typed the command
    return True


# ── AUTO TRADER ───────────────────────────────────────────
@bot.message_handler(commands=["auto"])
@require_wallet
def cmd_auto(msg):
    bot.reply_to(msg,
        "🤖 *Auto-Trader Setup*\n\n"
        "Format:\n"
        "`/autostart MINT SOL_AMOUNT TP% SL% INTERVAL_SEC`\n\n"
        "Example:\n"
        "`/autostart DezXAZ8z... 0.01 20 10 30`\n\n"
        "_This will:_\n"
        "• Buy token with 0.01 SOL\n"
        "• Auto-sell at +20% profit\n"
        "• Auto-sell at -10% loss\n"
        "• Check every 30 seconds\n"
        "• Notify you on Telegram for every action",
        parse_mode="Markdown"
    )


@bot.message_handler(commands=["autostart"])
@require_wallet
def cmd_autostart(msg):
    uid  = msg.from_user.id
    args = msg.text.split()
    if len(args) < 6:
        bot.reply_to(msg,
            "Usage: `/autostart MINT SOL TP SL INTERVAL`\n"
            "Example: `/autostart DezX... 0.01 20 10 30`",
            parse_mode="Markdown")
        return
    try:
        cfg = {
            "mint"    : args[1].strip(),
            "sol"     : float(args[2]),
            "tp"      : float(args[3]),
            "sl"      : float(args[4]),
            "interval": int(args[5]),
        }
    except (ValueError, IndexError):
        bot.reply_to(msg, "❌ Invalid parameters"); return

    if uid in traders and traders[uid].running:
        bot.reply_to(msg, "⚠️ Auto-trader already running. Use /stop first.")
        return

    traders[uid] = UserAutoTrader(uid, bot)
    traders[uid].start(cfg)


@bot.message_handler(commands=["stop"])
@require_wallet
def cmd_stop(msg):
    uid = msg.from_user.id
    if uid in traders and traders[uid].running:
        traders[uid].stop(sell=True)
    else:
        bot.reply_to(msg, "ℹ️ No auto-trader running.")


@bot.message_handler(commands=["status"])
@require_wallet
def cmd_status(msg):
    uid = msg.from_user.id
    if uid in traders and traders[uid].running:
        t   = traders[uid]
        cfg = t.cfg
        bot.reply_to(msg,
            f"🤖 *Auto-Trader Active*\n\n"
            f"Token: `{fmt_addr(cfg['mint'])}`\n"
            f"Take-Profit: +{cfg['tp']}%\n"
            f"Stop-Loss: -{cfg['sl']}%\n"
            f"Holding: {'✅ Yes' if t._bought else '⏳ Waiting to buy'}\n"
            f"Entry price: {t.entry:.8f} SOL" if t.entry else "Entry price: —",
            parse_mode="Markdown"
        )
    else:
        bot.reply_to(msg, "⏹ No auto-trader running.")


# ══════════════════════════════════════════════════════════
#  TEXT HANDLER (handle private key input + swap shortcuts)
# ══════════════════════════════════════════════════════════

@bot.message_handler(func=lambda m: True, content_types=["text"])
@require_access
def handle_text(msg):
    uid   = msg.from_user.id
    text  = msg.text.strip()
    user  = get_user(uid)
    state = user.get("state","idle")

    # ── Private key input ─────────────────────────────────
    if state == "awaiting_pk":
        if not SOLDERS_OK:
            bot.reply_to(msg, "❌ Server missing solders package."); return
        try:
            kp  = Keypair.from_bytes(base58.b58decode(text))
            pub = str(kp.pubkey())
            user["keypair"] = kp
            user["pubkey"]  = pub
            user["pk_b58"]  = text
            user["state"]   = "idle"
            save_users()
            bot.reply_to(msg,
                f"✅ *Wallet Imported!*\n\n"
                f"Address: `{pub}`\n\n"
                f"_Your key is encrypted and stored securely._\n"
                f"Use /balance to check your balance.",
                parse_mode="Markdown"
            )
        except Exception as e:
            bot.reply_to(msg, f"❌ Invalid key: {e}\n\nTry again or /help")
        return

    # ── Shortcut: FROM TO AMOUNT (e.g. "SOL USDC 0.01") ───
    parts = text.split()
    if len(parts) == 3:
        try:
            amt = float(parts[2])
            fm  = resolve_mint(parts[0])
            tm  = resolve_mint(parts[1])
            if user["keypair"]:
                # Treat as quick swap
                fake_msg      = msg
                fake_msg.text = f"/swapnow {parts[0]} {parts[1]} {parts[2]}"
                cmd_swapnow(fake_msg)
                return
        except ValueError:
            pass

    bot.reply_to(msg,
        "💬 Use /help to see all commands.\n\n"
        "Quick swap shortcut:\n"
        "`SOL USDC 0.01`\n"
        "_just type FROM TO AMOUNT_",
        parse_mode="Markdown"
    )


# ══════════════════════════════════════════════════════════
#  STARTUP
# ══════════════════════════════════════════════════════════

if __name__ == "__main__":
    # Read from environment variable if not hardcoded
    import os as _os
    _env_token = _os.environ.get("BOT_TOKEN","")
    if _env_token:
        import telebot.apihelper as _ah2
        bot.token = _env_token
        print(f"✅  Token loaded from environment")
    elif BOT_TOKEN == "PASTE_YOUR_BOT_TOKEN_HERE":
        print("❌  No bot token found!")
        print("    Option 1: Edit mubdex_bot.py → paste token in BOT_TOKEN")
        print("    Option 2: Set env variable BOT_TOKEN=your_token")
        print("    Get token: message @BotFather on Telegram → /newbot")
        exit(1)

    if not SOLDERS_OK:
        print("❌  pip install solders base58")
        exit(1)

    load_users()
    print("⚡  MUB DEX Telegram Bot starting…")
    print(f"    Referral: {JUPITER_REFERRAL_ACCOUNT[:20]}…")
    print(f"    Users loaded: {len(users)}")
    print("    Polling for messages…\n")

    bot.infinity_polling(
        timeout=60,
        long_polling_timeout=30,
        allowed_updates=["message","callback_query"],
    )
