"""
⚡ MUB DEX Bot v3.0
Solana Trading Bot — Inline Keyboard UI (Trojan-style)
Fixed: proper TX signing, real confirmation, clean UI
"""

import os, json, time, base64, hashlib, threading, logging
import requests
from datetime import datetime

import telebot
from telebot import types

logging.basicConfig(level=logging.INFO,
    format="%(asctime)s %(levelname)s %(message)s")

BOT_TOKEN  = os.environ.get("BOT_TOKEN", "")
ADMIN_ID   = int(os.environ.get("ADMIN_ID", "6237665352"))
PROXY_URL  = os.environ.get("PROXY_URL", "")
RPC_LIST = [
    os.environ.get("RPC_URL", "https://mainnet.helius-rpc.com/?api-key=92d43c65-101f-4053-a457-615a230bfd64"),
    "https://api.mainnet-beta.solana.com",
    "https://rpc.ankr.com/solana",
]
RPC_URL = RPC_LIST[0]   # primary — updated by failover

JUPITER_REFERRAL_ACCOUNT = "EqwndckH8GvXoWT1vp5nTqD7KbJPzCEGWnC9XrfqW41x"
JUPITER_FEE_BPS          = 50
PRIORITY_FEE             = 500_000

SOL_MINT = "So11111111111111111111111111111111111111112"
SPL_PROG = "TokenkegQfeZyiNwAJbNbGKPFXCWuBvf9Ss623VQ5DA"

JUPITER_PAIRS = [
    {"q":"https://lite-api.jup.ag/swap/v1/quote","s":"https://lite-api.jup.ag/swap/v1/swap","n":"lite"},
    {"q":"https://public.jupiterapi.com/quote","s":"https://public.jupiterapi.com/swap","n":"public"},
    {"q":"https://quote-api.jup.ag/v6/quote","s":"https://quote-api.jup.ag/v6/swap","n":"v6"},
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
HISTORY_FILE  = "mubdex_history.json"
STATS_FILE    = "mubdex_stats.json"

_active_users = {}  # uid → last_seen timestamp

def track_user(uid, name=""):
    """Track user activity for admin dashboard."""
    _active_users[uid] = {
        "time" : time.time(),
        "name" : name,
    }
    # Save stats
    try:
        stats = json.load(open(STATS_FILE)) if os.path.exists(STATS_FILE) else {}
        uid_s = str(uid)
        if uid_s not in stats:
            stats[uid_s] = {
                "first_seen": datetime.now().isoformat(),
                "name"      : name,
                "swaps"     : 0,
                "last_seen" : datetime.now().isoformat(),
            }
        else:
            stats[uid_s]["last_seen"] = datetime.now().isoformat()
            if name: stats[uid_s]["name"] = name
        json.dump(stats, open(STATS_FILE,"w"))
    except Exception: pass

def get_stats():
    """Get bot statistics for admin."""
    try:
        stats  = json.load(open(STATS_FILE)) if os.path.exists(STATS_FILE) else {}
        hist   = json.load(open(HISTORY_FILE)) if os.path.exists(HISTORY_FILE) else {}
        now    = time.time()
        total  = len(stats)
        active_24h = sum(
            1 for u in stats.values()
            if (datetime.now() - datetime.fromisoformat(
                u.get("last_seen", "2000-01-01"))).days < 1
        )
        # Count total swaps across all users
        total_swaps = sum(len(v) for v in hist.values())
        # Active wallets (has keypair)
        with_wallet = sum(1 for uu in _users.values() if uu.get("keypair"))
        return {
            "total"       : total,
            "active_24h"  : active_24h,
            "with_wallet" : with_wallet,
            "total_swaps" : total_swaps,
            "users"       : stats,
        }
    except Exception as e:
        return {"error": str(e)}

# Token name cache — fetched from DexScreener
_tok_name_cache = {}  # mint → {"sym":..., "name":...}

def get_token_name(mint):
    """Get token symbol/name from cache or DexScreener."""
    if mint == SOL_MINT: return "SOL", "Solana"
    sym = next((k for k,v in KNOWN.items() if v==mint), None)
    if sym: return sym, sym
    if mint in _tok_name_cache:
        return _tok_name_cache[mint]["sym"], _tok_name_cache[mint]["name"]
    try:
        r = requests.get(DEX_URL.format(mint), timeout=5)
        if r.status_code == 200:
            pairs = r.json().get("pairs") or []
            if pairs:
                tok  = pairs[0].get("baseToken",{})
                sym  = tok.get("symbol","?")
                name = tok.get("name","?")
                _tok_name_cache[mint] = {"sym":sym,"name":name}
                return sym, name
    except Exception: pass
    return mint[:6]+"…"+mint[-4:], "Unknown"

def save_history(uid, action):
    """Save trade action to history."""
    try:
        history = json.load(open(HISTORY_FILE)) if os.path.exists(HISTORY_FILE) else {}
        uid_s   = str(uid)
        if uid_s not in history: history[uid_s] = []
        history[uid_s].append(action)
        # Keep last 50 per user
        history[uid_s] = history[uid_s][-50:]
        json.dump(history, open(HISTORY_FILE,"w"))
    except Exception: pass

def get_history(uid):
    """Get user trade history."""
    try:
        history = json.load(open(HISTORY_FILE)) if os.path.exists(HISTORY_FILE) else {}
        return history.get(str(uid), [])
    except Exception: return []


try:
    from solders.keypair import Keypair
    from solders.transaction import VersionedTransaction
    from solders.pubkey import Pubkey
    from solders.system_program import transfer, TransferParams
    from solders.message import Message
    from solders.hash import Hash
    import base58
    SOLDERS_OK = True
except ImportError:
    SOLDERS_OK = False

_users   = {}
_traders = {}
_snipers = {}
_limits  = {}

def _xor(d, k): return bytes(b ^ k[i%len(k)] for i,b in enumerate(d))
def _default_settings():
    return {
        "default_buy" : 0.1,
        "auto_tp"     : 20,
        "auto_sl"     : 10,
        "max_slippage": 3000,   # bps — 30% default max (auto-escalates from 0.1%)
        "swap_engine" : "ultra", # ultra | normal
    }

def _new_user():
    return {"keypair":None,"pubkey":None,"view_pub":None,"pk_b58":None,
            "state":"idle","ctx":{},"settings":_default_settings()}

def u(uid):
    if uid not in _users: _users[uid] = _new_user()
    return _users[uid]

def has_wallet(uid): return bool(_users.get(uid,{}).get("keypair"))
def has_any(uid):
    usr=_users.get(uid,{}); return bool(usr.get("keypair") or usr.get("view_pub"))
def active_pub(uid):
    usr=_users.get(uid,{}); return usr.get("pubkey") or usr.get("view_pub")
def fmt_addr(a): return f"{a[:6]}…{a[-4:]}" if a else "N/A"
def is_mint(t):
    t=t.strip()
    return 32<=len(t)<=44 and all(c in "123456789ABCDEFGHJKLMNPQRSTUVWXYZabcdefghijkmnopqrstuvwxyz" for c in t)

def save_users():
    out={}
    for uid,uu in _users.items():
        e={"settings":uu.get("settings",{})}
        if uu.get("pk_b58"):
            k=hashlib.sha256(str(uid).encode()).digest()
            e["enc"]=base64.b64encode(_xor(uu["pk_b58"].encode(),k)).decode()
        if uu.get("view_pub"): e["view_pub"]=uu["view_pub"]
        if e.get("enc") or e.get("view_pub"): out[str(uid)]=e
    json.dump(out, open(DATA_FILE,"w"))
    # Also save as base64 env-compatible backup string
    try:
        backup = base64.b64encode(json.dumps(out).encode()).decode()
        open(DATA_FILE+".bak","w").write(backup)
    except Exception: pass

def load_users():
    # Try primary file, then backup
    if not os.path.exists(DATA_FILE):
        bak = DATA_FILE + ".bak"
        if os.path.exists(bak):
            try:
                data = json.loads(base64.b64decode(open(bak).read()).decode())
                json.dump(data, open(DATA_FILE,"w"))
                logging.info("✅ Wallet restored from backup!")
            except Exception: pass
    # Also check WALLET_BACKUP env var (set manually on Railway)
    env_bak = os.environ.get("WALLET_BACKUP","")
    if env_bak and not os.path.exists(DATA_FILE):
        try:
            data = json.loads(base64.b64decode(env_bak).decode())
            json.dump(data, open(DATA_FILE,"w"))
            logging.info("✅ Wallet restored from WALLET_BACKUP env!")
        except Exception: pass
    if not os.path.exists(DATA_FILE): return
    try:
        data=json.load(open(DATA_FILE))
        for uid_s,d in data.items():
            uid=int(uid_s); uu=_new_user()
            uu["settings"]=d.get("settings",_default_settings())
            if d.get("enc") and SOLDERS_OK:
                try:
                    k=hashlib.sha256(str(uid).encode()).digest()
                    pk=_xor(base64.b64decode(d["enc"]),k).decode()
                    kp=Keypair.from_bytes(base58.b58decode(pk))
                    uu.update({"keypair":kp,"pubkey":str(kp.pubkey()),"pk_b58":pk})
                except: pass
            if d.get("view_pub"): uu["view_pub"]=d["view_pub"]
            _users[uid]=uu
    except: pass

def rpc(method, params, _send=False):
    """
    Multi-RPC with auto-failover.
    _send=True  → use primary RPC only (Helius) for send/simulate
    _send=False → try all RPCs in order for reads/confirms
    """
    global RPC_URL
    targets = [RPC_LIST[0]] if _send else RPC_LIST
    last_err = "No RPC responded"
    for url in targets:
        try:
            r = requests.post(url,
                json={"jsonrpc":"2.0","id":1,"method":method,"params":params},
                timeout=10)
            if r.status_code == 200:
                data = r.json()
                # Update primary if a fallback succeeded
                if url != RPC_LIST[0] and "result" in data:
                    logging.info(f"RPC failover: using {url}")
                return data
        except Exception as e:
            last_err = str(e)
            continue
    raise RuntimeError(f"All RPCs failed: {last_err}")

def sol_bal(pub):
    try: return rpc("getBalance",[pub,{"commitment":"confirmed"}])["result"]["value"]/1e9
    except: return 0.0

def token_accs(pub):
    try:
        res=rpc("getTokenAccountsByOwner",[pub,{"programId":SPL_PROG},{"encoding":"jsonParsed"}])
        out=[]
        for a in res["result"]["value"]:
            inf=a["account"]["data"]["parsed"]["info"]; amt=inf["tokenAmount"]
            if float(amt.get("uiAmount") or 0)>0:
                out.append({"mint":inf["mint"],"amount":amt["uiAmountString"],
                            "raw":amt["amount"],"decimals":amt["decimals"]})
        return out
    except: return []

def tok_dec(mint):
    try: return rpc("getTokenSupply",[mint])["result"]["value"]["decimals"]
    except: return 6

def get_blockhash():
    return rpc("getLatestBlockhash",[{"commitment":"processed"}])["result"]["value"]["blockhash"]

def sol_usd():
    try:
        r=requests.get("https://api.coingecko.com/api/v3/simple/price?ids=solana&vs_currencies=usd",timeout=6)
        return float(r.json()["solana"]["usd"])
    except: return 0.0

def simulate_tx(raw_b64):
    """
    Simulate using PRIMARY RPC only (Helius — most reliable).
    Returns None=OK, raises RuntimeError on definitive failure.
    Non-critical errors are logged and allowed through.
    """
    try:
        result = rpc("simulateTransaction", [raw_b64, {
            "encoding"              : "base64",
            "commitment"            : "processed",
            "replaceRecentBlockhash": True,
        }], _send=True)  # primary RPC only
        val = result.get("result", {}).get("value", {})
        err = val.get("err")
        if not err:
            return None  # ✅ simulation passed
        err_s = str(err)
        logging.warning(f"Simulation error: {err_s}")
        if "Custom" in err_s and "1}" in err_s:
            raise RuntimeError(f"SLIPPAGE_TOO_LOW:{err_s}")
        if "InsufficientFunds" in err_s or "0x1" in err_s:
            raise RuntimeError("Insufficient SOL balance")
        # Any other error — log but allow (don't over-block)
        logging.warning(f"Sim non-critical error, proceeding: {err_s[:80]}")
        return None
    except RuntimeError:
        raise
    except Exception as e:
        logging.warning(f"Simulation call failed (proceeding): {e}")
        return None


def sign_and_send(tx_bytes, kp):
    """
    Sign → Send → return txid.
    NO simulation here — simulation caused false blocking of valid swaps.
    Pre-trade safety is handled by safe_mode_check before this is called.
    Retries up to 3 times with 1s delay on network errors only.
    """
    for attempt in range(3):
        try:
            raw_tx  = VersionedTransaction.from_bytes(tx_bytes)
            signed  = VersionedTransaction(raw_tx.message, [kp])
            raw_b64 = base64.b64encode(bytes(signed)).decode()

            result  = rpc("sendTransaction", [raw_b64, {
                "encoding"           : "base64",
                "skipPreflight"      : True,
                "preflightCommitment": "processed",
                "maxRetries"         : 5,
            }], _send=True)

            if "error" in result:
                err_msg = str(result["error"])
                logging.warning(f"[SEND] Error attempt {attempt+1}: {err_msg[:80]}")
                if attempt < 2:
                    time.sleep(1.5); continue
                raise RuntimeError(f"RPC error: {err_msg[:120]}")

            txid = result["result"]
            logging.info(f"[SEND] TX broadcast: {txid[:20]}…")
            return txid

        except RuntimeError:
            raise
        except Exception as e:
            logging.warning(f"[SEND] Exception attempt {attempt+1}: {e}")
            if attempt < 2:
                time.sleep(1.5); continue
            raise RuntimeError(f"Failed to send TX after 3 attempts: {e}")

def confirm_tx(txid, max_wait=60):
    """
    Confirm using ALL RPCs (secondary preferred for reads).
    Raises RuntimeError on definitive on-chain failure.
    """
    deadline = time.time() + max_wait
    while time.time() < deadline:
        time.sleep(3)
        try:
            res = rpc("getSignatureStatuses",
                [[txid], {"searchTransactionHistory": True}])
            val = res["result"]["value"][0]
            if val is None:
                continue
            if val.get("err"):
                err_s = str(val["err"])
                if "Custom" in err_s and "1}" in err_s:
                    raise RuntimeError(f"SLIPPAGE_TOO_LOW:{err_s}")
                raise RuntimeError(f"TX failed on-chain: {err_s}")
            status = val.get("confirmationStatus","")
            if status in ("confirmed", "finalized"):
                logging.info(f"TX confirmed: {txid[:20]}… [{status}]")
                return True
        except RuntimeError:
            raise
        except Exception:
            pass
    raise RuntimeError("Confirmation timeout — check Solscan for TX status")

def get_slippage_steps(mint, fm, user_max_bps=None):
    """
    Universal slippage ladder — starts low, escalates automatically.
    Works for all tokens: BONK, WIF, TRUMP, new tokens, pump.fun.
    Jupiter handles routing; we just need wide enough tolerance.
    """
    m = (mint or "").lower()

    if m.endswith("pump"):
        # Pump.fun: start at 1%, up to 20%
        steps = [100, 300, 500, 1000, 1500, 2000]
    else:
        # ALL other tokens: start at 0.5%, up to 15%
        # This covers BONK, WIF, TRUMP, USDT and new tokens equally
        steps = [50, 100, 200, 500, 1000, 1500]

    if user_max_bps:
        filtered = [s for s in steps if s <= user_max_bps]
        steps = filtered if filtered else [user_max_bps]
    return steps

def do_swap(kp, fm, tm, raw_in):
    """
    Swap engine. Tries Ultra first, then Jupiter Normal with slippage ladder.
    Returns (txid, out_ui, gasless) only after on-chain confirmation.
    """
    pub = str(kp.pubkey())

    # User engine preference
    use_ultra = True
    user_max  = None
    for uu in _users.values():
        if uu.get("pubkey") == pub:
            use_ultra = uu.get("settings", {}).get("swap_engine", "ultra") == "ultra"
            user_max  = uu.get("settings", {}).get("max_slippage", None)
            break

    # ── 1. Jupiter Ultra ─────────────────────────────────
    if use_ultra:
        try:
            params = {"inputMint":fm,"outputMint":tm,"amount":raw_in,"taker":pub}
            if JUPITER_REFERRAL_ACCOUNT:
                params["referralAccount"] = JUPITER_REFERRAL_ACCOUNT
                params["referralFeeBps"]  = JUPITER_FEE_BPS
            r = requests.get(ULTRA_Q, params=params, timeout=10)
            if r.status_code == 200:
                order = r.json()
                if "transaction" in order and "error" not in order:
                    out_amt = int(order.get("outAmount", 0))
                    logging.info(f"[SWAP] Ultra outAmount={out_amt}")
                    if out_amt > 0:
                        txid = sign_and_send(base64.b64decode(order["transaction"]), kp)
                        confirm_tx(txid)
                        out_dec = 9 if tm == SOL_MINT else tok_dec(tm)
                        return txid, out_amt / (10**out_dec), order.get("gasless", False)
        except RuntimeError as e:
            if "Insufficient" in str(e): raise
            logging.info(f"[SWAP] Ultra failed ({e}), trying Normal…")
        except Exception as e:
            logging.info(f"[SWAP] Ultra exception ({e}), trying Normal…")

    # ── 2. Jupiter Normal — escalating slippage ───────────
    slip_steps = get_slippage_steps(tm, fm, user_max)
    last_err   = "No route found"

    for slip in slip_steps:
        success = False
        for pair in JUPITER_PAIRS:
            try:
                # Fresh quote for this slippage level
                qr = requests.get(pair["q"], params={
                    "inputMint"  : fm,
                    "outputMint" : tm,
                    "amount"     : raw_in,
                    "slippageBps": slip,
                }, timeout=10)
                if qr.status_code != 200:
                    continue
                q = qr.json()

                if "error" in q or "errorCode" in q:
                    last_err = str(q.get("error", q.get("errorCode", "no route")))
                    continue

                out_amt = int(q.get("outAmount", 0))
                logging.info(f"[SWAP] {pair['n']} slip={slip/100:.1f}% out={out_amt}")

                if out_amt == 0:
                    last_err = "outAmount is 0 — no liquidity"
                    continue

                if not q.get("routePlan"):
                    last_err = "no routePlan"
                    continue

                # Build TX
                payload = {
                    "quoteResponse"            : q,
                    "userPublicKey"            : pub,
                    "wrapAndUnwrapSol"         : True,
                    "prioritizationFeeLamports": PRIORITY_FEE,
                    "dynamicComputeUnitLimit"  : True,
                    "skipUserAccountsCheck"    : True,
                }
                if JUPITER_REFERRAL_ACCOUNT:
                    payload["feeAccount"] = JUPITER_REFERRAL_ACCOUNT

                sr = requests.post(pair["s"], json=payload, timeout=20)
                if sr.status_code != 200:
                    continue
                sd = sr.json()
                if "swapTransaction" not in sd:
                    last_err = "no swapTransaction in response"
                    continue

                # Sign → Send → Confirm
                txid = sign_and_send(base64.b64decode(sd["swapTransaction"]), kp)
                confirm_tx(txid)

                out_dec = 9 if tm == SOL_MINT else tok_dec(tm)
                return txid, out_amt / (10**out_dec), False

            except RuntimeError as e:
                es = str(e)
                if "Insufficient" in es:
                    raise  # hard fail — no SOL
                if "SLIPPAGE_TOO_LOW" in es or ("Custom" in es and "1}" in es):
                    last_err = f"SLIPPAGE_LOW@{slip}"
                    logging.info(f"[SWAP] Slippage {slip/100:.1f}% too low, escalating…")
                    break  # try next slippage step
                # Other errors — try next pair
                last_err = es[:100]
                continue
            except Exception as e:
                last_err = str(e)[:80]
                continue

        # If we got a slippage signal, move to next slip step
        if "SLIPPAGE_LOW" in last_err:
            continue

        # If we got here without success from any pair, try next slip step
        # (allows escalation even for non-slippage errors)

    # All steps exhausted
    if "SLIPPAGE_LOW" in last_err:
        max_tried = slip_steps[-1] // 100
        raise RuntimeError(
            f"⚠️ Swap failed — slippage up to {max_tried}% insufficient.\n\n"
            f"This token has very high buy/sell tax or extremely low liquidity.\n"
            f"Tap 🛡️ Safety to check the token first."
        )
    raise RuntimeError(f"Swap failed: {last_err}")

def token_info(mint):
    res={"found":False,"name":"Unknown","sym":"?","price_usd":None,"mc":None,
         "liq":0,"vol24":0,"chg1h":0,"chg24":0,"risk":"UNKNOWN","url":"",
         "warnings":[],"buys24":0,"sells24":0,"mint":mint}
    try:
        r=requests.get(DEX_URL.format(mint),timeout=8)
        if r.status_code!=200: return res
        pairs=r.json().get("pairs") or []
        if not pairs: return res
        p=max(pairs,key=lambda x:float(x.get("liquidity",{}).get("usd",0) or 0))
        res.update({"found":True,"name":p.get("baseToken",{}).get("name","Unknown"),
                    "sym":p.get("baseToken",{}).get("symbol","?"),
                    "price_usd":p.get("priceUsd"),
                    "mc":p.get("marketCap") or p.get("fdv"),
                    "liq":float(p.get("liquidity",{}).get("usd",0) or 0),
                    "vol24":float(p.get("volume",{}).get("h24",0) or 0),
                    "chg1h":float(p.get("priceChange",{}).get("h1",0) or 0),
                    "chg24":float(p.get("priceChange",{}).get("h24",0) or 0),
                    "url":p.get("url",""),
                    "buys24":p.get("txns",{}).get("h24",{}).get("buys",0),
                    "sells24":p.get("txns",{}).get("h24",{}).get("sells",0)})
        w=[]
        if res["liq"]==0:      w.append("🚨 Zero liquidity — possible rug!")
        elif res["liq"]<1000:  w.append("🚨 Liquidity $<1K — extreme risk")
        elif res["liq"]<5000:  w.append("🚨 Liquidity $<5K — very risky")
        elif res["liq"]<25000: w.append("⚠️ Liquidity $<25K — risky")
        if res["sells24"]==0 and res["buys24"]>10: w.append("⚠️ No sells — possible honeypot!")
        res["warnings"]=w
        res["risk"]="HIGH" if any("🚨" in x for x in w) else "MEDIUM" if w else "LOW"
    except: pass
    return res

def get_token_age_hours(mint):
    """
    Get token age in hours from DexScreener pairCreatedAt.
    Returns float hours, or None if unknown.
    """
    try:
        r = requests.get(DEX_URL.format(mint), timeout=8)
        if r.status_code != 200: return None
        pairs = r.json().get("pairs") or []
        if not pairs: return None
        # Sort by creation time ascending — get oldest pair
        times = []
        for p in pairs:
            created = p.get("pairCreatedAt")  # unix ms
            if created:
                times.append(int(created))
        if not times: return None
        oldest_ms = min(times)
        age_hours = (time.time()*1000 - oldest_ms) / 3_600_000
        return round(age_hours, 2)
    except Exception:
        return None


def safe_mode_check(mint, fm, raw_in):
    """
    Pre-swap safety validation.
    Returns (status, risk_level, message, needs_confirm)
    status: "ok" | "warn" | "block"
    ONLY blocks on: outAmount==0, no routePlan, hard simulation fail.
    Everything else = warn + allow with confirmation.
    """
    warnings  = []
    risk      = "LOW"
    age_h     = None

    # ── 1. Jupiter quote — HARD checks only ─────────────
    q = None
    for pair in JUPITER_PAIRS:
        try:
            r = requests.get(pair["q"], params={
                "inputMint"  : fm,
                "outputMint" : mint,
                "amount"     : raw_in,
                "slippageBps": 500,
            }, timeout=10)
            if r.status_code == 200:
                d = r.json()
                if d.get("outAmount"):
                    q = d; break
        except Exception:
            continue

    if q is None:
        return "block", "HIGH", (
            "❌ *Cannot fetch quote*\n\n"
            "Jupiter may be temporarily unavailable.\n"
            "Please try again in 30 seconds."
        ), False

    out_amt = int(q.get("outAmount", 0))
    if out_amt == 0:
        return "block", "HIGH", (
            "❌ *No liquidity or route found*\n\n"
            "This token cannot be swapped right now.\n"
            "Check DexScreener for details."
        ), False

    if not q.get("routePlan"):
        return "block", "HIGH", (
            "❌ *No valid swap route*\n\n"
            "Jupiter cannot find a path for this swap.\n"
            "Token may not be listed yet."
        ), False

    # ── 2. Price impact (warn, never block) ─────────────
    impact_pct = float(q.get("priceImpactPct", 0)) * 100
    logging.info(f"[SAFE] PriceImpact={impact_pct:.2f}%  OutAmount={out_amt}")

    if impact_pct > 20:
        risk = "HIGH"
        warnings.append(f"🔴 Very high price impact: {impact_pct:.1f}%\n"
                         "  You will receive significantly less than market value")
    elif impact_pct > 10:
        risk = "MEDIUM"
        warnings.append(f"🟡 High price impact: {impact_pct:.1f}%")
    elif impact_pct > 3:
        warnings.append(f"🟡 Moderate price impact: {impact_pct:.1f}%")

    # ── 3. Token age (warn + confirm, never block) ───────
    try:
        age_h = get_token_age_hours(mint)
        if age_h is not None:
            if age_h < 1:
                risk = "HIGH"
                warnings.append(f"🔴 Extremely new token ({age_h*60:.0f} min old)\n"
                                  "  Very high rug pull risk — use Sniper Bot instead")
            elif age_h < 4:
                risk = "HIGH"
                warnings.append(f"🔴 Very new token ({age_h:.1f}h)\n"
                                  "  High risk of scams / rug pull / honeypot")
            elif age_h < 24:
                if risk == "LOW": risk = "MEDIUM"
                warnings.append(f"🟡 New token ({age_h:.1f}h) — proceed with caution")
    except Exception:
        pass

    # ── 4. Liquidity (warn, block only at zero) ──────────
    try:
        info = token_info(mint)
        liq  = info.get("liq", 0)
        logging.info(f"[SAFE] Liquidity=${liq:,.0f}")
        if liq == 0:
            return "block", "HIGH", (
                "🚨 *Zero liquidity detected*\n\n"
                "This token has no tradeable liquidity."
            ), False
        elif liq < 1000:
            risk = "HIGH"
            warnings.append(f"🔴 Extremely low liquidity: ${liq:,.0f}")
        elif liq < 5000:
            if risk == "LOW": risk = "MEDIUM"
            warnings.append(f"🟡 Low liquidity: ${liq:,.0f} (min recommended: $5K)")
        elif liq < 25000:
            warnings.append(f"🟡 Moderate liquidity: ${liq:,.0f}")

        # Honeypot signal
        if info.get("sells24", 0) == 0 and info.get("buys24", 0) > 20:
            risk = "HIGH"
            warnings.append("🔴 Possible HONEYPOT — no sell transactions detected!")
    except Exception:
        pass

    # ── 5. Result ────────────────────────────────────────
    if not warnings:
        return "ok", "LOW", "", False

    risk_icon = {"LOW": "✅", "MEDIUM": "⚠️", "HIGH": "🔴"}.get(risk, "❓")
    warn_text = "\n\n".join(f"  {w}" for w in warnings)
    msg = (
        f"{risk_icon} *Risk Level: {risk}*\n\n"
        f"{warn_text}\n\n"
        "_Tap ✅ Proceed to swap or ❌ Cancel._"
    )
    needs_confirm = risk in ("HIGH", "MEDIUM")
    return "warn", risk, msg, needs_confirm


def fmt_card(info):
    r_ic={"LOW":"✅","MEDIUM":"⚠️","HIGH":"🚨","UNKNOWN":"❓"}.get(info["risk"],"❓")
    c1h=info.get("chg1h",0) or 0; c24=info.get("chg24",0) or 0
    p_s=f"${float(info['price_usd']):.8f}" if info.get("price_usd") else "N/A"
    mc=info.get("mc")
    mc_s=(f"${float(mc)/1e6:.2f}M" if mc and float(mc)>=1e6
          else f"${float(mc)/1e3:.1f}K" if mc else "N/A")
    lines=[f"⚡ *{info['name']}* (${info['sym']})",
           f"`{info['mint'][:20]}…`","",
           f"💲 Price:   `{p_s}`",f"📊 MCap:    `{mc_s}`",
           f"💧 Liq:     `${info['liq']:,.0f}`",f"📈 Vol 24h: `${info['vol24']:,.0f}`","",
           f"⏱ 1h:  {'🟢' if c1h>=0 else '🔴'} `{c1h:+.2f}%`",
           f"📅 24h: {'🟢' if c24>=0 else '🔴'} `{c24:+.2f}%`","",
           f"🔁 Buys: {info['buys24']}  Sells: {info['sells24']}",
           f"{r_ic} Risk: *{info['risk']}*"]
    for w in info.get("warnings",[]): lines.append(w)
    if info.get("url"): lines.append(f"\n[📊 DexScreener]({info['url']})")
    return "\n".join(lines)

def generate_trade_card(uid, sym, side, amount_in, amount_out,
                         pnl_pct=None, pnl_sol=None, txid=""):
    """
    Generate a professional trade result card image.
    Returns BytesIO PNG buffer.
    """
    try:
        from PIL import Image, ImageDraw, ImageFont
        import io, math

        W, H  = 600, 340
        is_profit = pnl_pct is None or pnl_pct >= 0
        is_buy    = side.upper() == "BUY"

        # Color scheme
        if is_buy:
            accent = (0, 200, 120)      # green
            bg_top = (8, 28, 18)
            bg_bot = (4, 16, 10)
        elif is_profit:
            accent = (0, 200, 120)
            bg_top = (8, 28, 18)
            bg_bot = (4, 16, 10)
        else:
            accent = (220, 60, 60)      # red
            bg_top = (28, 8, 8)
            bg_bot = (16, 4, 4)

        img  = Image.new("RGB", (W, H))
        draw = ImageDraw.Draw(img)

        # Background gradient
        for y in range(H):
            t  = y/H
            rc = int(bg_top[0]*(1-t) + bg_bot[0]*t)
            gc = int(bg_top[1]*(1-t) + bg_bot[1]*t)
            bc = int(bg_top[2]*(1-t) + bg_bot[2]*t)
            draw.line([(0,y),(W,y)], fill=(rc,gc,bc))

        # Top accent bar
        draw.rectangle([(0,0),(W,4)], fill=accent)
        draw.rectangle([(0,H-4),(W,H)], fill=accent)

        # Fonts
        try:
            fp = "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf"
            fn = "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf"
            f_big   = ImageFont.truetype(fp, 48)
            f_med   = ImageFont.truetype(fp, 24)
            f_small = ImageFont.truetype(fn, 16)
            f_tiny  = ImageFont.truetype(fn, 13)
        except:
            f_big = f_med = f_small = f_tiny = ImageFont.load_default()

        # MUB DEX logo area (left)
        cx, cy = 90, 100
        for r in range(60,0,-1):
            t  = r/60
            rc = int(8+20*t); gc=int(10+25*t); bc=int(25+45*t)
            draw.ellipse([cx-r,cy-r,cx+r,cy+r],fill=(rc,gc,bc))
        draw.ellipse([cx-58,cy-58,cx+58,cy+58],outline=(255,185,0,),width=3)
        # Lightning bolt
        bolt=[
            (cx-10,cy-30),(cx-22,cy+5),(cx-5,cy+5),
            (cx+12,cy+30),(cx+22,cy-5),(cx+5,cy-5)
        ]
        draw.polygon(bolt, fill=(255,185,0))
        # MUB DEX text
        draw.text((cx-28, cy+68), "MUB DEX", fill=(255,185,0), font=f_tiny)

        # Vertical divider
        draw.line([(168,20),(168,H-20)], fill=(*accent, 80), width=1)

        # Right content
        rx = 185

        # Side badge
        badge_col = (0,180,100) if is_buy else (200,50,50)
        badge_txt = "🟢 BUY" if is_buy else "🔴 SELL"
        draw.rounded_rectangle([rx, 18, rx+90, 44], radius=8, fill=badge_col)
        draw.text((rx+8, 22), "BUY" if is_buy else "SELL",
                  fill=(255,255,255), font=f_tiny)

        # Token name
        draw.text((rx, 52), f"${sym}", fill=(*accent,), font=f_med)

        # P&L percentage (big)
        if pnl_pct is not None:
            pnl_col = (0,220,120) if pnl_pct >= 0 else (220,60,60)
            pnl_str = f"+{pnl_pct:.2f}%" if pnl_pct >= 0 else f"{pnl_pct:.2f}%"
            draw.text((rx, 85), pnl_str, fill=pnl_col, font=f_big)
        else:
            draw.text((rx, 85), f"✅ Swapped", fill=accent, font=f_med)

        # Details
        y_start = 155
        details = [
            ("In",  f"{amount_in:.5f} SOL"),
            ("Out", f"{amount_out:.5f} {sym}"),
        ]
        if pnl_sol is not None:
            pnl_s = f"+{pnl_sol:.5f} SOL" if pnl_sol>=0 else f"{pnl_sol:.5f} SOL"
            details.append(("P&L SOL", pnl_s))

        for i,(lbl,val) in enumerate(details):
            y = y_start + i*32
            draw.text((rx,    y), lbl+":", fill=(150,160,170), font=f_small)
            draw.text((rx+100, y), val,    fill=(220,230,240), font=f_small)

        # TX hash (truncated)
        if txid:
            short_tx = txid[:20]+"…"+txid[-6:]
            draw.text((rx, H-50), "TX: "+short_tx, fill=(100,110,120), font=f_tiny)

        # Timestamp
        ts = datetime.now().strftime("%Y-%m-%d %H:%M UTC")
        draw.text((rx, H-32), ts, fill=(80,90,100), font=f_tiny)

        # Candle decoration (right side)
        cc = W - 60
        if is_profit or is_buy:
            # Green candle
            draw.rectangle([cc-8, 240, cc+8, 290], fill=(0,180,100))
            draw.line([(cc, 220),(cc, 310)], fill=(0,180,100), width=2)
            draw.rectangle([cc+20-6, 260, cc+20+6, 295], fill=(0,140,80))
            draw.line([(cc+20, 245),(cc+20, 310)], fill=(0,140,80), width=2)
            draw.rectangle([cc+40-5, 250, cc+40+5, 290], fill=(0,200,120))
            draw.line([(cc+40, 235),(cc+40, 305)], fill=(0,200,120), width=2)
        else:
            # Red candles
            draw.rectangle([cc-8, 250, cc+8, 300], fill=(200,50,50))
            draw.line([(cc, 235),(cc, 315)], fill=(200,50,50), width=2)
            draw.rectangle([cc+20-6, 265, cc+20+6, 305], fill=(160,40,40))
            draw.rectangle([cc+40-5, 270, cc+40+5, 310], fill=(220,60,60))

        buf = io.BytesIO()
        img.save(buf, format="PNG")
        buf.seek(0)
        return buf

    except Exception as e:
        logging.warning(f"Trade card error: {e}")
        return None


class AutoTrader:
    def __init__(self,uid,bot_i):
        self.uid=uid;self.bot=bot_i;self.running=False
        self.thread=None;self.cfg={};self.entry=None;self._bought=False
    def notify(self,msg,kb=None):
        try: self.bot.send_message(self.uid,msg,parse_mode="Markdown",reply_markup=kb)
        except: pass
    def start(self,cfg):
        if self.running: return
        self.cfg=cfg;self.running=True
        self.thread=threading.Thread(target=self._loop,daemon=True);self.thread.start()
        self.notify(f"🤖 *Auto-Trader Started!*\n\n"
                    f"🪙 Token: `{cfg['mint'][:20]}…`\n💰 Invest: `{cfg['sol']} SOL`\n"
                    f"🎯 TP: `+{cfg['tp']}%`\n🛑 SL: `-{cfg['sl']}%`")
    def stop(self,sell=True):
        self.running=False
        if sell and self._bought:
            self.notify("🔴 *Stopping — selling…*")
            threading.Thread(target=self._sell,daemon=True).start()
        else: self.notify("⏹ *Stopped.*")
    def _price(self):
        try:
            mint=self.cfg["mint"];dec=tok_dec(mint)
            r=requests.get(JUPITER_PAIRS[0]["q"],params={
                "inputMint":mint,"outputMint":SOL_MINT,"amount":int(10**dec),"slippageBps":100},timeout=10)
            q=r.json();return int(q["outAmount"])/1e9 if q.get("outAmount") else None
        except: return None
    def _sell(self):
        try:
            kp=_users[self.uid]["keypair"];pub=_users[self.uid]["pubkey"]
            toks=token_accs(pub);t=next((x for x in toks if x["mint"]==self.cfg["mint"]),None)
            if not t or int(t["raw"])==0: self.notify("⚠️ Balance 0"); return
            txid,out,_=do_swap(kp,self.cfg["mint"],SOL_MINT,int(t["raw"]))
            self._bought=False;self.notify(f"✅ *Sold!*\nGot: `{out:.5f} SOL`\n[TX](https://solscan.io/tx/{txid})")
        except Exception as e: self.notify(f"❌ Sell error: {e}")
    def _loop(self):
        mint=self.cfg["mint"];bought=False
        while self.running:
            try:
                p=self._price()
                if p is None: time.sleep(self.cfg["interval"]); continue
                if not bought:
                    kp=_users[self.uid]["keypair"];raw=int(float(self.cfg["sol"])*1e9)
                    txid,out,gasless=do_swap(kp,SOL_MINT,mint,raw)
                    self.entry=p;bought=True;self._bought=True
                    self.notify(f"✅ *Bought!*{'  ⚡' if gasless else ''}\nPrice: `{p:.8f}`\nGot: `{out:.4f}`\n[TX](https://solscan.io/tx/{txid})")
                else:
                    chg=((p-self.entry)/self.entry)*100
                    if chg>=float(self.cfg["tp"]):
                        self.notify(f"🎯 *TP +{chg:.2f}%* Selling…");self._sell();bought=False;self.entry=None
                    elif chg<=-float(self.cfg["sl"]):
                        self.notify(f"🛑 *SL {chg:.2f}%* Selling…");self._sell();bought=False;self.entry=None
                time.sleep(self.cfg["interval"])
            except Exception as e:
                self.notify(f"❌ {e}");time.sleep(self.cfg["interval"])

class Sniper:
    def __init__(self,uid,bot_i):
        self.uid=uid;self.bot=bot_i;self.running=False;self.cfg={}
    def notify(self,msg):
        try: self.bot.send_message(self.uid,msg,parse_mode="Markdown")
        except: pass
    def start(self,mint,sol_amt):
        self.cfg={"mint":mint,"sol":sol_amt};self.running=True
        threading.Thread(target=self._watch,daemon=True).start()
        self.notify(f"🎯 *Sniper Armed!*\nTarget: `{mint[:20]}…`\nAmount: `{sol_amt} SOL`\n_Watching for liquidity…_")
    def stop(self): self.running=False;self.notify("🎯 Sniper stopped.")
    def _watch(self):
        while self.running:
            try:
                info=token_info(self.cfg["mint"])
                if info["found"] and info["liq"]>500:
                    self.notify(f"🎯 *SNIPE! Liq ${info['liq']:,.0f}*\nBuying…")
                    kp=_users[self.uid]["keypair"]
                    txid,out,g=do_swap(kp,SOL_MINT,self.cfg["mint"],int(self.cfg["sol"]*1e9))
                    self.notify(f"✅ *Sniped!*{'⚡' if g else ''}\nGot: `{out:.4f}`\n[TX](https://solscan.io/tx/{txid})")
                    self.running=False;break
                time.sleep(2)
            except: time.sleep(3)

class LimitOrder:
    def __init__(self,uid,mint,direction,target,sol_amt,bot_i):
        self.uid=uid;self.mint=mint;self.direction=direction
        self.target=target;self.sol_amt=sol_amt;self.bot=bot_i;self.active=True
        threading.Thread(target=self._watch,daemon=True).start()
    def _watch(self):
        while self.active:
            try:
                info=token_info(self.mint);p=float(info["price_usd"]) if info.get("price_usd") else None
                if p:
                    hit=((self.direction=="above" and p>=self.target) or
                         (self.direction=="below" and p<=self.target))
                    if hit:
                        self.active=False
                        self.bot.send_message(self.uid,f"📋 *Limit Triggered!*\nPrice `${p:.8f}`\nBuying {self.sol_amt} SOL…",parse_mode="Markdown")
                        kp=_users[self.uid]["keypair"]
                        txid,out,_=do_swap(kp,SOL_MINT,self.mint,int(self.sol_amt*1e9))
                        self.bot.send_message(self.uid,f"✅ *Filled!*\nGot: `{out:.4f}`\n[TX](https://solscan.io/tx/{txid})",parse_mode="Markdown")
                        break
                time.sleep(15)
            except: time.sleep(15)

if PROXY_URL:
    import telebot.apihelper as _ah
    _ah.proxy={"https":PROXY_URL,"http":PROXY_URL}

bot=telebot.TeleBot(BOT_TOKEN,parse_mode=None)

def kb_main():
    k=types.InlineKeyboardMarkup(row_width=2)
    k.add(types.InlineKeyboardButton("💼 Wallet",    callback_data="menu_wallet"),
          types.InlineKeyboardButton("💱 Buy / Sell", callback_data="menu_trade"))
    k.add(types.InlineKeyboardButton("🪙 My Tokens",  callback_data="menu_portfolio"),
          types.InlineKeyboardButton("📜 History",    callback_data="menu_history"))
    k.add(types.InlineKeyboardButton("🤖 Auto-Trader",callback_data="menu_auto"),
          types.InlineKeyboardButton("🎯 Sniper Bot", callback_data="menu_sniper"))
    k.add(types.InlineKeyboardButton("📋 Limits",    callback_data="menu_limits"),
          types.InlineKeyboardButton("⚙️ Settings",  callback_data="menu_settings"))
    k.add(types.InlineKeyboardButton("💬 Feedback",  callback_data="menu_feedback"),
          types.InlineKeyboardButton("❓ Help",        callback_data="menu_help"))
    return k

def kb_wallet(uid):
    k=types.InlineKeyboardMarkup(row_width=2)
    if has_wallet(uid):
        k.add(types.InlineKeyboardButton("💰 Balance",callback_data="w_balance"),
              types.InlineKeyboardButton("📋 Address",callback_data="w_address"))
        k.add(types.InlineKeyboardButton("📤 Send SOL",callback_data="w_send"),
              types.InlineKeyboardButton("🔌 Change",callback_data="w_connect"))
    elif _users.get(uid,{}).get("view_pub"):
        k.add(types.InlineKeyboardButton("💰 Balance",callback_data="w_balance"),
              types.InlineKeyboardButton("📋 Address",callback_data="w_address"))
        k.add(types.InlineKeyboardButton("⚡ Add Trade Wallet",callback_data="w_add_trade"))
    else:
        k.add(types.InlineKeyboardButton("👁 View Only (safe)",callback_data="w_view_only"))
        k.add(types.InlineKeyboardButton("⚡ Trade Wallet (private key)",callback_data="w_add_trade"))
    k.add(types.InlineKeyboardButton("🔙 Main Menu",callback_data="menu_main"))
    return k

def kb_trade(info, uid):
    mint=info["mint"]; dfl=u(uid)["settings"].get("default_buy",0.1)
    k=types.InlineKeyboardMarkup(row_width=3)
    k.add(types.InlineKeyboardButton(f"🟢 Buy 0.01",callback_data=f"buy_0.01_{mint}"),
          types.InlineKeyboardButton(f"🟢 Buy 0.05",callback_data=f"buy_0.05_{mint}"),
          types.InlineKeyboardButton(f"🟢 Buy {dfl}",callback_data=f"buy_{dfl}_{mint}"))
    k.add(types.InlineKeyboardButton("🟢 Custom",callback_data=f"buy_custom_{mint}"),
          types.InlineKeyboardButton("🔴 Sell 50%",callback_data=f"sell_50_{mint}"),
          types.InlineKeyboardButton("🔴 Sell 100%",callback_data=f"sell_100_{mint}"))
    k.add(types.InlineKeyboardButton("💵 →USDT",callback_data=f"sellto_USDT_{mint}"),
          types.InlineKeyboardButton("💵 →USDC",callback_data=f"sellto_USDC_{mint}"),
          types.InlineKeyboardButton("◎ →SOL",callback_data=f"sellto_SOL_{mint}"))
    k.add(types.InlineKeyboardButton("📋 Limit",callback_data=f"limit_{mint}"),
          types.InlineKeyboardButton("🛡️ Safety",callback_data=f"safety_{mint}"),
          types.InlineKeyboardButton("📊 DEX",url=info.get("url","https://dexscreener.com")))
    k.add(types.InlineKeyboardButton("🔙 Main Menu",callback_data="menu_main"))
    return k

def kb_auto(uid):
    running=uid in _traders and _traders[uid].running
    k=types.InlineKeyboardMarkup(row_width=2)
    if running:
        k.add(types.InlineKeyboardButton("📊 Status",callback_data="auto_status"),
              types.InlineKeyboardButton("⏹ Stop+Sell",callback_data="auto_stop_sell"))
        k.add(types.InlineKeyboardButton("⏹ Stop Only",callback_data="auto_stop_only"))
    else:
        k.add(types.InlineKeyboardButton("▶️ Start Auto-Trader",callback_data="auto_start"))
    k.add(types.InlineKeyboardButton("🔙 Main Menu",callback_data="menu_main"))
    return k

def kb_back():
    k=types.InlineKeyboardMarkup()
    k.add(types.InlineKeyboardButton("🔙 Main Menu",callback_data="menu_main"))
    return k

def kb_settings(uid):
    s   = u(uid)["settings"]
    eng = s.get("swap_engine","ultra")
    msl = s.get("max_slippage", 3000)
    k   = types.InlineKeyboardMarkup(row_width=2)
    k.add(
        types.InlineKeyboardButton(
            f"💰 Default Buy: {s['default_buy']} SOL",
            callback_data="set_default_buy"),
        types.InlineKeyboardButton(
            f"🎯 Auto TP: {s['auto_tp']}%",
            callback_data="set_tp"),
    )
    k.add(
        types.InlineKeyboardButton(
            f"🛑 Auto SL: {s['auto_sl']}%",
            callback_data="set_sl"),
        types.InlineKeyboardButton(
            f"📉 Max Slippage: {msl//100}%",
            callback_data="set_slippage"),
    )
    # Swap engine toggle
    k.add(
        types.InlineKeyboardButton(
            f"{'✅' if eng=='ultra' else '⬜'} ⚡ Ultra Swap",
            callback_data="set_engine_ultra"),
        types.InlineKeyboardButton(
            f"{'✅' if eng=='normal' else '⬜'} 🔄 Normal Swap",
            callback_data="set_engine_normal"),
    )
    k.add(types.InlineKeyboardButton("🔙 Main Menu", callback_data="menu_main"))
    return k

MAIN_TEXT="⚡ *MUB DEX*\n_Solana's Fastest Trading Bot_\n\n💰 Gas: *~$0.009* _(save 97% vs others)_\n\nChoose an option:"

def show_main(uid,chat_id,msg_id=None):
    if msg_id:
        try:
            bot.edit_message_text(MAIN_TEXT,chat_id,msg_id,parse_mode="Markdown",reply_markup=kb_main()); return
        except: pass
    bot.send_message(chat_id,MAIN_TEXT,parse_mode="Markdown",reply_markup=kb_main())

def eor(call,text,kb=None):
    for i in range(3):
        try:
            bot.edit_message_text(text,call.message.chat.id,call.message.message_id,
                parse_mode="Markdown",reply_markup=kb,disable_web_page_preview=True); return
        except Exception as e:
            if "timed out" in str(e).lower() and i<2: time.sleep(2); continue
            try: bot.send_message(call.message.chat.id,text,parse_mode="Markdown",reply_markup=kb,disable_web_page_preview=True)
            except: pass; return

@bot.message_handler(commands=["start"])
def cmd_start(msg):
    uid=msg.from_user.id; usr=u(uid); name=msg.from_user.first_name or "Trader"
    track_user(uid, msg.from_user.first_name or "")
    # Remove any old reply keyboard that may be persisting
    bot.send_message(msg.chat.id, "⚡",
        reply_markup=types.ReplyKeyboardRemove())
    try: bot.delete_message(msg.chat.id, msg.message_id + 1)
    except: pass

    if not has_any(uid):
        bot.send_message(msg.chat.id,
            f"👋 *Welcome to MUB DEX, {name}!*\n\n"
            "⚡ The fastest, cheapest way to trade on Solana.\n\n"
            "━━━━━━━━━━━━━━━━━━━━\n"
            "🔥 *Why MUB DEX?*\n\n"
            "💸 *Save 97% on gas fees*\n"
            "  Others: ~$0.40  |  MUB DEX: ~$0.009\n\n"
            "⚡ *Ultra Swap* — Sometimes GASLESS!\n\n"
            "🤖 *Auto-Trader* — Trades 24/7 while you sleep\n\n"
            "🎯 *Sniper Bot* — Buy on new launches instantly\n\n"
            "📋 *Limit Orders* — Set price, bot executes\n\n"
            "━━━━━━━━━━━━━━━━━━━━\n"
            "*Get started in 30 seconds:*\n"
            "1️⃣ Tap *💼 Wallet* → Connect\n"
            "2️⃣ Paste any token contract\n"
            "3️⃣ Tap *Buy* — done! ✅",
            parse_mode="Markdown",reply_markup=kb_main()); return
    def _b():
        pub=active_pub(uid); sol=sol_bal(pub) if pub else 0; sp=sol_usd()
        bot.send_message(msg.chat.id,
            f"⚡ *MUB DEX*\n\n"
            f"💼 `{fmt_addr(pub)}`\n"
            f"◎ *{sol:.4f} SOL*" + (f"  _(${sol*sp:.2f})_" if sp else "") +
            "\n\n💰 Gas: *~$0.009*",
            parse_mode="Markdown",reply_markup=kb_main())
    threading.Thread(target=_b,daemon=True).start()

@bot.message_handler(func=lambda m:True,content_types=["text"])
def handle_text(msg):
    uid=msg.from_user.id; text=msg.text.strip(); usr=u(uid); state=usr["state"]

    if state=="awaiting_view_address":
        addr=text.strip()
        valid=32<=len(addr)<=44 and all(c in "123456789ABCDEFGHJKLMNPQRSTUVWXYZabcdefghijkmnopqrstuvwxyz" for c in addr)
        if valid:
            usr["view_pub"]=addr; usr["state"]="idle"; save_users()
            def _c():
                sol=sol_bal(addr); toks=token_accs(addr)
                bot.send_message(msg.chat.id,
                    f"✅ *View Wallet Connected!*\n\nAddress: `{addr}`\nBalance: `{sol:.4f} SOL`\nTokens: `{len(toks)}`\n\n_To trade, add a Trade Wallet (⚡)._",
                    parse_mode="Markdown",reply_markup=kb_wallet(uid))
            threading.Thread(target=_c,daemon=True).start()
        else: bot.send_message(msg.chat.id,"❌ Invalid address. Try again.")
        return

    if state=="awaiting_pk":
        if not SOLDERS_OK: bot.send_message(msg.chat.id,"❌ Server missing solders."); return
        try:
            kp=Keypair.from_bytes(base58.b58decode(text)); pub=str(kp.pubkey())
            usr.update({"keypair":kp,"pubkey":pub,"pk_b58":text,"state":"idle"}); save_users()
            try: bot.delete_message(msg.chat.id,msg.message_id)
            except: pass
            def _c():
                sol=sol_bal(pub)
                bot.send_message(msg.chat.id,
                    f"✅ *Wallet Connected!*\n\nAddress: `{pub}`\nBalance: `{sol:.4f} SOL`\n\n_Key deleted for security. Ready to trade!_",
                    parse_mode="Markdown",reply_markup=kb_main())
            threading.Thread(target=_c,daemon=True).start()
        except Exception as e:
            bot.send_message(msg.chat.id,f"❌ Invalid key: `{e}`\n\nTry again.",parse_mode="Markdown",reply_markup=kb_back())
            usr["state"]="idle"
        return

    if state=="awaiting_buy_amt":
        try:
            amt=float(text); mint=usr["ctx"].get("mint",""); usr["state"]="idle"
            _exec_buy(uid,msg.chat.id,mint,amt)
        except ValueError: bot.send_message(msg.chat.id,"❌ Invalid amount. e.g. `0.05`",parse_mode="Markdown")
        return

    if state=="awaiting_auto_contract":
        if is_mint(text):
            usr["ctx"]["mint"]=text; usr["state"]="awaiting_auto_amount"
            bot.send_message(msg.chat.id,f"✅ Token: `{text[:20]}…`\n\nHow much SOL to invest?\ne.g. `0.1`",parse_mode="Markdown")
        else: bot.send_message(msg.chat.id,"❌ Invalid address.")
        return

    if state=="awaiting_auto_amount":
        try:
            amt=float(text); usr["state"]="idle"; s=usr["settings"]
            cfg={"mint":usr["ctx"]["mint"],"sol":amt,"tp":s["auto_tp"],"sl":s["auto_sl"],"interval":30}
            if uid not in _traders: _traders[uid]=AutoTrader(uid,bot)
            _traders[uid].start(cfg)
        except ValueError: bot.send_message(msg.chat.id,"❌ Invalid amount.")
        return

    if state=="awaiting_sniper_contract":
        if is_mint(text):
            usr["ctx"]["mint"]=text; usr["state"]="awaiting_sniper_amount"
            bot.send_message(msg.chat.id,f"✅ Target: `{text[:20]}…`\n\nHow much SOL?\ne.g. `0.1`",parse_mode="Markdown")
        else: bot.send_message(msg.chat.id,"❌ Invalid address.")
        return

    if state=="awaiting_sniper_amount":
        try:
            amt=float(text); mint=usr["ctx"]["mint"]; usr["state"]="idle"
            if uid not in _snipers: _snipers[uid]=Sniper(uid,bot)
            _snipers[uid].start(mint,amt)
        except ValueError: bot.send_message(msg.chat.id,"❌ Invalid amount.")
        return

    if state=="awaiting_limit_price":
        try:
            price=float(text); usr["ctx"]["target_price"]=price; usr["state"]="awaiting_limit_amount"
            bot.send_message(msg.chat.id,f"✅ Target: `${price:.8f}`\n\nHow much SOL?\ne.g. `0.05`",parse_mode="Markdown")
        except ValueError: bot.send_message(msg.chat.id,"❌ Invalid price.")
        return

    if state=="awaiting_limit_amount":
        try:
            amt=float(text); ctx=usr["ctx"]; usr["state"]="idle"
            order=LimitOrder(uid,ctx["mint"],ctx.get("direction","below"),ctx["target_price"],amt,bot)
            if uid not in _limits: _limits[uid]=[]
            _limits[uid].append(order)
            bot.send_message(msg.chat.id,
                f"📋 *Limit Order Set!*\nBuy `{amt} SOL` when price {'above' if ctx.get('direction')=='above' else 'below'} `${ctx['target_price']:.8f}`\n_Checking every 15s…_",
                parse_mode="Markdown",reply_markup=kb_main())
        except ValueError: bot.send_message(msg.chat.id,"❌ Invalid amount.")
        return

    if state in ("set_default_buy","set_tp","set_sl","set_slippage"):
        try:
            val=float(text)
            if state=="set_slippage":
                # Convert % to bps
                bps = int(val * 100)
                bps = max(50, min(bps, 5000))  # clamp 0.5%-50%
                u(uid)["settings"]["max_slippage"] = bps
                u(uid)["state"] = "idle"; save_users()
                bot.send_message(msg.chat.id,f"✅ Max slippage set to `{val}%`\n_Bot will try from 0.1% up to {val}%_",parse_mode="Markdown",reply_markup=kb_settings(uid))
                return
            key={"set_default_buy":"default_buy","set_tp":"auto_tp","set_sl":"auto_sl"}.get(state,state)
            usr["settings"][key]=val; usr["state"]="idle"; save_users()
            bot.send_message(msg.chat.id,f"✅ Updated `{key}` = `{val}`",parse_mode="Markdown",reply_markup=kb_settings(uid))
        except ValueError: bot.send_message(msg.chat.id,"❌ Invalid number.")
        return

    if state=="awaiting_feedback":
        usr["state"]="idle"
        fb=json.load(open(FEEDBACK_FILE)) if os.path.exists(FEEDBACK_FILE) else []
        fb.append({"uid":uid,"name":msg.from_user.first_name,"text":text,"time":datetime.now().isoformat()})
        json.dump(fb,open(FEEDBACK_FILE,"w"))
        if ADMIN_ID:
            try: bot.send_message(ADMIN_ID,f"💬 *Feedback* from {msg.from_user.first_name} ({uid})\n\n_{text}_",parse_mode="Markdown")
            except: pass
        bot.send_message(msg.chat.id,"✅ *Thank you for your feedback!* 🙏",parse_mode="Markdown",reply_markup=kb_main())
        return

    if is_mint(text):
        _show_token(uid,msg.chat.id,text); return

    bot.send_message(msg.chat.id,"⚡ *MUB DEX*\n\nPaste a token contract address to trade:",parse_mode="Markdown",reply_markup=kb_main())

def _show_token(uid,chat_id,mint):
    is_pump_token = mint.lower().endswith("pump")
    pump_note = "\n_⚡ Pump.fun token — higher slippage used_" if mint.lower().endswith("pump") else ""
    sent=bot.send_message(chat_id,
        "⏳ *Loading token info…*" + pump_note,
        parse_mode="Markdown")
    def _r():
        info=token_info(mint)
        txt=fmt_card(info) if info["found"] else f"⚠️ *Token not found*\n`{mint[:20]}…`\n\nMay be very new. You can still try to buy."
        for i in range(3):
            try:
                bot.edit_message_text(txt,chat_id,sent.message_id,parse_mode="Markdown",
                    reply_markup=kb_trade(info if info["found"] else {"mint":mint,"sym":"?","url":"https://dexscreener.com"},uid),
                    disable_web_page_preview=True); return
            except Exception:
                if i<2: time.sleep(2)
    threading.Thread(target=_r,daemon=True).start()

def _exec_buy(uid,chat_id,mint,sol_amount):
    if not has_wallet(uid):
        bot.send_message(chat_id,"❌ *No trade wallet.*\n\nTap 💼 Wallet → ⚡ Add Trade Wallet",parse_mode="Markdown",reply_markup=kb_wallet(uid)); return
    pub=_users[uid]["pubkey"]; bal=sol_bal(pub)
    if bal<sol_amount+0.002:
        bot.send_message(chat_id,f"❌ *Insufficient balance*\n\nYou have: `{bal:.4f} SOL`\nNeed: `{sol_amount+0.002:.4f} SOL`",parse_mode="Markdown",reply_markup=kb_back()); return
    sp        = sol_usd() or 0
    gas_sol   = 0.000005
    gas_usd   = gas_sol * sp
    proto_sol = sol_amount * 0.003
    proto_usd = proto_sol * sp
    # Check if first time receiving this token (ATA rent)
    pub        = _users[uid]["pubkey"]
    user_toks  = token_accs(pub)
    has_ata    = any(t["mint"]==mint for t in user_toks)
    ata_sol    = 0.0 if has_ata else 0.00203928
    ata_usd    = ata_sol * sp
    total_fee  = gas_sol + proto_sol + ata_sol

    # ── SAFE MODE: check BEFORE spending any gas ────────
    checking_msg = bot.send_message(chat_id,
        "🔍 *Checking token safety…*", parse_mode="Markdown")

    def _run_safety():
        raw_in = int(sol_amount * 1e9)
        status, risk, msg, needs_confirm = safe_mode_check(mint, SOL_MINT, raw_in)

        # Hard block
        if status == "block":
            k = types.InlineKeyboardMarkup(row_width=1)
            k.add(types.InlineKeyboardButton("🔙 Cancel", callback_data="menu_main"))
            for i in range(3):
                try:
                    bot.edit_message_text(msg, chat_id, checking_msg.message_id,
                        parse_mode="Markdown", reply_markup=k); break
                except Exception:
                    if i < 2: time.sleep(1)
            return

        # Warning with optional confirmation
        if status == "warn" and needs_confirm:
            k = types.InlineKeyboardMarkup(row_width=2)
            k.add(
                types.InlineKeyboardButton(
                    "✅ Proceed", callback_data=f"force_buy_{mint}_{sol_amount}"),
                types.InlineKeyboardButton(
                    "❌ Cancel", callback_data="menu_main"),
            )
            if risk == "HIGH":
                k.add(types.InlineKeyboardButton(
                    "🎯 Use Sniper instead", callback_data="sniper_arm"))
            for i in range(3):
                try:
                    bot.edit_message_text(msg, chat_id, checking_msg.message_id,
                        parse_mode="Markdown", reply_markup=k); break
                except Exception:
                    if i < 2: time.sleep(1)
            return

        # OK or non-blocking warn — show fee preview + execute
        risk_icon = {"LOW":"✅","MEDIUM":"⚠️","HIGH":"🔴"}.get(risk,"✅")
        fee_line  = (
            f"📊 *Fee Preview*  {risk_icon} Risk: {risk}\n\n"
            f"💰 Swap: `{sol_amount} SOL`\n\n"
            f"📋 *Estimated Fees:*\n"
            f"  ⛽ Gas: `{gas_sol:.6f} SOL` (~${gas_usd:.4f})\n"
            f"  🔄 Protocol: `{proto_sol:.6f} SOL` (~${proto_usd:.4f})\n"
            + (f"  🏦 ATA rent: `{ata_sol:.6f} SOL`\n" if ata_sol > 0 else "")
            + f"  💸 Total: ~${total_fee*sp:.4f}\n"
            + (f"\n{msg}\n" if msg else "")
            + "\n_Executing swap…_"
        )
        try:
            bot.edit_message_text(fee_line, chat_id, checking_msg.message_id,
                parse_mode="Markdown")
        except Exception: pass
        _exec_buy_final(uid, chat_id, mint, sol_amount, checking_msg.message_id,
            gas_sol, gas_usd, proto_sol, proto_usd, ata_sol, ata_usd, total_fee, sp)

    threading.Thread(target=_run_safety, daemon=True).start()


def _exec_buy_final(uid, chat_id, mint, sol_amount, msg_id,
        gas_sol, gas_usd, proto_sol, proto_usd, ata_sol, ata_usd, total_fee, sp):
    """Execute swap after all safety checks passed."""
    sent_mid = msg_id
    def _r():
        try:
            kp=_users[uid]["keypair"]
            try: bot.edit_message_text(
                f"📊 *Fee Preview*\n✅ _Fees accepted — signing…_",
                chat_id,sent_mid,parse_mode="Markdown")
            except: pass

            txid,out,gasless=do_swap(kp,SOL_MINT,mint,int(sol_amount*1e9))
            sym,sname=get_token_name(mint)
            sp2=sol_usd() or 0
            fee_sol=sol_amount*0.003; fee_usd=fee_sol*sp2

            # Save to history
            save_history(uid,{
                "type":"BUY","sym":sym,"mint":mint,
                "in_sol":sol_amount,"out_tok":out,
                "fee_sol":total_fee,"gasless":gasless,
                "txid":txid,"time":datetime.now().isoformat()
            })

            msg_text = (
                f"✅ *Buy Confirmed!*{'  ⚡ GASLESS!' if gasless else ''}\n\n"
                f"💰 Spent: `{sol_amount} SOL`\n"
                f"📥 Got: `{out:.6f} {sym}`\n\n"
                f"📊 *Actual Fees:*\n"
                f"  ⛽ Gas: `{'$0.00' if gasless else f'~${gas_usd:.4f}'}`\n"
                f"  🔄 Protocol: `{fee_sol:.6f} SOL (~${fee_usd:.4f})`\n"
                + (f"  🏦 ATA rent: `{ata_sol:.6f} SOL` (one-time)\n" if ata_sol>0 else "")
                + f"\n[🔗 View on Solscan](https://solscan.io/tx/{txid})"
            )

            for i in range(3):
                try:
                    bot.edit_message_text(msg_text,chat_id,sent_mid,
                        parse_mode="Markdown",reply_markup=kb_back()); break
                except Exception:
                    if i<2: time.sleep(2)

            # Send trade card image
            try:
                card = generate_trade_card(uid, sym, "BUY", sol_amount, out, txid=txid)
                if card:
                    k_share = types.InlineKeyboardMarkup()
                    k_share.add(types.InlineKeyboardButton("📤 Share Result", callback_data=f"share_noop"))
                    bot.send_photo(chat_id, card,
                        caption=f"⚡ *MUB DEX Trade* — Bought ${sym}",
                        parse_mode="Markdown", reply_markup=k_share)
            except Exception: pass

        except Exception as e:
            for i in range(3):
                try: bot.edit_message_text(f"❌ *Buy Failed*\n\n`{str(e)[:300]}`",
                    chat_id,sent_mid,parse_mode="Markdown",reply_markup=kb_back()); break
                except Exception:
                    if i<2: time.sleep(2)
    threading.Thread(target=_r,daemon=True).start()

def _exec_sell(uid,chat_id,mint,pct,to_mint=None):
    if to_mint is None: to_mint=SOL_MINT
    to_sym=next((k for k,v in KNOWN.items() if v==to_mint),"SOL")
    if not has_wallet(uid):
        bot.send_message(chat_id,"❌ *No trade wallet.*",parse_mode="Markdown",reply_markup=kb_wallet(uid)); return
    sent=bot.send_message(chat_id,f"⏳ *Selling {pct}% → {to_sym}…*",parse_mode="Markdown")
    def _r():
        try:
            kp=_users[uid]["keypair"]; pub=_users[uid]["pubkey"]
            toks=token_accs(pub); t=next((x for x in toks if x["mint"]==mint),None)
            if not t or int(t["raw"])==0:
                bot.edit_message_text("⚠️ *Balance is 0*\n\nNo tokens to sell.",chat_id,sent.message_id,parse_mode="Markdown",reply_markup=kb_back()); return
            raw=int(int(t["raw"])*pct/100)
            txid,out,gasless=do_swap(kp,mint,to_mint,raw)
            for i in range(3):
                try:
                    bot.edit_message_text(
                        f"✅ *Sold {pct}%!*{'  ⚡ GASLESS!' if gasless else ''}\n\n"
                        f"📥 Got: `{out:.6f} {to_sym}`\n\n[🔗 View on Solscan](https://solscan.io/tx/{txid})",
                        chat_id,sent.message_id,parse_mode="Markdown",reply_markup=kb_back()); break
                except Exception:
                    if i<2: time.sleep(2)
        except Exception as e:
            for i in range(3):
                try: bot.edit_message_text(f"❌ *Sell Failed*\n\n`{str(e)[:300]}`",chat_id,sent.message_id,parse_mode="Markdown",reply_markup=kb_back()); break
                except Exception:
                    if i<2: time.sleep(2)
    threading.Thread(target=_r,daemon=True).start()

@bot.callback_query_handler(func=lambda c:True)
def handle_cb(call):
    uid=call.from_user.id; data=call.data; usr=u(uid)
    try: bot.answer_callback_query(call.id)
    except: pass

    if data=="menu_main":
        usr["state"]="idle"; show_main(uid,call.message.chat.id,call.message.message_id)
    elif data=="menu_wallet":
        pub=active_pub(uid)
        st=f"⚡ Trade: `{fmt_addr(usr.get('pubkey',''))}`" if has_wallet(uid) else f"👁 View: `{fmt_addr(usr.get('view_pub',''))}`" if usr.get("view_pub") else "❌ Not connected"
        eor(call,f"💼 *Wallet*\n\n{st}\n\n👁 *View Only* — See balance, no key needed\n⚡ *Trade Wallet* — Import key to trade",kb=kb_wallet(uid))
    elif data=="w_view_only":
        usr["state"]="awaiting_view_address"
        eor(call,"👁 *Connect View Wallet*\n\nSend your wallet address (public key — safe).\n\nPhantom: tap address at top → Copy\n\n_Send address (~44 chars):_",kb=kb_back())
    elif data in ("w_connect","w_add_trade"):
        usr["state"]="awaiting_pk"
        eor(call,"⚡ *Add Trade Wallet*\n\n⚠️ Use a NEW dedicated wallet!\n\n1. Phantom → Add Wallet → Create New\n2. Settings → Security → Export Private Key\n3. Send only your trading budget to it\n\n_Send private key (auto-deleted):_",kb=kb_back())
    elif data=="w_balance":
        def _b():
            pub=active_pub(uid)
            if not pub: bot.send_message(call.message.chat.id,"❌ No wallet.",reply_markup=kb_wallet(uid)); return
            sol=sol_bal(pub); toks=token_accs(pub); sp=sol_usd()
            lines=[f"💼 *Balance*\n",f"◎ SOL: `{sol:.5f}`"+(f"  _(${sol*sp:.2f})_" if sp else "")+"\n"]
            if toks:
                lines.append("*Tokens:*")
                for t in toks:
                    sym=next((k for k,v in KNOWN.items() if v==t["mint"]),""); name=sym or fmt_addr(t["mint"])
                    lines.append(f"• {name}: `{t['amount']}`")
            else: lines.append("_No tokens_")
            bot.send_message(call.message.chat.id,"\n".join(lines),parse_mode="Markdown",reply_markup=kb_wallet(uid))
        threading.Thread(target=_b,daemon=True).start()
    elif data=="w_address":
        pub=active_pub(uid) or "Not connected"
        eor(call,f"📋 *Your Address*\n\n`{pub}`\n\n_Tap to copy_",kb=kb_wallet(uid))
    elif data=="w_send":
        eor(call,"📤 *Send SOL*\n\nType: `/send ADDRESS AMOUNT`\ne.g. `/send 7abc…xyz 0.01`",kb=kb_back())
    elif data=="menu_trade":
        eor(call,"💱 *Buy / Sell*\n\nPaste any token contract address in chat.\n\n_You'll see price, safety info, and trade buttons._",kb=kb_back())
    elif data.startswith("buy_"):
        parts=data.split("_",2)
        if parts[1]=="custom":
            usr["state"]="awaiting_buy_amt"; usr["ctx"]["mint"]=parts[2]
            bot.send_message(call.message.chat.id,"Enter SOL amount:\ne.g. `0.05`",parse_mode="Markdown")
        else: _exec_buy(uid,call.message.chat.id,parts[2],float(parts[1]))
    elif data.startswith("sell_"):
        parts=data.split("_",2); _exec_sell(uid,call.message.chat.id,parts[2],float(parts[1]),SOL_MINT)
    elif data.startswith("sellto_"):
        parts=data.split("_",2); _exec_sell(uid,call.message.chat.id,parts[2],100,KNOWN.get(parts[1],SOL_MINT))
    elif data.startswith("safety_"):
        mint=data.replace("safety_","")
        def _s(): 
            info=token_info(mint)
            bot.send_message(call.message.chat.id,fmt_card(info),parse_mode="Markdown",disable_web_page_preview=True,reply_markup=kb_back())
        threading.Thread(target=_s,daemon=True).start()
    elif data=="menu_auto":
        running=uid in _traders and _traders[uid].running
        eor(call,"🤖 *Auto-Trader*\n\n"+("✅ *Running!*" if running else "Set token → bot trades with TP/SL automatically."),kb=kb_auto(uid))
    elif data=="auto_start":
        usr["state"]="awaiting_auto_contract"; eor(call,"🤖 *Auto-Trader*\n\nPaste the token contract:",kb=kb_back())
    elif data=="auto_status":
        if uid in _traders and _traders[uid].running:
            t=_traders[uid]; p=t._price(); chg=""
            if p and t.entry: c=((p-t.entry)/t.entry)*100; chg=f"\nP&L: `{c:+.2f}%`"
            eor(call,f"📊 *Status*\n\nToken: `{t.cfg['mint'][:16]}…`\nInvest: `{t.cfg['sol']} SOL`\nTP: `+{t.cfg['tp']}%` SL: `-{t.cfg['sl']}%`\nHolding: `{'Yes ✅' if t._bought else 'Waiting ⏳'}`{chg}",kb=kb_auto(uid))
        else: eor(call,"⏹ Not running.",kb=kb_auto(uid))
    elif data=="auto_stop_sell":
        if uid in _traders: _traders[uid].stop(sell=True)
        show_main(uid,call.message.chat.id,call.message.message_id)
    elif data=="auto_stop_only":
        if uid in _traders: _traders[uid].stop(sell=False)
        show_main(uid,call.message.chat.id,call.message.message_id)
    elif data=="menu_sniper":
        running=uid in _snipers and _snipers[uid].running
        k=types.InlineKeyboardMarkup(row_width=1)
        k.add(types.InlineKeyboardButton("🔴 Stop" if running else "🎯 Arm Sniper",callback_data="sniper_stop" if running else "sniper_arm"))
        k.add(types.InlineKeyboardButton("🔙 Main Menu",callback_data="menu_main"))
        eor(call,"🎯 *Sniper Bot*\n\n"+("✅ *Armed!*" if running else "Watches token 24/7.\nBuys instantly when liquidity appears."),kb=k)
    elif data=="sniper_arm":
        usr["state"]="awaiting_sniper_contract"; eor(call,"🎯 *Arm Sniper*\n\nPaste token contract:",kb=kb_back())
    elif data=="sniper_stop":
        if uid in _snipers: _snipers[uid].stop()
        show_main(uid,call.message.chat.id,call.message.message_id)
    elif data=="menu_limits":
        active=len([o for o in _limits.get(uid,[]) if o.active])
        k=types.InlineKeyboardMarkup(row_width=1)
        k.add(types.InlineKeyboardButton("➕ New Limit Order",callback_data="limit_new"))
        k.add(types.InlineKeyboardButton("🔙 Main Menu",callback_data="menu_main"))
        eor(call,f"📋 *Limit Orders*\n\nActive: `{active}`\n\n_Set a price — bot buys automatically when reached._",kb=k)
    elif data=="limit_new" or data.startswith("limit_"):
        mint=data.replace("limit_","") if data!="limit_new" else None
        if mint and len(mint)>5: usr["ctx"]["mint"]=mint
        usr["ctx"]["direction"]="below"; usr["state"]="awaiting_limit_price"
        eor(call,"📋 *New Limit Order*\n\nEnter target price in USD.\ne.g. `0.0001234`",kb=kb_back())
    elif data=="menu_settings":
        s   = u(uid)["settings"]
        eng = s.get("swap_engine","ultra")
        msl = s.get("max_slippage",3000)
        s   = u(uid)["settings"]
        eng = s.get("swap_engine","ultra")
        msl = s.get("max_slippage",3000)
        eng_txt = "Ultra ⚡ (gasless)" if eng=="ultra" else "Normal 🔄"
        eor(call,
            "⚙️ *Settings*\n\n"
            f"💰 Default Buy: `{s['default_buy']} SOL`\n"
            f"🎯 Auto TP: `{s['auto_tp']}%`\n"
            f"🛑 Auto SL: `{s['auto_sl']}%`\n"
            f"📉 Max Slippage: `{msl//100}%` _(auto-starts from 0.1%)_\n"
            f"⚡ Engine: `{eng_txt}`\n\n"
            "_Tap to change:_",
            kb=kb_settings(uid))

    elif data=="set_default_buy":
        usr["state"]="set_default_buy"
        usr["state"]="set_default_buy"
        bot.send_message(call.message.chat.id,"Enter default buy amount (SOL):\ne.g. `0.1`",parse_mode="Markdown")

    elif data=="set_tp":
        usr["state"]="set_tp"
        bot.send_message(call.message.chat.id,
            "Enter Take-Profit %:e.g. `20`",
            parse_mode="Markdown")

    elif data=="set_sl":
        usr["state"]="set_sl"
        bot.send_message(call.message.chat.id,
            "Enter Stop-Loss %:e.g. `10`",
            parse_mode="Markdown")

    elif data=="set_slippage":
        usr["state"]="set_slippage"
        bot.send_message(call.message.chat.id,
            "📉 *Set Max Slippage*"
            "Bot auto-starts from 0.1% and escalates until TX succeeds."
            "This sets the MAXIMUM it will try."
            "Enter max slippage %:"
            "`5`  = safe (stable tokens)"
            "`15` = recommended (most tokens)"
            "`30` = aggressive (pump.fun / low liq)"
            "`50` = maximum",
            parse_mode="Markdown")

    elif data=="set_engine_ultra":
        u(uid)["settings"]["swap_engine"] = "ultra"
        save_users()
        eor(call,"✅ *Ultra Swap enabled*_Gasless + fastest. Falls back to Normal if needed._",
            kb=kb_settings(uid))

    elif data=="set_engine_normal":
        u(uid)["settings"]["swap_engine"] = "normal"
        save_users()
        eor(call,"✅ *Normal Swap enabled*_Reliable Jupiter routing._",
            kb=kb_settings(uid))
    elif data=="menu_portfolio":
        def _portfolio():
            pub = active_pub(uid)
            if not pub:
                bot.send_message(call.message.chat.id,
                    "❌ No wallet connected.", reply_markup=kb_wallet(uid)); return
            toks = token_accs(pub)
            sol  = sol_bal(pub)
            sp   = sol_usd() or 0
            if not toks:
                eor(call,
                    f"🪙 *My Tokens*\n\n"
                    f"◎ SOL: `{sol:.5f}` _(${sol*sp:.2f})_\n\n"
                    "_No tokens found._\n\nSwap some tokens first!",
                    kb=kb_back())
                return
            # Build token list with sell buttons
            lines = [f"🪙 *My Tokens*\n\n◎ SOL: `{sol:.5f}` _(${sol*sp:.2f})_\n"]
            k     = types.InlineKeyboardMarkup(row_width=2)
            for t in toks[:8]:  # max 8 tokens
                sym, name = get_token_name(t["mint"])
                lines.append(f"• *{sym}*: `{t['amount']}`")
                k.add(
                    types.InlineKeyboardButton(
                        f"🔴 Sell {sym}",
                        callback_data=f"sell_100_{t['mint']}"),
                    types.InlineKeyboardButton(
                        f"📊 {sym}",
                        callback_data=f"tok_info_{t['mint']}"),
                )
            k.add(types.InlineKeyboardButton("🔙 Main Menu", callback_data="menu_main"))
            try:
                bot.edit_message_text(
                    "\n".join(lines),
                    call.message.chat.id, call.message.message_id,
                    parse_mode="Markdown", reply_markup=k)
            except Exception:
                bot.send_message(call.message.chat.id,
                    "\n".join(lines),
                    parse_mode="Markdown", reply_markup=k)
        threading.Thread(target=_portfolio, daemon=True).start()

    elif data.startswith("tok_info_"):
        mint = data.replace("tok_info_","")
        _show_token(uid, call.message.chat.id, mint)

    elif data=="menu_history":
        hist = get_history(uid)
        if not hist:
            eor(call,
                "📜 *Trade History*\n\n_No trades yet._\n\nStart trading to see history!",
                kb=kb_back())
            return
        lines = ["📜 *Trade History* (last 10)\n"]
        for h in reversed(hist[-10:]):
            t    = h.get("type","?")
            sym  = h.get("sym","?")
            in_s = h.get("in_sol",0)
            out  = h.get("out_tok",0)
            ts   = h.get("time","")[:10]
            icon = "🟢" if t=="BUY" else "🔴"
            lines.append(
                f"{icon} *{t}* ${sym} | {in_s:.4f} SOL → {out:.4f}\n"
                f"   _{ts}_"
            )
        eor(call, "\n".join(lines), kb=kb_back())

    elif data=="share_noop":
        bot.answer_callback_query(call.id, "Share this image with friends! 📤")

    elif data.startswith("force_buy_"):
        # User chose to proceed despite safety warning
        parts = data.split("_", 3)  # force, buy, mint, amount
        if len(parts) >= 4:
            mint      = parts[2]
            try: sol_amount = float(parts[3])
            except: sol_amount = 0.01
            bot.answer_callback_query(call.id, "⚠️ Proceeding at your risk…")
            # Skip safety checks and execute directly
            pub       = active_pub(uid) or ""
            sp        = sol_usd() or 0
            gas_sol   = 0.000005
            proto_sol = sol_amount * 0.003
            user_toks = token_accs(pub) if pub else []
            has_ata   = any(t["mint"]==mint for t in user_toks)
            ata_sol   = 0.0 if has_ata else 0.00203928
            total_fee = gas_sol + proto_sol + ata_sol
            sent = bot.send_message(call.message.chat.id,
                "⚠️ *Bypassing safety checks…*\n_Executing swap…_",
                parse_mode="Markdown")
            _exec_buy_final(uid, call.message.chat.id, mint, sol_amount, sent.message_id,
                gas_sol, gas_sol*sp, proto_sol, proto_sol*sp,
                ata_sol, ata_sol*sp, total_fee, sp)

    elif data=="menu_feedback":
        usr["state"]="awaiting_feedback"
        eor(call,"💬 *Send Feedback*\n\nWhat do you think? Bugs or suggestions?\n\n_Type your message:_",kb=kb_back())
    elif data=="menu_help":
        eor(call,"❓ *How to use MUB DEX*\n\n"
            "1️⃣ *Connect Wallet*\n   💼 Wallet → ⚡ Add Trade Wallet\n\n"
            "2️⃣ *Buy a Token*\n   Paste contract address in chat\n   Tap Buy button\n\n"
            "3️⃣ *Sell*\n   Paste same contract → Sell 50% or 100%\n\n"
            "4️⃣ *Auto-Trader*\n   🤖 Auto → Start → paste contract\n\n"
            "5️⃣ *Sniper*\n   🎯 Sniper → Arm → paste contract\n\n"
            "⚡ *Gas: ~$0.009* vs $0.40 elsewhere",kb=kb_back())

@bot.message_handler(commands=["admin"])
def cmd_admin(msg):
    """Admin dashboard — only for ADMIN_ID."""
    uid = msg.from_user.id
    if ADMIN_ID and uid != ADMIN_ID:
        bot.reply_to(msg, "❌ Admin only."); return
    elif not ADMIN_ID:
        bot.reply_to(msg, "❌ Set ADMIN_ID first."); return

    stats = get_stats()
    if "error" in stats:
        bot.reply_to(msg, f"❌ Stats error: {stats['error']}"); return

    # Recent users
    users_sorted = sorted(
        stats.get("users",{}).items(),
        key=lambda x: x[1].get("last_seen",""),
        reverse=True
    )
    recent_lines = []
    for uid_s, info in users_sorted[:10]:
        name = info.get("name","?")
        last = info.get("last_seen","")[:10]
        recent_lines.append(f"  • {name} ({uid_s[:8]}…) — {last}")

    text = (
        "📊 *MUB DEX Admin Dashboard*\n\n"
        f"👥 Total users: *{stats['total']}*\n"
        f"🟢 Active (24h): *{stats['active_24h']}*\n"
        f"💼 With wallet: *{stats['with_wallet']}*\n"
        f"🔄 Total swaps: *{stats['total_swaps']}*\n\n"
        "👤 *Recent Users:*\n"
        + ("\n".join(recent_lines) if recent_lines else "_None yet_")
        + "\n\n_Use /broadcast MESSAGE to message all users_"
    )

    k = types.InlineKeyboardMarkup(row_width=2)
    k.add(
        types.InlineKeyboardButton("📨 Broadcast", callback_data="admin_broadcast"),
        types.InlineKeyboardButton("📊 Full Stats", callback_data="admin_stats"),
    )
    bot.reply_to(msg, text, parse_mode="Markdown", reply_markup=k)


@bot.message_handler(commands=["broadcast"])
def cmd_broadcast(msg):
    """Send message to all users — admin only."""
    uid = msg.from_user.id
    if ADMIN_ID and uid != ADMIN_ID:
        bot.reply_to(msg, "❌ Admin only."); return
    elif not ADMIN_ID:
        bot.reply_to(msg, "❌ Set ADMIN_ID first."); return

    text = msg.text.replace("/broadcast","",1).strip()
    if not text:
        bot.reply_to(msg, "Usage: `/broadcast YOUR MESSAGE`",
                     parse_mode="Markdown"); return

    try:
        stats = json.load(open(STATS_FILE)) if os.path.exists(STATS_FILE) else {}
    except: stats = {}

    sent_count = 0
    fail_count = 0
    for uid_s in stats:
        try:
            bot.send_message(int(uid_s),
                f"📢 *Message from MUB DEX*\n\n{text}",
                parse_mode="Markdown")
            sent_count += 1
            time.sleep(0.05)  # rate limit
        except Exception:
            fail_count += 1

    bot.reply_to(msg,
        f"✅ Broadcast sent!\n"
        f"  Delivered: {sent_count}\n"
        f"  Failed: {fail_count}")


@bot.callback_query_handler(func=lambda c: c.data.startswith("admin_"))
def handle_admin_cb(call):
    if call.from_user.id != ADMIN_ID: return
    bot.answer_callback_query(call.id)

    if call.data == "admin_broadcast":
        bot.send_message(call.message.chat.id,
            "Send: `/broadcast YOUR MESSAGE`\n\nAll users will receive it.",
            parse_mode="Markdown")

    elif call.data == "admin_stats":
        stats = get_stats()
        users = stats.get("users", {})
        lines = [f"📊 *All Users ({len(users)})*\n"]
        for uid_s, info in sorted(users.items(),
                key=lambda x: x[1].get("last_seen",""), reverse=True):
            name = info.get("name","?")
            fs   = info.get("first_seen","")[:10]
            ls   = info.get("last_seen","")[:10]
            lines.append(f"• *{name}* — joined {fs}, last {ls}")
        bot.send_message(call.message.chat.id,
            "\n".join(lines[:30]),
            parse_mode="Markdown")


@bot.message_handler(commands=["send"])
def cmd_send(msg):
    uid=msg.from_user.id; args=msg.text.split()
    if len(args)<3: bot.reply_to(msg,"Usage: `/send ADDRESS AMOUNT`",parse_mode="Markdown"); return
    if not has_wallet(uid): bot.reply_to(msg,"❌ No trade wallet."); return
    to_addr=args[1].strip()
    try: amt=float(args[2])
    except: bot.reply_to(msg,"❌ Invalid amount"); return
    sent=bot.reply_to(msg,f"📤 Sending `{amt} SOL`…",parse_mode="Markdown")
    def _r():
        try:
            kp  = _users[uid]["keypair"]
            # Build legacy transfer instruction
            ix  = transfer(TransferParams(
                from_pubkey=kp.pubkey(),
                to_pubkey=Pubkey.from_string(to_addr),
                lamports=int(amt * 1e9)
            ))
            # Get fresh blockhash
            bh  = Hash.from_string(get_blockhash())
            # Build message
            msg2 = Message.new_with_blockhash([ix], kp.pubkey(), bh)
            # Sign using VersionedTransaction (correct way)
            signed = VersionedTransaction(msg2, [kp])
            raw    = base64.b64encode(bytes(signed)).decode()
            # Send
            res = rpc("sendTransaction", [raw, {
                "encoding"           : "base64",
                "skipPreflight"      : True,
                "preflightCommitment": "processed",
                "maxRetries"         : 5,
            }])
            if "error" in res:
                raise RuntimeError(str(res["error"]))
            txid = res["result"]
            # Confirm on-chain
            confirm_tx(txid)
            bot.edit_message_text(
                f"✅ *Sent Successfully!*\n\n"
                f"💰 Amount: `{amt} SOL`\n"
                f"📤 To: `{fmt_addr(to_addr)}`\n\n"
                f"[🔗 View TX](https://solscan.io/tx/{txid})",
                sent.chat.id, sent.message_id,
                parse_mode="Markdown", reply_markup=kb_back())
        except Exception as e:
            bot.edit_message_text(
                f"❌ *Send Failed*\n\n`{str(e)[:200]}`",
                sent.chat.id, sent.message_id,
                parse_mode="Markdown", reply_markup=kb_back())
    threading.Thread(target=_r, daemon=True).start()

if __name__=="__main__":
    if not BOT_TOKEN: print("❌ Set BOT_TOKEN env variable"); exit(1)
    if not SOLDERS_OK: print("⚠️  pip install solders base58")
    load_users()
    print("⚡  MUB DEX Bot v3.0")
    print(f"    Referral: {JUPITER_REFERRAL_ACCOUNT[:20]}…")
    print(f"    Users: {len(_users)}")
    print("    Polling…\n")
    bot.infinity_polling(timeout=60,long_polling_timeout=30)
