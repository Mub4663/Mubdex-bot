# ⚡ MUB DEX Bot — Deployment Guide

## WHY LOCAL FAILS
Your ISP (MTN/Airtel Nigeria) blocks `api.telegram.org`.
Solution: Run bot on a FREE cloud server outside Nigeria.

---

## ✅ OPTION A — Render.com (FREE, Easiest) — RECOMMENDED

### Steps:

**1. Create GitHub account**
   - Go to github.com → Sign up free

**2. Upload bot files to GitHub**
   - New repository → name: `mubdex-bot`
   - Upload these files:
     - `mubdex_bot.py`
     - `requirements.txt`
     - `render.yaml`

**3. Create Render account**
   - Go to render.com → Sign up with GitHub

**4. Deploy**
   - New → Blueprint
   - Connect your GitHub repo
   - Render auto-detects render.yaml
   - Add environment variable:
     - Key: `BOT_TOKEN`
     - Value: your token from @BotFather

**5. Done!**
   - Bot runs 24/7 on Render servers
   - No blocking — US/EU servers
   - Free tier: enough for personal use

**Cost: FREE** (Render free tier = 750hrs/month)

---

## ✅ OPTION B — Railway.app (FREE, Also Easy)

1. Go to railway.app → Sign up with GitHub
2. New Project → Deploy from GitHub repo
3. Add variable: `BOT_TOKEN = your_token`
4. Deploy → done!

**Cost: FREE** ($5 credit/month on free tier)

---

## ✅ OPTION C — Local with Proxy (If you have VPN)

Edit `mubdex_bot.py` line 38:

```python
# If using VPN/proxy on your PC:
PROXY_URL = "socks5://127.0.0.1:1080"  # Tor/SOCKS proxy
# or
PROXY_URL = "http://127.0.0.1:8080"    # HTTP proxy
```

Then run normally: `python mubdex_bot.py`

---

## UPDATE BOT TOKEN (important!)

Open `mubdex_bot.py` → line 34 → replace:
```python
BOT_TOKEN = "PASTE_YOUR_BOT_TOKEN_HERE"
```
with your actual token from @BotFather.

On Render/Railway: set as environment variable instead
(more secure — token not in code).

---

## READ BOT_TOKEN FROM ENVIRONMENT (recommended for cloud)

The bot already supports this. On Render/Railway:
- Set env var: `BOT_TOKEN = 123456:ABCdef...`
- Bot reads it automatically at startup

---

## CHECKING IF BOT IS RUNNING

Send `/start` to your bot on Telegram.
If it replies → working! ✅
If no reply after 30s → check Render/Railway logs.

---

*MUB DEX — Built by Mubarak*
