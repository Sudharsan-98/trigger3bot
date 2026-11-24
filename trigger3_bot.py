# Trigger-3 v1.2 live signal bot (NIFTY 50, 15-min) -> Telegram
# Data: Angel One SmartAPI (free). Does NOT place any orders.
import configparser, os, sys, time, datetime as dt, traceback
import requests, pyotp
import pandas as pd, numpy as np

HERE = os.path.dirname(os.path.abspath(__file__))
CFG_PATH = os.path.join(HERE, "config.txt")
IST = dt.timezone(dt.timedelta(hours=5, minutes=30))
NIFTY_TOKEN, NIFTY_SYM = "99926000", "Nifty 50"

# ---------------- config ----------------
cfg = configparser.ConfigParser()
cfg.read(CFG_PATH, encoding="utf-8")
C = cfg["settings"] if cfg.has_section("settings") else {}
def opt(env, key, default=""):
    v = os.environ.get(env, "").strip()
    return v if v else str(C.get(key, default)).strip()
API_KEY, CLIENT, PIN, TOTP = opt("API_KEY", "api_key"), opt("CLIENT_ID", "client_id"), opt("PIN", "pin"), opt("TOTP_SECRET", "totp_secret")
TG_TOKEN, TG_CHAT = opt("TELEGRAM_TOKEN", "telegram_token"), opt("TELEGRAM_CHAT_ID", "telegram_chat_id")
SL = float(opt("SL_POINTS", "sl_points", "65"))
TEST = os.environ.get("TEST", "").strip().lower() == "yes"
SCHED = os.environ.get("SCHED", "").strip()
# cloud runs in 2 parts: part 1 till 12:40, part 2 from 12:40 (GitHub max job time)
RESP_START, END_HHMM = (1240, 1535) if SCHED.startswith("47 6") else ((0, 1240) if SCHED else (0, 1535))
START_HHMM, LAST_HHMM = 930, 1300

def now():
    return dt.datetime.now(IST)

def log(msg):
    line = f"{now():%H:%M:%S}  {msg}"
    try:
        print(line, flush=True)
    except Exception:
        print(line.encode("ascii", "ignore").decode(), flush=True)
    with open(os.path.join(HERE, "bot_log.txt"), "a", encoding="utf-8") as f:
        f.write(f"{now():%Y-%m-%d} {line}\n")

# ---------------- telegram ----------------
def tg_chat_id():
    global TG_CHAT
    if TG_CHAT:
        return TG_CHAT
    r = requests.get(f"https://api.telegram.org/bot{TG_TOKEN}/getUpdates", timeout=20).json()
    for u in reversed(r.get("result", [])):
        m = u.get("message") or u.get("channel_post")
        if m:
            TG_CHAT = str(m["chat"]["id"])
            if cfg.has_section("settings"):
                cfg["settings"]["telegram_chat_id"] = TG_CHAT
                with open(CFG_PATH, "w", encoding="utf-8") as f:
                    cfg.write(f)
            return TG_CHAT
    sys.exit("Telegram chat id kidaikkala: unga bot-ku Telegram-la 'hi' anuppitu marupadi start pannunga.")

def tg(msg):
    log("TG: " + msg.replace("\n", " | "))
    for _ in range(3):
        try:
            requests.post(f"https://api.telegram.org/bot{TG_TOKEN}/sendMessage",
                          data={"chat_id": tg_chat_id(), "text": msg}, timeout=20)
            return
        except Exception as e:
            log(f"telegram error {e}")
            time.sleep(3)

# ---------------- angel ----------------
def angel_login():
    from SmartApi import SmartConnect
    obj = SmartConnect(api_key=API_KEY)
    d = obj.generateSession(CLIENT, PIN, pyotp.TOTP(TOTP).now())
    if not d or not d.get("status"):
        raise RuntimeError(f"Angel login failed: {d}")
    return obj

def candles(obj, interval, days):
    end = now()
    start = end - dt.timedelta(days=days)
    p = {"exchange": "NSE", "symboltoken": NIFTY_TOKEN, "interval": interval,
         "fromdate": start.strftime("%Y-%m-%d 09:00"), "todate": end.strftime("%Y-%m-%d %H:%M")}
    for _ in range(3):
        r = obj.getCandleData(p)
        if r and r.get("status") and r.get("data"):
            df = pd.DataFrame(r["data"], columns=["t", "o", "h", "l", "c", "v"])
            df["t"] = pd.to_datetime(df["t"]).dt.tz_convert(IST)
            return df.drop(columns="v")
        time.sleep(2)
    raise RuntimeError(f"candle fetch failed: {r}")

def ltp(obj):
    try:
        r = obj.ltpData("NSE", NIFTY_SYM, NIFTY_TOKEN)
        return float(r["data"]["ltp"])
    except Exception:
        return None

# ---------------- Trigger-3 logic (same rules as the Pine script) ----------------
def features(m15, daily):
    d = m15.copy().reset_index(drop=True)
    d["date"] = d.t.dt.date
    d["ct"] = d.t + pd.Timedelta(minutes=15)                      # candle close time
    d["hhmm"] = d.ct.dt.hour * 100 + d.ct.dt.minute
    d["mins"] = d.ct.dt.hour * 60 + d.ct.dt.minute - 555
    d["k"] = d.mins // 15 - 1
    # Supertrend(3,10) - TradingView formula
    h, l, c = d.h.values, d.l.values, d.c.values
    n = len(d)
    tr = np.empty(n); tr[0] = h[0] - l[0]
    tr[1:] = np.maximum(h[1:] - l[1:], np.maximum(abs(h[1:] - c[:-1]), abs(l[1:] - c[:-1])))
    atr = np.full(n, np.nan); atr[9] = tr[:10].mean()
    for i in range(10, n):
        atr[i] = (atr[i - 1] * 9 + tr[i]) / 10
    hl2 = (h + l) / 2
    up, lo = hl2 + 3 * atr, hl2 - 3 * atr
    dirn = np.ones(n); st = np.full(n, np.nan)
    for i in range(n):
        if i > 0 and not np.isnan(atr[i - 1]):
            pu, pl = up[i - 1], lo[i - 1]
            lo[i] = lo[i] if (lo[i] > pl or c[i - 1] < pl) else pl
            up[i] = up[i] if (up[i] < pu or c[i - 1] > pu) else pu
            dirn[i] = (-1 if c[i] > up[i] else 1) if st[i - 1] == pu else (1 if c[i] < lo[i] else -1)
        else:
            dirn[i] = 1
        st[i] = lo[i] if dirn[i] == -1 else up[i]
    prev = np.r_[1, dirn[:-1]]
    d["stUp"] = (dirn < 0) & (prev > 0)
    d["stDn"] = (dirn > 0) & (prev < 0)
    # previous day H/L/C from daily candles
    dd = daily.copy(); dd["date"] = dd.t.dt.date
    dd = dd.set_index("date")[["h", "l", "c"]].sort_index()
    prevmap = dd.shift(1)
    d = d.join(prevmap.rename(columns={"h": "pH", "l": "pL", "c": "pC"}), on="date")
    pp = (d.pH + d.pL + d.pC) / 3; bc0 = (d.pH + d.pL) / 2; tc0 = 2 * pp - bc0
    d["TC"] = np.maximum(tc0, bc0); d["BC"] = np.minimum(tc0, bc0)
    pc = d.c.shift(1)
    d["cprUp"] = (d.c > d.TC) & (pc <= d.TC)
    d["cprDn"] = (d.c < d.BC) & (pc >= d.BC)
    # noise band (14-day) + intraday average price
    d["dOpen"] = d.groupby("date").o.transform("first")
    d["mv"] = (d.c / d.dOpen - 1).abs()
    piv = d.pivot_table(index="date", columns="k", values="mv")
    sig = piv.shift(1).rolling(13, min_periods=10).mean()
    d = d.join(sig.stack().rename("sigma"), on=["date", "k"])
    d["vw"] = ((d.h + d.l + d.c) / 3).groupby(d.date).transform(lambda x: x.expanding().mean())
    d["UB"] = np.maximum(d.dOpen, d.pC) * (1 + d.sigma)
    d["LB"] = np.minimum(d.dOpen, d.pC) * (1 - d.sigma)
    chk = (d.mins % 30 == 0) & d.sigma.notna()
    d["nzUp"] = chk & (d.c > d.UB) & (d.c > d.vw)
    d["nzDn"] = chk & (d.c < d.LB) & (d.c < d.vw)
    return d

def replay_today(d, today):
    """Walk today's completed candles exactly like the strategy. Returns state dict."""
    s = dict(traded=False, dir=0, entry=None, stop=None, be=False, exited=False, exit_px=None,
             reason="", src="", entry_time=None, exit_time=None, be_time=None)
    t = d[d.date == today]
    for _, r in t.iterrows():
        if s["dir"] != 0 and not s["exited"]:
            # stop check (orders from previous candle)
            if s["dir"] == 1 and r.l <= s["stop"]:
                s.update(exited=True, exit_px=min(s["stop"], r.o), reason="BE" if s["be"] else "SL", exit_time=r.ct)
            elif s["dir"] == -1 and r.h >= s["stop"]:
                s.update(exited=True, exit_px=max(s["stop"], r.o), reason="BE" if s["be"] else "SL", exit_time=r.ct)
            elif not s["be"] and ((s["dir"] == 1 and r.h >= s["entry"] + SL) or (s["dir"] == -1 and r.l <= s["entry"] - SL)):
                s.update(stop=s["entry"], be=True, be_time=r.ct)
            if not s["exited"] and r.hhmm >= 1515:
                s.update(exited=True, exit_px=r.c, reason="3:15", exit_time=r.ct)
        if not s["traded"] and START_HHMM <= r.hhmm < LAST_HHMM and not pd.isna(r.pC):
            gl = bool(r.cprUp or r.stUp or r.nzUp); gs = bool(r.cprDn or r.stDn or r.nzDn)
            if gl != gs:
                dr = 1 if gl else -1
                src = ("CPR" if r.cprUp else "Supertrend" if r.stUp else "Noise") if gl else ("CPR" if r.cprDn else "Supertrend" if r.stDn else "Noise")
                s.update(traded=True, dir=dr, entry=r.c, stop=r.c - dr * SL, src=src, entry_time=r.ct)
    return s

def itm_strike(entry, dr):
    atm = round(entry / 50) * 50
    return (atm - 50, "CE") if dr == 1 else (atm + 50, "PE")

# ---------------- main loop ----------------
def hm(t):
    return t.hour * 100 + t.minute

def main():
    tg_chat_id()
    obj = angel_login()
    log("Angel login OK")
    if TEST:
        p = ltp(obj)
        tg(f"✅ Bot connected!\nAngel login OK, NIFTY LTP: {p}\nTelegram chat id: {tg_chat_id()}\n(Indha number-a TELEGRAM_CHAT_ID secret-a podunga)")
        return
    today = now().date()
    daily = candles(obj, "ONE_DAY", 30)
    if (daily.t.dt.date == today).sum() == 0:
        daily = pd.concat([daily, pd.DataFrame([{"t": pd.Timestamp(now()).normalize(), "o": np.nan, "h": np.nan, "l": np.nan, "c": np.nan}])], ignore_index=True)
    if RESP_START == 0:
        tg(f"Trigger-3 bot START ({today:%d %b})\nSL {SL:.0f} pts | 9:30-1:00 entry | 3:15 exit\nSignal vandha inga message varum.")
    sent = set()
    def fresh(t):  # announce only events in this run's time slot
        return t is None or hm(t) >= RESP_START
    while True:
        try:
            t_now = now()
            if hm(t_now) >= END_HHMM:
                break
            if hm(t_now) 
