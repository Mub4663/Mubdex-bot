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
RPC_URL    = "https://api.mainnet-beta.solana.com"

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
def _default_settings(): return {"default_buy":0.1,"auto_tp":20,"auto_sl":10}

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
    json.dump(out,open(DATA_FILE,"w"))

def load_users():
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

def rpc(method,params):
    r=requests.post(RPC_URL,json={"jsonrpc":"2.0","id":1,"method":method,"params":params},timeout=15)
    return r.json()

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

def sign_and_send(tx_bytes, kp):
    for attempt in range(3):
        try:
            raw_tx = VersionedTransaction.from_bytes(tx_bytes)
            signed = VersionedTransaction(raw_tx.message, [kp])
            raw_b64 = base64.b64encode(bytes(signed)).decode()
            result = rpc("sendTransaction",[raw_b64,{
                "encoding":"base64","skipPreflight":True,
                "preflightCommitment":"processed","maxRetries":5}])
            if "error" in result: raise RuntimeError(f"RPC: {result['error']}")
            return result["result"]
        except RuntimeError: raise
        except Exception as e:
            if attempt<2: time.sleep(1); continue
            raise RuntimeError(f"Sign failed: {e}")

def confirm_tx(txid, max_wait=45):
    deadline=time.time()+max_wait
    while time.time()<deadline:
        time.sleep(3)
        try:
            res=rpc("getSignatureStatuses",[[txid],{"searchTransactionHistory":True}])
            val=res["result"]["value"][0]
            if val is None: continue
            if val.get("err"): raise RuntimeError(f"TX failed: {val['err']}")
            if val.get("confirmationStatus") in ("confirmed","finalized"): return True
        except RuntimeError: raise
        except: pass
    raise RuntimeError("Confirmation timeout — check Solscan")

SLIPPAGE_STEPS = [50, 100, 200, 300, 500]

def do_swap(kp, fm, tm, raw_in):
    pub = str(kp.pubkey())

    # Try Ultra first
    try:
        params={"inputMint":fm,"outputMint":tm,"amount":raw_in,"taker":pub}
        if JUPITER_REFERRAL_ACCOUNT:
            params["referralAccount"]=JUPITER_REFERRAL_ACCOUNT
            params["referralFeeBps"]=JUPITER_FEE_BPS
        r=requests.get(ULTRA_Q,params=params,timeout=10)
        if r.status_code==200:
            order=r.json()
            if "transaction" in order and "error" not in order:
                out_amt=int(order.get("outAmount",0))
                if out_amt==0: raise RuntimeError("No liquidity")
                txid=sign_and_send(base64.b64decode(order["transaction"]),kp)
                confirm_tx(txid)
                out_dec=9 if tm==SOL_MINT else tok_dec(tm)
                return txid, out_amt/(10**out_dec), order.get("gasless",False)
    except RuntimeError as e:
        if "No liquidity" in str(e): raise
    except: pass

    # Jupiter Normal — fresh quote each step
    last_err="No route"
    for slip in SLIPPAGE_STEPS:
        for pair in JUPITER_PAIRS:
            try:
                qr=requests.get(pair["q"],params={
                    "inputMint":fm,"outputMint":tm,
                    "amount":raw_in,"slippageBps":slip},timeout=10)
                if qr.status_code!=200: continue
                q=qr.json()
                if "error" in q or "errorCode" in q:
                    last_err=str(q.get("error",q.get("errorCode",""))); continue
                if not q.get("outAmount") or int(q["outAmount"])==0:
                    raise RuntimeError("No liquidity for this token")
                payload={
                    "quoteResponse":q,"userPublicKey":pub,
                    "wrapAndUnwrapSol":True,"prioritizationFeeLamports":PRIORITY_FEE,
                    "dynamicComputeUnitLimit":True,"skipUserAccountsCheck":True,
                }
                if JUPITER_REFERRAL_ACCOUNT: payload["feeAccount"]=JUPITER_REFERRAL_ACCOUNT
                sr=requests.post(pair["s"],json=payload,timeout=20)
                if sr.status_code!=200: continue
                sd=sr.json()
                if "swapTransaction" not in sd: last_err="No swapTransaction"; continue
                txid=sign_and_send(base64.b64decode(sd["swapTransaction"]),kp)
                confirm_tx(txid)
                out_raw=int(q.get("outAmount",0))
                out_dec=9 if tm==SOL_MINT else tok_dec(tm)
                return txid, out_raw/(10**out_dec), False
            except RuntimeError as e:
                es=str(e)
                if "No liquidity" in es: raise
                if "Custom" in es and "1}" in es:
                    last_err=f"Slippage {slip/100:.1f}% low"; break
                last_err=es; continue
            except Exception as e:
                last_err=str(e)[:80]; continue

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
        if res["liq"]==0: w.append("🚨 Zero liquidity — possible rug!")
        elif res["liq"]<1000: w.append("🚨 Very low liquidity")
        elif res["liq"]<5000: w.append("⚠️ Low liquidity")
        if res["sells24"]==0 and res["buys24"]>10: w.append("⚠️ No sells — possible honeypot!")
        res["warnings"]=w
        res["risk"]="HIGH" if any("🚨" in x for x in w) else "MEDIUM" if w else "LOW"
    except: pass
    return res

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
    k.add(types.InlineKeyboardButton("💼 Wallet",callback_data="menu_wallet"),
          types.InlineKeyboardButton("💱 Buy / Sell",callback_data="menu_trade"))
    k.add(types.InlineKeyboardButton("🤖 Auto-Trader",callback_data="menu_auto"),
          types.InlineKeyboardButton("🎯 Sniper Bot",callback_data="menu_sniper"))
    k.add(types.InlineKeyboardButton("📋 Limit Orders",callback_data="menu_limits"),
          types.InlineKeyboardButton("⚙️ Settings",callback_data="menu_settings"))
    k.add(types.InlineKeyboardButton("💬 Feedback",callback_data="menu_feedback"),
          types.InlineKeyboardButton("❓ Help",callback_data="menu_help"))
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
    s=u(uid)["settings"]
    k=types.InlineKeyboardMarkup(row_width=2)
    k.add(types.InlineKeyboardButton(f"💰 Buy: {s['default_buy']} SOL",callback_data="set_default_buy"),
          types.InlineKeyboardButton(f"🎯 TP: {s['auto_tp']}%",callback_data="set_tp"))
    k.add(types.InlineKeyboardButton(f"🛑 SL: {s['auto_sl']}%",callback_data="set_sl"))
    k.add(types.InlineKeyboardButton("🔙 Main Menu",callback_data="menu_main"))
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

    if state in ("set_default_buy","set_tp","set_sl"):
        try:
            val=float(text); key={"set_default_buy":"default_buy","set_tp":"auto_tp","set_sl":"auto_sl"}[state]
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
    sent=bot.send_message(chat_id,"⏳ *Loading token info…*",parse_mode="Markdown")
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
    sent=bot.send_message(chat_id,f"⏳ *Buying {sol_amount} SOL…*\n_Getting fresh quote…_",parse_mode="Markdown")
    def _r():
        try:
            kp=_users[uid]["keypair"]
            try: bot.edit_message_text(f"⏳ *Buying {sol_amount} SOL…*\n_Signing transaction…_",chat_id,sent.message_id,parse_mode="Markdown")
            except: pass
            txid,out,gasless=do_swap(kp,SOL_MINT,mint,int(sol_amount*1e9))
            sym=token_info(mint).get("sym","?"); fee_sol=sol_amount*0.003; fee_usd=fee_sol*(sol_usd() or 0)
            for i in range(3):
                try:
                    bot.edit_message_text(
                        f"✅ *Buy Confirmed!*{'  ⚡ GASLESS!' if gasless else ''}\n\n"
                        f"💰 Spent: `{sol_amount} SOL`\n📥 Got: `{out:.6f} {sym}`\n\n"
                        f"📊 *Fee Breakdown:*\n"
                        f"  Gas: `{'$0.00 (gasless)' if gasless else '~$0.009'}`\n"
                        f"  Protocol: `{fee_sol:.5f} SOL (~${fee_usd:.3f})`\n\n"
                        f"[🔗 View on Solscan](https://solscan.io/tx/{txid})",
                        chat_id,sent.message_id,parse_mode="Markdown",reply_markup=kb_back()); break
                except Exception:
                    if i<2: time.sleep(2)
        except Exception as e:
            for i in range(3):
                try: bot.edit_message_text(f"❌ *Buy Failed*\n\n`{str(e)[:300]}`",chat_id,sent.message_id,parse_mode="Markdown",reply_markup=kb_back()); break
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
        eor(call,"⚙️ *Settings*\n\nTap to change:",kb=kb_settings(uid))
    elif data=="set_default_buy":
        usr["state"]="set_default_buy"; bot.send_message(call.message.chat.id,"Enter default buy amount (SOL):\ne.g. `0.1`",parse_mode="Markdown")
    elif data=="set_tp":
        usr["state"]="set_tp"; bot.send_message(call.message.chat.id,"Enter Take-Profit %:\ne.g. `20`",parse_mode="Markdown")
    elif data=="set_sl":
        usr["state"]="set_sl"; bot.send_message(call.message.chat.id,"Enter Stop-Loss %:\ne.g. `10`",parse_mode="Markdown")
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
            kp=_users[uid]["keypair"]
            ix=transfer(TransferParams(from_pubkey=kp.pubkey(),to_pubkey=Pubkey.from_string(to_addr),lamports=int(amt*1e9)))
            bh=Hash.from_string(get_blockhash()); msg2=Message.new_with_blockhash([ix],kp.pubkey(),bh)
            signed=VersionedTransaction(msg2,[kp]); raw=base64.b64encode(bytes(signed)).decode()
            res=rpc("sendTransaction",[raw,{"encoding":"base64","skipPreflight":True,"preflightCommitment":"processed","maxRetries":5}])
            if "error" in res: raise RuntimeError(str(res["error"]))
            txid=res["result"]; confirm_tx(txid)
            bot.edit_message_text(f"✅ *Sent!*\n\nAmount: `{amt} SOL`\nTo: `{fmt_addr(to_addr)}`\n\n[🔗 View TX](https://solscan.io/tx/{txid})",sent.chat.id,sent.message_id,parse_mode="Markdown",reply_markup=kb_back())
        except Exception as e:
            bot.edit_message_text(f"❌ Send failed: `{e}`",sent.chat.id,sent.message_id,parse_mode="Markdown")
    threading.Thread(target=_r,daemon=True).start()

if __name__=="__main__":
    if not BOT_TOKEN: print("❌ Set BOT_TOKEN env variable"); exit(1)
    if not SOLDERS_OK: print("⚠️  pip install solders base58")
    load_users()
    print("⚡  MUB DEX Bot v3.0")
    print(f"    Referral: {JUPITER_REFERRAL_ACCOUNT[:20]}…")
    print(f"    Users: {len(_users)}")
    print("    Polling…\n")
    bot.infinity_polling(timeout=60,long_polling_timeout=30)
