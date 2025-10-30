
#!/usr/bin/env python3
# -*- coding: utf-8 -*-
from __future__ import annotations
import os, sys, time, json, math, threading
from dataclasses import dataclass, field, asdict
from typing import List, Dict, Optional
from datetime import datetime, timezone
try:
    from zoneinfo import ZoneInfo
except Exception:
    ZoneInfo = None

BINANCE_API = "https://api.binance.com"
CONFIG_PATH = os.environ.get("SCANNER_CONFIG","binance_scanner_config.json")

@dataclass
class EarlySignalSettings:
    use_partial_candle: bool = True
    partial_grace_sec: int = 120
    pre_breakout_enable: bool = True

@dataclass
class DisplaySettings:
    timezone_name: str = "Asia/Bangkok"

@dataclass
class SignalSettings:
    timeframe: str = "15m"
    ema_fast: int = 9
    ema_slow: int = 21
    rsi_period: int = 14
    rsi_min: float = 55.0
    atr_period: int = 14
    stop_atr_mult: float = 1.8
    rr_target: float = 2.0
    breakout_lookback: int = 20
    adx_period: int = 14
    adx_min: float = 18.0
    vwap_atr_dist_max: float = 6.0

@dataclass
class FilterSettings:
    atr_pct_min: float = 0.002
    atr_pct_max: float = 0.15
    min_24h_qv_usdt: float = 5_000_000.0
    min_bar_qv_usdt: float = 200_000.0
    bar_qv_ma_period: int = 20
    vol_spike_mult: float = 1.2

@dataclass
class RuntimeSettings:
    top_symbols: int = 80
    workers: int = 8
    rate_sleep: float = 0.2
    mode: str = "auto"  # breakout/pullback/auto

@dataclass
class Config:
    signal: SignalSettings = field(default_factory=SignalSettings)
    filters: FilterSettings = field(default_factory=FilterSettings)
    early: EarlySignalSettings = field(default_factory=EarlySignalSettings)
    display: DisplaySettings = field(default_factory=DisplaySettings)
    runtime: RuntimeSettings = field(default_factory=RuntimeSettings)
    exclude_leveraged: bool = True

    def to_json(self, path: str):
        with open(path,"w",encoding="utf-8") as f:
            json.dump(asdict(self), f, ensure_ascii=False, indent=2)

    @staticmethod
    def from_json(path: str) -> "Config":
        if not os.path.exists(path):
            return Config()
        data = json.load(open(path,"r",encoding="utf-8"))
        def load(cls, key):
            obj = cls()
            vals = data.get(key,{})
            for k in obj.__dict__.keys():
                if k in vals: setattr(obj,k,vals[k])
            return obj
        return Config(
            signal=load(SignalSettings,"signal"),
            filters=load(FilterSettings,"filters"),
            early=load(EarlySignalSettings,"early"),
            display=load(DisplaySettings,"display"),
            runtime=load(RuntimeSettings,"runtime"),
            exclude_leveraged=bool(data.get("exclude_leveraged",True)),
        )

def timeframe_to_seconds(tf: str) -> int:
    unit = tf[-1].lower(); n = int(tf[:-1])
    return n*60 if unit=="m" else n*3600 if unit=="h" else n*86400

def fmt_ts_ms(ms: int, tz_name: str) -> str:
    dt_utc = datetime.fromtimestamp(ms/1000, tz=timezone.utc)
    if tz_name and ZoneInfo:
        try:
            dt_local = dt_utc.astimezone(ZoneInfo(tz_name))
        except Exception:
            dt_local = dt_utc.astimezone()
    else:
        dt_local = dt_utc.astimezone()
    return dt_local.strftime("%Y-%m-%d %H:%M:%S")

def seconds_to_close(open_ms: int, tf_sec: int) -> int:
    now_utc = datetime.now(timezone.utc).timestamp()
    close_ts = open_ms/1000 + tf_sec
    return int(close_ts - now_utc)

import requests

def http_get(url: str, params: Dict=None, timeout: int=15, retries: int=2):
    for i in range(retries+1):
        try:
            r = requests.get(url, params=params, timeout=timeout)
            if r.status_code == 200:
                return r.json()
            time.sleep(0.4*(i+1))
        except Exception:
            time.sleep(0.4*(i+1))
    raise RuntimeError(f"GET failed: {url} {params}")

def fetch_top_symbols(limit: int=80, exclude_leveraged: bool=True):
    data = http_get(f"{BINANCE_API}/api/v3/ticker/24hr")
    items=[]
    for t in data:
        s=t.get("symbol","")
        if not s.endswith("USDT"): continue
        if exclude_leveraged and ("UPUSDT" in s or "DOWNUSDT" in s): continue
        qv=float(t.get("quoteVolume",0.0)); items.append((s,qv))
    items.sort(key=lambda x: x[1], reverse=True)
    return [s for s,_ in items[:limit]]

def fetch_klines(symbol: str, interval: str, limit: int=210):
    return http_get(f"{BINANCE_API}/api/v3/klines", params={"symbol":symbol,"interval":interval,"limit":limit})

def fetch_24h_stats(symbol: str):
    return http_get(f"{BINANCE_API}/api/v3/ticker/24hr", params={"symbol":symbol})

def ema(vals, period):
    if period<=1: return vals[:]
    out=[math.nan]*len(vals); k=2/(period+1)
    prev=None
    for i,v in enumerate(vals):
        if math.isnan(v): continue
        if prev is None: prev=v
        else: prev = v*k + prev*(1-k)
        out[i]=prev
    return out

def rsi(closes, period):
    out=[math.nan]*len(closes)
    gains=[0.0]*len(closes); losses=[0.0]*len(closes)
    for i in range(1,len(closes)):
        ch=closes[i]-closes[i-1]
        gains[i]=max(0.0,ch); losses[i]=max(0.0,-ch)
    ema_g=ema(gains, period); ema_l=ema(losses, period)
    for i in range(len(closes)):
        if ema_l[i] in (0.0, None) or math.isnan(ema_l[i]) or math.isnan(ema_g[i]): continue
        rs=ema_g[i]/ema_l[i]; out[i]=100 - (100/(1+rs))
    return out

def true_range(h,l,c_prev): return max(h-l, abs(h-c_prev), abs(l-c_prev))

def atr(highs,lows,closes,period):
    trs=[math.nan]*len(closes)
    for i in range(1,len(closes)):
        trs[i]=true_range(highs[i],lows[i],closes[i-1])
    return ema([0 if math.isnan(x) else x for x in trs], period)

def adx(highs,lows,closes,period=14):
    plus_dm=[0.0]*len(closes); minus_dm=[0.0]*len(closes); tr=[0.0]*len(closes)
    for i in range(1,len(closes)):
        up=highs[i]-highs[i-1]; dn=lows[i-1]-lows[i]
        plus_dm[i]=up if (up>dn and up>0) else 0.0
        minus_dm[i]=dn if (dn>up and dn>0) else 0.0
        tr[i]=true_range(highs[i],lows[i],closes[i-1])
    def _ema(a,p): return ema(a,p)
    ema_tr=_ema(tr,period); ema_pdm=_ema(plus_dm,period); ema_mdm=_ema(minus_dm,period)
    plus_di=[0.0]*len(closes); minus_di=[0.0]*len(closes); dx=[math.nan]*len(closes)
    for i in range(len(closes)):
        if ema_tr[i] and ema_tr[i]>0:
            plus_di[i]=100*(ema_pdm[i]/ema_tr[i]) if ema_pdm[i] else 0.0
            minus_di[i]=100*(ema_mdm[i]/ema_tr[i]) if ema_mdm[i] else 0.0
            s=plus_di[i]+minus_di[i]; dx[i]=0.0 if s==0 else 100*abs(plus_di[i]-minus_di[i])/s
    adx_vals=ema([0 if math.isnan(x) else x for x in dx], period)
    return adx_vals, plus_di, minus_di

def session_vwap(highs,lows,closes,vols):
    num=0.0; den=0.0; out=[math.nan]*len(closes)
    for i in range(len(closes)):
        tp=(highs[i]+lows[i]+closes[i])/3.0
        num+=tp*vols[i]; den+=vols[i]
        out[i]= (num/den) if den>0 else math.nan
    return out

from dataclasses import dataclass
@dataclass
class Signal:
    symbol: str
    entry: float
    t1: float; t2: float; t3: float
    stop: float
    ts_ms: int
    mode_used: str
    bar_qv: float; bar_qv_ma: float; day_qv: float

def round_to_tick(v: float, step: float=0.0001, direction: Optional[str]=None) -> float:
    q = int(v/step + (0 if direction=='down' else 0.5))
    if direction=='down': q=int(v/step)
    if direction=='up': q=math.ceil(v/step)
    return round(q*step, 8)


def evaluate_signal_for_symbol(symbol, klines, stats24h, cfg: Config):
    s=cfg.signal; f=cfg.filters; rt=cfg.runtime; e=cfg.early
    tf_sec = timeframe_to_seconds(s.timeframe)

    opens=[]; highs=[]; lows=[]; closes=[]; vols=[]; qvols=[]; ctimes=[]
    for k in klines:
        opens.append(float(k[1])); highs.append(float(k[2])); lows.append(float(k[3])); closes.append(float(k[4]))
        vols.append(float(k[5])); ctimes.append(int(k[6]))
        qvols.append(float(k[7]) if len(k)>7 else float(k[5])*float(k[4]))

    need = max(s.ema_slow, s.rsi_period, s.atr_period, s.breakout_lookback) + 5
    if len(closes) < need: return None

    ema_fast = ema(closes, s.ema_fast)
    ema_slow = ema(closes, s.ema_slow)
    rsi_vals = rsi(closes, s.rsi_period)
    atr_vals = atr(highs, lows, closes, s.atr_period)
    adx_vals, plus_di, minus_di = adx(highs, lows, closes, s.adx_period)
    vwap_vals = session_vwap(highs, lows, closes, qvols)

    i=len(closes)-1
    close_i=closes[i]; emaf=ema_fast[i]; emas=ema_slow[i]
    rsi_i=rsi_vals[i]; atr_i=atr_vals[i]; adx_i=adx_vals[i] if adx_vals[i] is not None else 0.0
    bar_qv=qvols[i]; bar_qv_ma=sum(qvols[-f.bar_qv_ma_period:])/max(1,f.bar_qv_ma_period)
    day_qv=float(stats24h.get("quoteVolume",0.0))

    # Filters
    if day_qv < f.min_24h_qv_usdt: return None
    if bar_qv < f.min_bar_qv_usdt: return None
    if bar_qv_ma>0 and (bar_qv < f.vol_spike_mult*bar_qv_ma): return None
    if any(math.isnan(x) for x in [emaf,emas,rsi_i,atr_i]): return None
    atr_pct = atr_i/close_i
    if not (f.atr_pct_min <= atr_pct <= f.atr_pct_max): return None
    if adx_i is not None and adx_i < s.adx_min: return None
    vwap_i=vwap_vals[i]
    if not math.isnan(vwap_i) and s.vwap_atr_dist_max<999:
        if abs(close_i - vwap_i) > s.vwap_atr_dist_max*atr_i: return None

    recent_high=max(highs[-s.breakout_lookback:])
    bullish = closes[i] > opens[i]
    trend_up = emaf >= emas and plus_di[i] >= minus_di[i]

    def build(mode):
        entry=close_i; stop=entry - s.stop_atr_mult*atr_i
        if stop >= entry: return None
        R=entry - stop
        t1=entry + 1.0*R; t2=entry + 1.5*R; t3=entry + s.rr_target*R
        entry=round_to_tick(entry); stop=round_to_tick(stop, direction="down")
        t1=round_to_tick(t1, direction="up"); t2=round_to_tick(t2, direction="up"); t3=round_to_tick(t3, direction="up")
        return Signal(symbol, entry, t1, t2, t3, stop, ctimes[i], mode, bar_qv, (bar_qv_ma if not math.isnan(bar_qv_ma) else 0.0), day_qv)

    # Early
    if e.use_partial_candle:
        secs_left = seconds_to_close(int(klines[-1][0]), tf_sec)
        if secs_left <= e.partial_grace_sec and rt.mode in ("auto","breakout"):
            if e.pre_breakout_enable and close_i >= recent_high and trend_up and rsi_i>=s.rsi_min:
                sig=build("breakout-early")
                if sig: return sig

    if rt.mode in ("auto","breakout"):
        if close_i >= recent_high and trend_up and rsi_i >= s.rsi_min and bullish:
            sig=build("breakout")
            if sig: return sig

    if rt.mode in ("auto","pullback"):
        if trend_up and closes[i] >= emaf >= emas and rsi_i >= s.rsi_min and bullish:
            sig=build("pullback")
            if sig: return sig

    return None

def format_signal(sig: Signal, cfg: Config) -> str:
    ts = fmt_ts_ms(sig.ts_ms, cfg.display.timezone_name)
    lines = [
        "🔔 BUY SIGNAL วันที่/เวลา: " + ts,
        f"🪙เหรียญ {sig.symbol}",
        f"🟢BUY :{sig.entry}",
        f"🟡SELL :{sig.t1}",
        f"🎯T1: {sig.t1}",
        f"🎯T2: {sig.t2}",
        f"🎯T3: {sig.t3}",
        f"🔴STOP LOSS  :{sig.stop}",
        f"📊Vol(bar): {sig.bar_qv/1e6:.2f}M USDT (MA{cfg.filters.bar_qv_ma_period}×{(sig.bar_qv/max(1.0,sig.bar_qv_ma)):.2f})",
        f"💧Vol(24h): {sig.day_qv/1e6:.2f}M USDT",
        f"⚙️Mode: {sig.mode_used}",
    ]
    return "\n".join(lines)

def notify_telegram(token: str, chat_id: str, sig: Signal, cfg: Config):
    if not token or not chat_id:
        print("[dryrun]", format_signal(sig, cfg)); return
    import requests
    url=f"https://api.telegram.org/bot{token}/sendMessage"
    payload={"chat_id": chat_id, "text": format_signal(sig, cfg), "parse_mode": "HTML"}
    try:
        requests.post(url, json=payload, timeout=10)
    except Exception as ex:
        print("[telegram error]", ex)

def scan_once(cfg: Config, tg_token: str, tg_chat: str):
    symbols = fetch_top_symbols(cfg.runtime.top_symbols, cfg.exclude_leveraged)
    for s in symbols:
        try:
            kl = fetch_klines(s, cfg.signal.timeframe, 210)
            st = fetch_24h_stats(s)
            sig = evaluate_signal_for_symbol(s, kl, st, cfg)
            if sig:
                notify_telegram(tg_token, tg_chat, sig, cfg)
            time.sleep(cfg.runtime.rate_sleep)
        except Exception as ex:
            print("[scan error]", s, ex)

def menu_settings(cfg: Config) -> Config:
    s=cfg.signal; f=cfg.filters; e=cfg.early; r=cfg.runtime; d=cfg.display
    while True:
        print("\n===== Settings (Menu 3) =====")
        print(f"Timeframe           : {s.timeframe}")
        print(f"EMA fast/slow       : {s.ema_fast} / {s.ema_slow}")
        print(f"RSI period/min      : {s.rsi_period} / {s.rsi_min}")
        print(f"ATR period/stop*R   : {s.atr_period} / {s.stop_atr_mult}")
        print(f"RR target           : {s.rr_target}")
        print(f"Breakout lookback   : {s.breakout_lookback}")
        print(f"ADX period/min      : {s.adx_period} / {s.adx_min}")
        print(f"VWAP dist (ATR)     : {s.vwap_atr_dist_max}")
        print("--- Filters ---")
        print(f"ATR% min/max        : {f.atr_pct_min} / {f.atr_pct_max}")
        print(f"24h QV min (USDT)   : {int(f.min_24h_qv_usdt)}")
        print(f"Bar QV min (USDT)   : {int(f.min_bar_qv_usdt)}")
        print(f"Vol MA / spike×     : {f.bar_qv_ma_period} / {f.vol_spike_mult}")
        print("--- Early Signal ---")
        print(f"Use partial candle  : {e.use_partial_candle}")
        print(f"Grace seconds       : {e.partial_grace_sec}")
        print(f"Pre-breakout        : {e.pre_breakout_enable}")
        print("--- Display ---")
        print(f"Timezone            : {d.timezone_name}")
        print("--- Runtime ---")
        print(f"Mode                : {r.mode}")
        print(f"Top symbols/workers/rate: {r.top_symbols}/{r.workers}/{r.rate_sleep}s")
        print("Commands: tf/ema/rsi/atr/rr/lookback/adx/vwap | atrp/vol/24h/bar | early/display | mode/top/workers/rate | save/exit")
        k=input(": ").strip().lower()
        try:
            if k=="tf": s.timeframe=input("Timeframe (5m/15m/1h): ").strip()
            elif k=="ema": s.ema_fast=int(input("EMA fast: ")); s.ema_slow=int(input("EMA slow: "))
            elif k=="rsi": s.rsi_period=int(input("RSI period: ")); s.rsi_min=float(input("RSI min: "))
            elif k=="atr": s.atr_period=int(input("ATR period: ")); s.stop_atr_mult=float(input("Stop ATR mult: "))
            elif k=="rr": s.rr_target=float(input("RR target: "))
            elif k=="lookback": s.breakout_lookback=int(input("Breakout lookback: "))
            elif k=="adx": s.adx_period=int(input("ADX period: ")); s.adx_min=float(input("ADX min: "))
            elif k=="vwap": s.vwap_atr_dist_max=float(input("VWAP dist max (ATR): "))
            elif k=="atrp": f.atr_pct_min=float(input("ATR% min: ")); f.atr_pct_max=float(input("ATR% max: "))
            elif k=="vol": f.bar_qv_ma_period=int(input("Vol MA period: ")); f.vol_spike_mult=float(input("Spike×: "))
            elif k=="24h": f.min_24h_qv_usdt=float(input("Min 24h quote vol USDT: "))
            elif k=="bar": f.min_bar_qv_usdt=float(input("Min per-bar quote vol USDT: "))
            elif k=="early":
                sub=input("Early (toggle/grace/pre): ").strip().lower()
                if sub=="toggle": e.use_partial_candle=not e.use_partial_candle
                elif sub=="grace": e.partial_grace_sec=int(input("Grace seconds: "))
                elif sub=="pre": e.pre_breakout_enable=not e.pre_breakout_enable
            elif k=="display":
                tz=input("Timezone (e.g. Asia/Bangkok or UTC): ").strip()
                if tz: d.timezone_name=tz
            elif k=="mode": r.mode=input("Mode (breakout/pullback/auto): ").strip()
            elif k=="top": r.top_symbols=int(input("Top symbols: "))
            elif k=="workers": r.workers=int(input("Workers: "))
            elif k=="rate": r.rate_sleep=float(input("Rate sleep (sec): "))
            elif k=="save": cfg.to_json(CONFIG_PATH); print("Saved.")
            elif k in ("exit","q"): cfg.to_json(CONFIG_PATH); return cfg
        except Exception as ex:
            print("[ERR]", ex)

def main():
    import argparse, os
    ap=argparse.ArgumentParser()
    ap.add_argument("--autostart", action="store_true")
    ap.add_argument("--minutes", type=int, default=5)
    ap.add_argument("--tf", type=str); ap.add_argument("--top", type=int); ap.add_argument("--mode", type=str)
    args=ap.parse_args()

    cfg=Config.from_json(CONFIG_PATH)
    if args.tf: cfg.signal.timeframe=args.tf
    if args.top: cfg.runtime.top_symbols=args.top
    if args.mode: cfg.runtime.mode=args.mode

    tg_token=os.environ.get("TELEGRAM_BOT_TOKEN",""); tg_chat=os.environ.get("TELEGRAM_CHAT_ID","")

    if not args.autostart:
        while True:
            print("\n=== Binance Scanner v1.2 (clean) ===")
            print("1) Scan once\n2) Auto-run\n3) Settings (Menu 3)\n4) Exit")
            ch=input(": ").strip()
            if ch=="1": scan_once(cfg, tg_token, tg_chat)
            elif ch=="2":
                mins=int(input("Every how many minutes? (default 5): ") or 5)
                while True:
                    t0=time.time(); scan_once(cfg, tg_token, tg_chat)
                    left=max(0, mins*60 - (time.time()-t0)); print(f"[sleep {int(left)}s]"); time.sleep(left)
            elif ch=="3": cfg=menu_settings(cfg)
            elif ch=="4": cfg.to_json(CONFIG_PATH); break
    else:
        while True:
            t0=time.time(); scan_once(cfg, tg_token, tg_chat)
            left=max(0, args.minutes*60 - (time.time()-t0)); time.sleep(left)

if __name__=="__main__":
    main()
