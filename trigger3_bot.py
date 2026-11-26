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
TRAIL = float(opt("TRAIL_POINTS", "trail_points", "0"))   # trail SL this many points behind best price (0 = off)
TMODE = os.environ.get("TEST", "").strip().lower()
TEST = TMODE == "yes"
REPLAY = TMODE == "replay"
BACKTEST = TMODE == "backtest"
SCHED = os.environ.get("SCHED", "").strip()
# cloud runs in 2 parts: part 1 till 12:40, part 2 from 12:40 (GitHub max job time)
RESP_START, END_HHMM = (1240, 1535) if (SCHED.startswith("47 6") or TMODE == "p2") else ((0, 1240) if (SCHED or TMODE == "p1") else (0, 1535))
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
            r = requests.post(f"https://api.telegram.org/bot{TG_TOKEN}/sendMessage",
                              data={"chat_id": tg_chat_id(), "text": msg}, timeout=20)
            if r.ok:
                return
            log(f"telegram not ok: {r.text[:200]}")
            time.sleep(3)
            continue
        except Exception as e:
            log(f"telegram error {e}")
            time.sleep(3)

# ---------------- angel ----------------
def angel_login():
    from SmartApi import SmartConnect
    last = None
    for k in range(8):
        try:
            obj = SmartConnect(api_key=API_KEY)
            d = obj.generateSession(CLIENT, PIN, pyotp.TOTP(TOTP).now())
            if d and d.get("status"):
                return obj
            last = d
        except Exception as e:
            last = repr(e)[:200]
        log(f"login retry {k + 1}: {last}")
        time.sleep(20)
    raise RuntimeError(f"Angel login failed: {last}")

def candles(obj, interval, days):
    end = now()
    start = end - dt.timedelta(days=days)
    p = {"exchange": "NSE", "symboltoken": NIFTY_TOKEN, "interval": interval,
         "fromdate": start.strftime("%Y-%m-%d 09:00"), "todate": end.strftime("%Y-%m-%d %H:%M")}
    r = None
    for k in range(6):
        try:
            r = obj.getCandleData(p)
            if r and r.get("status") and r.get("data"):
                df = pd.DataFrame(r["data"], columns=["t", "o", "h", "l", "c", "v"])
                df["t"] = pd.to_datetime(df["t"]).dt.tz_convert(IST)
                return df.drop(columns="v")
        except Exception as e:
            r = repr(e)[:200]
        log(f"candle retry {k + 1}: {r}")
        time.sleep(15)
    raise RuntimeError(f"candle fetch failed: {r}")

def ltp(obj):
    try:
        r = obj.ltpData("NSE", NIFTY_SYM, NIFTY_TOKEN)
        return float(r["data"]["ltp"])
    except Exception:
        return None

# ---------------- option lookup (Angel scrip master) ----------------
SCRIP_URL = "https://margincalculator.angelbroking.com/OpenAPI_File/files/OpenAPIScripMaster.json"
_SCRIP = []

def load_scrips():
    global _SCRIP
    if _SCRIP:
        return _SCRIP
    try:
        data = requests.get(SCRIP_URL, timeout=90).json()
        _SCRIP = [r for r in data if r.get("exch_seg") == "NFO" and r.get("name") == "NIFTY" and r.get("instrumenttype") == "OPTIDX"]
        log(f"scrip master loaded: {len(_SCRIP)} NIFTY options")
    except Exception as e:
        log("scrip master load failed " + repr(e))
    return _SCRIP

def find_option(strike, typ):
    """Nearest expiry that is at least 2 days away."""
    today = now().date()
    best = None
    for r in load_scrips():
        try:
            exp = dt.datetime.strptime(r["expiry"], "%d%b%Y").date()
            if (exp - today).days >= 2 and abs(float(r["strike"]) / 100 - strike) == 0 and r["symbol"].endswith(typ):
                if best is None or best[0] > exp:
                    best = (exp, r)
        except Exception:
            continue
    return best

def opt_ltp(obj, r):
    try:
        return float(obj.ltpData("NFO", r["symbol"], r["token"])["data"]["ltp"])
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
            lo[i] = lo[i] if (lo[i] > pl or pl > c[i - 1]) else pl
            up[i] = up[i] if (pu > up[i] or c[i - 1] > pu) else pu
            dirn[i] = (-1 if c[i] > up[i] else 1) if st[i - 1] == pu else (1 if lo[i] > c[i] else -1)
        else:
            dirn[i] = 1
        st[i] = lo[i] if dirn[i] == -1 else up[i]
    prev = np.r_[1, dirn[:-1]]
    d["stUp"] = (0 > dirn) & (prev > 0)
    d["stDn"] = (dirn > 0) & (0 > prev)
    # previous day H/L/C from daily candles
    dd = daily.copy(); dd["date"] = dd.t.dt.date
    dd = dd.set_index("date")[["h", "l", "c"]].sort_index()
    prevmap = dd.shift(1)
    d = d.join(prevmap.rename(columns={"h": "pH", "l": "pL", "c": "pC"}), on="date")
    pp = (d.pH + d.pL + d.pC) / 3; bc0 = (d.pH + d.pL) / 2; tc0 = 2 * pp - bc0
    d["TC"] = np.maximum(tc0, bc0); d["BC"] = np.minimum(tc0, bc0)
    pc = d.c.shift(1)
    d["cprUp"] = (d.c > d.TC) & (d.TC >= pc)
    d["cprDn"] = (d.BC > d.c) & (pc >= d.BC)
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
    d["nzDn"] = chk & (d.LB > d.c) & (d.vw > d.c)
    return d

def replay_today(d, today):
    """Walk today's completed candles exactly like the strategy. Returns state dict."""
    s = dict(traded=False, dir=0, entry=None, stop=None, be=False, exited=False, exit_px=None,
             reason="", src="", entry_time=None, exit_time=None, be_time=None, best=0.0)
    t = d[d.date == today]
    for _, r in t.iterrows():
        if s["dir"] != 0 and not s["exited"]:
            # stop check (orders from previous candle)
            why = ("TRAIL" if (s["stop"] - s["entry"]) * s["dir"] > 0.01 else "BE") if s["be"] else "SL"
            if s["dir"] == 1 and s["stop"] >= r.l:
                s.update(exited=True, exit_px=min(s["stop"], r.o), reason=why, exit_time=r.ct)
            elif s["dir"] == -1 and r.h >= s["stop"]:
                s.update(exited=True, exit_px=max(s["stop"], r.o), reason=why, exit_time=r.ct)
            else:
                fav = (r.h - s["entry"]) if s["dir"] == 1 else (s["entry"] - r.l)
                s["best"] = max(s["best"], fav)
                if not s["be"] and s["best"] >= SL:
                    s.update(stop=s["entry"], be=True, be_time=r.ct)
                if TRAIL > 0 and s["best"] >= TRAIL:
                    ns = s["entry"] + s["dir"] * (s["best"] - TRAIL)
                    s["stop"] = max(s["stop"], ns) if s["dir"] == 1 else min(s["stop"], ns)
            if not s["exited"] and r.hhmm >= 1515:
                s.update(exited=True, exit_px=r.c, reason="3:15", exit_time=r.ct)
        if not s["traded"] and r.hhmm >= START_HHMM and LAST_HHMM > r.hhmm and not pd.isna(r.pC):
            gl = bool(r.cprUp or r.stUp or r.nzUp); gs = bool(r.cprDn or r.stDn or r.nzDn)
            if gl != gs:
                dr = 1 if gl else -1
                src = ("CPR" if r.cprUp else "Supertrend" if r.stUp else "Noise") if gl else ("CPR" if r.cprDn else "Supertrend" if r.stDn else "Noise")
                s.update(traded=True, dir=dr, entry=r.c, stop=r.c - dr * SL, src=src, entry_time=r.ct)
    return s

def itm_strike(entry, dr):
    atm = round(entry / 50) * 50
    return (atm - 50, "CE") if dr == 1 else (atm + 50, "PE")

# ---------------- backtest (Angel history) ----------------
def fetch_range(obj, interval, start, end, chunk_days):
    frames = []
    cur = start
    while end > cur:
        nxt = min(cur + dt.timedelta(days=chunk_days), end)
        p = {"exchange": "NSE", "symboltoken": NIFTY_TOKEN, "interval": interval,
             "fromdate": cur.strftime("%Y-%m-%d 09:00"), "todate": nxt.strftime("%Y-%m-%d 15:30")}
        r = None
        for _ in range(4):
            try:
                r = obj.getCandleData(p)
                if r and r.get("status"):
                    break
            except Exception as e:
                log("fetch err " + repr(e)[:200])
            time.sleep(3)
        if r and r.get("data"):
            df = pd.DataFrame(r["data"], columns=["t", "o", "h", "l", "c", "v"])
            df["t"] = pd.to_datetime(df["t"]).dt.tz_convert(IST)
            frames.append(df.drop(columns="v"))
            log(f"{interval} {cur:%Y-%m-%d} -> {nxt:%Y-%m-%d}: {len(df)} candles")
        cur = nxt + dt.timedelta(days=1)
        time.sleep(1.5)
    return pd.concat(frames).drop_duplicates("t").sort_values("t").reset_index(drop=True)

def run_bt(d, days, trail):
    global TRAIL
    keep = TRAIL
    TRAIL = trail
    rows = []
    for day in days:
        s = replay_today(d, day)
        if s["traded"] and s["exited"]:
            rows.append((day, (s["exit_px"] - s["entry"]) * s["dir"], s["reason"]))
    TRAIL = keep
    return pd.DataFrame(rows, columns=["date", "pts", "why"])

def bt_report(t, title):
    t = t.copy()
    t["m"] = pd.to_datetime(t.date).dt.strftime("%b %y")
    t["mk"] = pd.to_datetime(t.date).dt.strftime("%Y-%m")
    t["opt"] = (0.6 * t.pts - 5) * 65 - 80
    g = t.groupby(["mk", "m"]).agg(n=("pts", "size"), w=("pts", lambda x: int((x > 0).sum())),
                                    pts=("pts", "sum"), opt=("opt", "sum")).reset_index()
    lines = [title, "Month | trades | win | points | option Rs (1 lot)"]
    for _, r in g.iterrows():
        lines.append(f"{r.m}: {r.n} | {r.w} | {r.pts:+.0f} | {r.opt:+,.0f}")
    lines.append(f"TOTAL: {len(t)} trades | win {int((t.pts > 0).sum())} | {t.pts.sum():+.0f} pts | Rs {t.opt.sum():+,.0f}")
    lines.append(f"Profit months: {int((g.opt > 0).sum())}/{len(g)} | best Rs {g.opt.max():+,.0f} | worst Rs {g.opt.min():+,.0f}")
    return "\n".join(lines), g

# ---------------- main loop ----------------
def hm(t):
    return t.hour * 100 + t.minute

def main():
    tg_chat_id()
    obj = angel_login()
    log("Angel login OK")
    if TEST:
        p = ltp(obj)
        k, typ = itm_strike(p or 25000, 1)
        bo = find_option(k, typ)
        ot = f"{bo[1]['symbol']} = Rs {opt_ltp(obj, bo[1])}" if bo else "option kidaikkala"
        tg(f"✅ Bot connected!\nAngel login OK, NIFTY LTP: {p}\nOption check: {ot}\nTelegram chat id: {tg_chat_id()}")
        return
    if BACKTEST:
        end = now()
        start = dt.datetime(2024, 11, 1, 9, 0, tzinfo=IST)
        tg("Backtest start: Jan 2025 - innaikku varai data edukkuren (2-3 nimisham)...")
        m15 = fetch_range(obj, "FIFTEEN_MINUTE", start, end, 90)
        daily = fetch_range(obj, "ONE_DAY", start - dt.timedelta(days=10), end, 1500)
        m15 = m15[(m15.t.dt.hour * 100 + m15.t.dt.minute) >= 915]
        d = features(m15, daily)
        today = end.date()
        days = sorted(x for x in d.date.unique() if x >= dt.date(2025, 1, 1) and today > x)
        log(f"backtest days: {len(days)}")
        for trail, name in [(0, "CURRENT rules (BE + 3:15)"), (250, "With Trail 250")]:
            t = run_bt(d, days, trail)
            txt, g = bt_report(t, f"BACKTEST {name}\nSL {SL:.0f}, 1 lot = 65, option = estimate")
            tg(txt)
            t.to_csv(os.path.join(HERE, f"backtest_trail{trail}.csv"), index=False)
        return
    if REPLAY:
        daily = candles(obj, "ONE_DAY", 30)
        d = features(candles(obj, "FIFTEEN_MINUTE", 45), daily)
        day = max(d.date)
        s = replay_today(d, day)
        if not s["traded"]:
            tg(f"REPLAY {day:%d %b}: signal illa, trade illa.")
        else:
            k, typ = itm_strike(s["entry"], s["dir"])
            pts = (s["exit_px"] - s["entry"]) * s["dir"] if s["exited"] else 0
            tg(f"REPLAY {day:%d %b}\n{'BUY' if s['dir'] == 1 else 'SELL'} ({s['src']}) at {s['entry_time']:%H:%M}\nEntry {s['entry']:.1f} | Option NIFTY {k} {typ}\nBreakeven: {'yes' if s['be'] else 'no'}\nExit: {s['reason']} at {s['exit_px']:.1f}\nResult: {pts:+.1f} points")
        return
    today = now().date()
    load_scrips()
    opt = {}
    daily = candles(obj, "ONE_DAY", 30)
    if (daily.t.dt.date == today).sum() == 0:
        daily = pd.concat([daily, pd.DataFrame([{"t": pd.Timestamp(now()).normalize(), "o": np.nan, "h": np.nan, "l": np.nan, "c": np.nan}])], ignore_index=True)
    if RESP_START == 0:
        tg(f"Trigger-3 bot START ({today:%d %b})\nSL {SL:.0f} | BE +{SL:.0f} | Trail {TRAIL:.0f} | 9:30-1:00 entry | 3:15 exit\nSignal vandha inga message varum.")
    sent = set()
    def fresh(t):  # announce only events in this run's time slot
        return t is None or hm(t) >= RESP_START
    m15 = None
    fetched_slot = None
    trail_sent = 0.0
    lo_live = hi_live = None
    while True:
        try:
            t_now = now()
            if hm(t_now) >= END_HHMM:
                break
            if 915 > hm(t_now):
                time.sleep(60); continue
            # candles: fetch only when a new 15-min candle should be complete (saves API calls)
            day0 = pd.Timestamp(t_now).normalize() + pd.Timedelta(hours=9, minutes=15)
            slot = int((pd.Timestamp(t_now) - day0).total_seconds() // 900)
            prev_start = day0 + pd.Timedelta(minutes=15 * (slot - 1))
            need = m15 is None or slot != fetched_slot
            if need:
                m15 = candles(obj, "FIFTEEN_MINUTE", 45)
                lo_live = hi_live = None
                # mark this slot done only when the just-closed candle is in the data
                if slot == 0 or (m15.t == prev_start).any():
                    fetched_slot = slot
            if (m15.t.dt.date == today).sum() == 0:
                if hm(t_now) >= 1000:
                    log("no candles today - holiday?"); break
                m15 = None
                time.sleep(60); continue
            done = m15[t_now - pd.Timedelta(seconds=5) >= m15.t + pd.Timedelta(minutes=15)]
            d = features(done, daily)
            s = replay_today(d, today)
            px = ltp(obj)
            if px is not None:
                lo_live = px if lo_live is None else min(lo_live, px)
                hi_live = px if hi_live is None else max(hi_live, px)
            if s["dir"] != 0 and not s["exited"] and lo_live is not None:
                if (s["dir"] == 1 and s["stop"] >= lo_live) or (s["dir"] == -1 and hi_live >= s["stop"]):
                    lr = ("TRAIL" if (s["stop"] - s["entry"]) * s["dir"] > 0.01 else "BE") if s["be"] else "SL"
                    s.update(exited=True, exit_px=s["stop"], reason=lr, exit_time=t_now)
            if s["traded"] and "entry" not in sent:
                sent.add("entry")
                k, typ = itm_strike(s["entry"], s["dir"])
                bo = find_option(k, typ)
                if bo:
                    opt.update(r=bo[1], exp=bo[0], px=opt_ltp(obj, bo[1]))
                if fresh(s["entry_time"]):
                    side = "BUY  ->  CE vaangu" if s["dir"] == 1 else "SELL  ->  PE vaangu"
                    oname = f"NIFTY {opt['exp']:%d %b} {k} {typ}" if opt else f"NIFTY {k} {typ}"
                    oprice = f"\nOption price ippo: Rs {opt['px']}" if opt.get("px") else ""
                    tg(f"🔔 {side}\nNIFTY entry: {s['entry']:.1f}  ({s['src']})\nOption: {oname} (1-ITM){oprice}\nSL (NIFTY): {s['entry'] - s['dir'] * SL:.1f}\nNIFTY SL-a thottaa option-a vithudu.")
            if s["be"] and "be" not in sent and "exit" not in sent:
                sent.add("be")
                if fresh(s["be_time"]) and not s["exited"]:
                    op = opt_ltp(obj, opt["r"]) if opt else None
                    tg(f"✅ +{SL:.0f} points vanthuduchu. SL ippo entry {s['entry']:.1f} (breakeven). Inime nashtam illa." + (f"\nOption price ippo: Rs {op}" if op else ""))
            if s["be"] and not s["exited"] and "exit" not in sent:
                locked = (s["stop"] - s["entry"]) * s["dir"]
                if locked - trail_sent >= 25:
                    trail_sent = locked
                    op = opt_ltp(obj, opt["r"]) if opt else None
                    tg(f"🔼 Trailing SL ethiyachu: NIFTY SL ippo {s['stop']:.1f}  (+{locked:.0f} points lock)" + (f"\nOption price ippo: Rs {op}" if op else ""))
            if s["exited"] and "exit" not in sent:
                sent.add("exit")
                if fresh(s["exit_time"]):
                    pts = (s["exit_px"] - s["entry"]) * s["dir"]
                    why = {"SL": "SL hit", "BE": "Breakeven exit", "TRAIL": "Trailing SL exit", "3:15": "3:15 exit"}[s["reason"]]
                    op = opt_ltp(obj, opt["r"]) if opt else None
                    ol = ""
                    if op and opt.get("px"):
                        ol = f"\nOption: Rs {opt['px']} -> Rs {op}  =  Rs {(op - opt['px']) * 65:+.0f} (1 lot)"
                    elif op:
                        ol = f"\nOption price ippo: Rs {op}"
                    tg(f"⏹ EXIT ({why})\nNIFTY {s['exit_px']:.1f}  |  {pts:+.1f} points{ol}\nOption-a ippo vithudu. Innaikku trade mudinjathu.")
            if s["traded"] and not s["exited"] and hm(t_now) >= 1515 and "exit" not in sent:
                sent.add("exit")
                pts = ((px or s["entry"]) - s["entry"]) * s["dir"]
                op = opt_ltp(obj, opt["r"]) if opt else None
                ol = ""
                if op and opt.get("px"):
                    ol = f"\nOption: Rs {opt['px']} -> Rs {op}  =  Rs {(op - opt['px']) * 65:+.0f} (1 lot)"
                elif op:
                    ol = f"\nOption price ippo: Rs {op}"
                tg(f"⏹ 3:15 EXIT\nNIFTY {px}  |  {pts:+.1f} points{ol}\nOption-a ippo vithudu.")
            if not s["traded"] and hm(t_now) >= 1300 and "nosig" not in sent:
                sent.add("nosig")
                tg("Innaikku 1 manikku munnadi signal varala. Trade illa.")
            log(f"ltp {px} | traded {s['traded']} dir {s['dir']} stop {s['stop']} be {s['be']} exited {s['exited']}")
        except Exception as e:
            msg = repr(e).lower()
            log("ERROR " + repr(e)[:300])
            if "access rate" in msg:
                time.sleep(30)
            elif "token" in msg or "session" in msg or "invalid" in msg or "unauthor" in msg:
                time.sleep(5)
                try:
                    obj = angel_login()
                except Exception as e2:
                    log("relogin failed " + repr(e2)[:300])
        time.sleep(max(5, 60 - now().second + 10))
    if END_HHMM >= 1535:
        tg("Market mudinjathu. Bot stop aagudhu.")

if __name__ == "__main__":
    main()
