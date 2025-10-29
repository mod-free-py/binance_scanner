
#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Program: binance_scanner_v1_2
Desc   : Binance USDT scanner with early-signal options exposed in Menu 3.
Requires: requests
"""

import os, sys, time, json, math, signal, threading, argparse
from dataclasses import dataclass, asdict, field
from datetime import datetime, timezone
from zoneinfo import ZoneInfo
from typing import Dict, List, Optional, Tuple
from concurrent.futures import ThreadPoolExecutor, as_completed
import requests

BINANCE_API = "https://api.binance.com"
DEFAULT_CONFIG_PATH = "binance_scanner_config.json"
TZ_BANGKOK = ZoneInfo("Asia/Bangkok")

ALLOWED_INTERVALS = ["5m","15m","1h","4h","1d"]

# ---------------- Config ---------------- #
@dataclass
class SignalSettings:
    timeframe: str = "15m"
    ema_fast: int = 9
    ema_slow: int = 21
    rsi_period: int = 14
    rsi_min: float = 55.0
    atr_period: int = 14
    stop_atr_mult: float = 1.3
    rr_target: float = 2.0
    breakout_lookback: int = 20
    min_candles: int = 200
    mode: str = "auto"        # breakout | pullback | auto
    adx_period: int = 14
    adx_min: float = 25.0
    vwap_dist_atr: float = 0.4
    min_24h_qv_usdt: float = 100_000_000.0
    min_bar_qv_usdt: float = 2_000_000.0
    vol_ma_period: int = 20
    vol_spike_mult: float = 1.2
    atr_pct_min: float = 0.002
    atr_pct_max: float = 0.15

@dataclass
class EarlySignalSettings:
    use_partial_candle: bool = True
    partial_grace_sec: int = 120
    pre_breakout_enable: bool = True

@dataclass
class Config:
    timescan_minutes: int = 5
    top_vol_limit: int = 80
    settings: SignalSettings = field(default_factory=SignalSettings)
    early: EarlySignalSettings = field(default_factory=EarlySignalSettings)
    telegram_token: str = ""
    telegram_chat_id: str = ""
    rate_limit_sleep: float = 0.02
    log_signals: bool = False
    workers: int = 4
    cache_ttl_sec: int = 3600
    exclude_leveraged: bool = True
    _config_path: str = DEFAULT_CONFIG_PATH

    def to_json(self)->dict:
        d = asdict(self)
        d["settings"] = asdict(self.settings)
        d["early"] = asdict(self.early)
        return d

    @staticmethod
    def from_json(data: dict)->"Config":
        s = data.get("settings",{})
        e = data.get("early",{})
        cfg = Config(
            timescan_minutes=int(data.get("timescan_minutes",5)),
            top_vol_limit=int(data.get("top_vol_limit",80)),
            settings=SignalSettings(
                timeframe=s.get("timeframe","15m"),
                ema_fast=int(s.get("ema_fast",9)),
                ema_slow=int(s.get("ema_slow",21)),
                rsi_period=int(s.get("rsi_period",14)),
                rsi_min=float(s.get("rsi_min",55.0)),
                atr_period=int(s.get("atr_period",14)),
                stop_atr_mult=float(s.get("stop_atr_mult",1.3)),
                rr_target=float(s.get("rr_target",2.0)),
                breakout_lookback=int(s.get("breakout_lookback",20)),
                min_candles=int(s.get("min_candles",200)),
                mode=s.get("mode","auto"),
                adx_period=int(s.get("adx_period",14)),
                adx_min=float(s.get("adx_min",25.0)),
                vwap_dist_atr=float(s.get("vwap_dist_atr",0.4)),
                min_24h_qv_usdt=float(s.get("min_24h_qv_usdt",100_000_000.0)),
                min_bar_qv_usdt=float(s.get("min_bar_qv_usdt",2_000_000.0)),
                vol_ma_period=int(s.get("vol_ma_period",20)),
                vol_spike_mult=float(s.get("vol_spike_mult",1.2)),
                atr_pct_min=float(s.get("atr_pct_min",0.002)),
                atr_pct_max=float(s.get("atr_pct_max",0.15)),
            ),
            early=EarlySignalSettings(
                use_partial_candle=bool(e.get("use_partial_candle",True)),
                partial_grace_sec=int(e.get("partial_grace_sec",120)),
                pre_breakout_enable=bool(e.get("pre_breakout_enable",True)),
            ),
            telegram_token=data.get("telegram_token",""),
            telegram_chat_id=data.get("telegram_chat_id",""),
            rate_limit_sleep=float(data.get("rate_limit_sleep",0.02)),
            log_signals=bool(data.get("log_signals",False)),
            workers=int(data.get("workers",4)),
            cache_ttl_sec=int(data.get("cache_ttl_sec",3600)),
            exclude_leveraged=bool(data.get("exclude_leveraged",True)),
        )
        cfg._config_path = data.get("_config_path", DEFAULT_CONFIG_PATH)
        if not cfg.telegram_token:
            cfg.telegram_token = os.environ.get("TELEGRAM_BOT_TOKEN","")
        if not cfg.telegram_chat_id:
            cfg.telegram_chat_id = os.environ.get("TELEGRAM_CHAT_ID","")
        return cfg

    def save(self):
        with open(self._config_path,"w",encoding="utf-8") as f:
            json.dump(self.to_json(),f,ensure_ascii=False,indent=2)

    @staticmethod
    def load(path: str = DEFAULT_CONFIG_PATH)->"Config":
        if os.path.exists(path):
            try:
                with open(path,"r",encoding="utf-8") as f:
                    data = json.load(f)
                cfg = Config.from_json(data)
                cfg._config_path = path
                return cfg
            except Exception:
                print("[WARN] Failed to load config; using defaults.")
        cfg = Config()
        cfg._config_path = path
        cfg.telegram_token = os.environ.get("TELEGRAM_BOT_TOKEN","")
        cfg.telegram_chat_id = os.environ.get("TELEGRAM_CHAT_ID","")
        return cfg

# -------------- Utils -------------- #
def now_bkk_str()->str:
    return datetime.now(TZ_BANGKOK).strftime("%Y-%m-%d %H:%M:%S")

def fmt_price(p: float)->str:
    if p < 0.0001: return f"{p:.8f}"
    if p < 0.01: return f"{p:.6f}"
    if p < 1: return f"{p:.5f}"
    if p < 10: return f"{p:.4f}"
    if p < 100: return f"{p:.3f}"
    return f"{p:.2f}"

def fmt_money(x: float)->str:
    ax = abs(x)
    if ax >= 1_000_000_000: return f"{x/1_000_000_000:.2f}B"
    if ax >= 1_000_000: return f"{x/1_000_000:.2f}M"
    if ax >= 1_000: return f"{x/1_000:.2f}K"
    return f"{x:.2f}"

def pct(a: float,b: float)->float:
    if b==0: return 0.0
    return 100.0*(a-b)/b

# -------------- Technicals -------------- #
def ema(series: List[float], period: int)->List[float]:
    if period<=0 or not series: return [math.nan]*len(series)
    k = 2/(period+1)
    out=[math.nan]*len(series)
    s = series[0]; out[0]=s
    for i in range(1,len(series)):
        s = series[i]*k + s*(1-k)
        out[i]=s
    return out

def rsi(series: List[float], period: int)->List[float]:
    n=len(series)
    if period<=0 or n<period+1: return [math.nan]*n
    deltas=[series[i]-series[i-1] for i in range(1,n)]
    gains=[max(d,0.0) for d in deltas]
    losses=[max(-d,0.0) for d in deltas]
    avg_gain=sum(gains[:period])/period
    avg_loss=sum(losses[:period])/period
    res=[math.nan]*(period+1)
    def _r(g,l):
        if l==0: return 100.0
        rs=g/l; return 100 - (100/(1+rs))
    res.append(_r(avg_gain,avg_loss))
    for i in range(period,len(deltas)):
        avg_gain=(avg_gain*(period-1)+gains[i])/period
        avg_loss=(avg_loss*(period-1)+losses[i])/period
        res.append(_r(avg_gain,avg_loss))
    return res

def true_range(h,l,pc): return max(h-l, abs(h-pc), abs(l-pc))

def atr(highs: List[float], lows: List[float], closes: List[float], period: int)->List[float]:
    n=len(closes)
    if period<=0 or n<period+1: return [math.nan]*n
    trs=[true_range(highs[0],lows[0],closes[0])]
    for i in range(1,n):
        trs.append(true_range(highs[i],lows[i],closes[i-1]))
    out=[math.nan]*period
    prev=sum(trs[1:period+1])/period
    out.append(prev)
    for i in range(period+1,len(trs)):
        prev=(prev*(period-1)+trs[i])/period
        out.append(prev)
    need=n-len(out)
    if need>0: out=[math.nan]*need + out
    return out

def dmi_adx(highs: List[float], lows: List[float], closes: List[float], period: int)->Tuple[List[float],List[float],List[float]]:
    n=len(closes)
    if n<period+2: return [math.nan]*n,[math.nan]*n,[math.nan]*n
    plus_dm=[0.0]*n; minus_dm=[0.0]*n; tr=[0.0]*n
    for i in range(1,n):
        up=highs[i]-highs[i-1]
        dn=lows[i-1]-lows[i]
        plus_dm[i]=up if (up>dn and up>0) else 0.0
        minus_dm[i]=dn if (dn>up and dn>0) else 0.0
        tr[i]=true_range(highs[i],lows[i],closes[i-1])
    def wilder(vals: List[float])->List[float]:
        out=[math.nan]*n
        sm=sum(vals[1:period+1])
        out[period]=sm
        for i in range(period+1,n):
            sm = sm - (sm/period) + vals[i]
            out[i]=sm
        return out
    tr_s=wilder(tr); plus_s=wilder(plus_dm); minus_s=wilder(minus_dm)
    dip=[math.nan]*n; dim=[math.nan]*n; dx=[math.nan]*n
    for i in range(period,n):
        if tr_s[i] and tr_s[i]>0:
            dip[i]=100.0*(plus_s[i]/tr_s[i])
            dim[i]=100.0*(minus_s[i]/tr_s[i])
            s=dip[i]+dim[i]
            if s>0:
                dx[i]=100.0*abs(dip[i]-dim[i])/s
    adx=[math.nan]*n
    if n>2*period:
        # seed
        vs=[d for d in dx[period:2*period+1] if not math.isnan(d)]
        seed=sum(vs)/len(vs) if vs else math.nan
        adx[2*period]=seed
        for i in range(2*period+1,n):
            prev=adx[i-1] if not math.isnan(adx[i-1]) else seed
            adx[i]=(prev*(period-1) + (dx[i] if not math.isnan(dx[i]) else 0.0))/period
    return dip,dim,adx

def sma(series: List[float], period: int)->List[float]:
    n=len(series)
    if period<=0 or n==0: return [math.nan]*n
    out=[math.nan]*n; csum=0.0
    for i in range(n):
        csum += series[i]
        if i>=period: csum -= series[i-period]
        if i>=period-1: out[i]=csum/period
    return out

def session_vwap(highs,lows,closes,vols,close_times_ms)->List[float]:
    n=len(closes); vwap=[math.nan]*n
    if n==0: return vwap
    cur_day=None; pv=0.0; vv=0.0
    for i in range(n):
        dt=datetime.fromtimestamp(close_times_ms[i]/1000.0,tz=timezone.utc).date()
        price=(highs[i]+lows[i]+closes[i])/3.0
        if cur_day is None or dt!=cur_day:
            cur_day=dt; pv=0.0; vv=0.0
        pv += price*vols[i]; vv += vols[i]
        vwap[i]= pv/vv if vv>0 else math.nan
    return vwap

# -------------- HTTP / REST -------------- #
_S = requests.Session()
_S.headers.update({"User-Agent":"binance-scanner/1.2"})
_HTTP_RETRIES=3; _HTTP_BACKOFF=0.4

def _http_get(url, params=None, timeout=15):
    last=None
    for a in range(_HTTP_RETRIES):
        try:
            r=_S.get(url, params=params, timeout=timeout)
            if r.status_code==200: return r.json()
            last=f"HTTP {r.status_code} {r.text[:200]}"
        except Exception as e:
            last=str(e)
        time.sleep(_HTTP_BACKOFF*(a+1))
    raise RuntimeError(f"GET failed: {url} ({last})")

def _http_post(url, json_payload, timeout=10):
    last=None
    for a in range(_HTTP_RETRIES):
        try:
            r=_S.post(url, json=json_payload, timeout=timeout)
            if r.status_code==200: return r.json()
            last=f"HTTP {r.status_code} {r.text[:200]}"
        except Exception as e:
            last=str(e)
        time.sleep(_HTTP_BACKOFF*(a+1))
    raise RuntimeError(f"POST failed: {url} ({last})")

_CACHE={"exchange_info":{"ts":0.0,"data":None},
        "usdt_symbols":{"ts":0.0,"data":None},
        "symbol_filters":{}}

def get_exchange_info(cfg: Config)->dict:
    now=time.time()
    c=_CACHE["exchange_info"]
    if c["data"] is not None and (now-c["ts"]<cfg.cache_ttl_sec): return c["data"]
    data=_http_get(f"{BINANCE_API}/api/v3/exchangeInfo", timeout=10)
    _CACHE["exchange_info"]={"ts":now,"data":data}
    _CACHE["symbol_filters"].clear()
    return data

def list_usdt_symbols(cfg: Config)->List[str]:
    now=time.time(); c=_CACHE["usdt_symbols"]
    if c["data"] is not None and (now-c["ts"]<cfg.cache_ttl_sec): return c["data"]
    info=get_exchange_info(cfg); out=[]
    for s in info.get("symbols",[]):
        if s.get("status")=="TRADING" and s.get("quoteAsset")=="USDT" and s.get("isSpotTradingAllowed",True):
            sym=s.get("symbol","")
            if cfg.exclude_leveraged:
                bad=("UPUSDT","DOWNUSDT","BULL","BEAR","3L","3S","4L","4S","5L","5S")
                if any(b in sym for b in bad): continue
            out.append(sym)
    out.sort(); _CACHE["usdt_symbols"]={"ts":now,"data":out}; return out

def get_24h_tickers()->List[dict]:
    data=_http_get(f"{BINANCE_API}/api/v3/ticker/24hr", timeout=20)
    if isinstance(data,dict): return [data]
    return data

def get_24h_qv_map_usdt(cfg: Config)->Dict[str,float]:
    us=set(list_usdt_symbols(cfg)); rows={}
    time.sleep(0.1)
    for t in get_24h_tickers():
        sym=t.get("symbol")
        if sym in us:
            rows[sym]=float(t.get("quoteVolume",0.0))
    return rows

def get_klines(symbol: str, interval: str, limit: int=200)->List[List]:
    return _http_get(f"{BINANCE_API}/api/v3/klines", params={"symbol":symbol,"interval":interval,"limit":limit}, timeout=15)

def _filters_for_symbol(symbol: str, cfg: Config)->dict:
    if symbol in _CACHE["symbol_filters"]: return _CACHE["symbol_filters"][symbol]
    info=get_exchange_info(cfg); f={}
    for s in info.get("symbols",[]):
        if s.get("symbol")==symbol:
            for fil in s.get("filters",[]): f[fil.get("filterType")]=fil
            break
    _CACHE["symbol_filters"][symbol]=f; return f

def _decimals_from_step(step_str: str)->int:
    if '.' not in step_str: return 0
    return len(step_str.split('.',1)[1].rstrip('0'))

def round_to_tick(val: float, symbol: str, cfg: Config, mode: str="nearest")->float:
    f=_filters_for_symbol(symbol,cfg).get("PRICE_FILTER",{})
    tick=float(f.get("tickSize","0.00000001"))
    if tick<=0: return val
    dec=_decimals_from_step(f.get("tickSize","0.00000001"))
    if mode=="down": q=math.floor(val/tick)*tick
    elif mode=="up": q=math.ceil(val/tick)*tick
    else: q=round(val/tick)*tick
    return float(f"{q:.{dec}f}")

# -------------- Signals -------------- #
class Signal:
    def __init__(self, symbol, entry, t1, t2, t3, stop, candle_close_ms, mode_used, bar_qv, bar_qv_ma, day_qv):
        self.symbol=symbol; self.entry=entry; self.t1=t1; self.t2=t2; self.t3=t3; self.stop=stop
        self.candle_close_ms=candle_close_ms; self.mode_used=mode_used
        self.bar_qv=bar_qv; self.bar_qv_ma=bar_qv_ma; self.day_qv=day_qv

def evaluate_signal_for_symbol(symbol: str, cfg: Config, day_qv_map: Optional[Dict[str,float]]=None)->Optional[Signal]:
    s=cfg.settings; e=cfg.early
    try: kl=get_klines(symbol, s.timeframe, limit=max(240, s.min_candles))
    except Exception: return None
    opens,highs,lows,closes,ctimes,vols,qvols=[],[],[],[],[],[],[]
    for row in kl:
        opens.append(float(row[1])); highs.append(float(row[2])); lows.append(float(row[3]))
        closes.append(float(row[4])); vols.append(float(row[5])); qvols.append(float(row[7]))
        ctimes.append(int(row[6]))
    n=len(closes)
    if n < max(s.ema_slow,s.rsi_period,s.atr_period,s.breakout_lookback,s.adx_period,s.vol_ma_period)+10: return None
    now_ms=int(time.time()*1000); i=n-1; last_close=ctimes[-1]
    if now_ms < last_close:
        remaining = last_close - now_ms
        if (not e.use_partial_candle) or (remaining > e.partial_grace_sec*1000):
            i -= 1
    if i<2: return None

    ema_fast=ema(closes,s.ema_fast); ema_slow=ema(closes,s.ema_slow)
    rsi_vals=rsi(closes,s.rsi_period); atr_vals=atr(highs,lows,closes,s.atr_period)
    dip,dim,adx_vals=dmi_adx(highs,lows,closes,s.adx_period)
    vwap_vals=session_vwap(highs,lows,closes,vols,ctimes); qv_ma=sma(qvols,s.vol_ma_period)

    close_i=closes[i]; atr_i=atr_vals[i]
    if math.isnan(atr_i) or atr_i<=0: return None
    atr_pct=atr_i/close_i
    if (s.atr_pct_min and atr_pct<s.atr_pct_min) or (s.atr_pct_max and atr_pct>s.atr_pct_max): return None

    trend_up = ema_fast[i] > ema_slow[i]
    cross_up = ema_fast[i-1] <= ema_slow[i-1] and ema_fast[i] > ema_slow[i]
    rsi_ok = rsi_vals[i] >= s.rsi_min
    recent_high = max(highs[max(0,i-s.breakout_lookback):i])
    breakout = close_i > recent_high
    if e.pre_breakout_enable and not breakout:
        if highs[i] > recent_high and closes[i] > ema_fast[i]:
            breakout=True
    adx_ok = (not math.isnan(adx_vals[i])) and adx_vals[i] >= s.adx_min
    di_up = (not math.isnan(dip[i]) and not math.isnan(dim[i]) and dip[i] > dim[i])
    vwap_i=vwap_vals[i]; near_vwap=(not math.isnan(vwap_i)) and abs(close_i-vwap_i) <= s.vwap_dist_atr*atr_i and close_i>=vwap_i
    bullish = closes[i] > opens[i]

    bar_qv=qvols[i]; bar_qv_ma=qv_ma[i]
    if bar_qv is None or math.isnan(bar_qv) or bar_qv < s.min_bar_qv_usdt: return None
    spike_ok=True
    if not math.isnan(bar_qv_ma) and bar_qv_ma>0:
        spike_ok = (bar_qv >= s.vol_spike_mult*bar_qv_ma)
    day_qv=float(day_qv_map.get(symbol,0.0)) if day_qv_map else 0.0
    if day_qv < s.min_24h_qv_usdt: return None

    mode_to_use=s.mode
    if mode_to_use=="auto":
        if adx_ok and di_up and trend_up and (breakout or cross_up) and spike_ok and rsi_ok:
            mode_to_use="breakout"
        else:
            mode_to_use="pullback"

    signal_ok=False
    if mode_to_use=="breakout":
        signal_ok = trend_up and (cross_up or breakout) and rsi_ok and spike_ok
    elif mode_to_use=="pullback":
        signal_ok = trend_up and rsi_vals[i]>=50 and near_vwap and bullish
    if not signal_ok: return None

    entry=close_i; stop = entry - s.stop_atr_mult*atr_i
    if stop>=entry: return None
    R=entry-stop; t1=entry+1.0*R; t2=entry+1.5*R; t3=entry+s.rr_target*R
    entry=round_to_tick(entry,symbol,cfg); stop=round_to_tick(stop,symbol,cfg,"down")
    t1=round_to_tick(t1,symbol,cfg,"up"); t2=round_to_tick(t2,symbol,cfg,"up"); t3=round_to_tick(t3,symbol,cfg,"up")
    return Signal(symbol,entry,t1,t2,t3,stop,ctimes[i],mode_to_use,bar_qv, (bar_qv_ma if not math.isnan(bar_qv_ma) else 0.0), day_qv)

# -------------- Telegram -------------- #
def tg_send(token: str, chat_id: str, text: str)->bool:
    if not token or not chat_id: return False
    url=f"https://api.telegram.org/bot{token}/sendMessage"
    payload={"chat_id":chat_id,"text":text,"parse_mode":"HTML","disable_web_page_preview":True}
    try:
        _http_post(url,payload,timeout=10); return True
    except Exception: return False

# -------------- Scanner -------------- #
class Scanner:
    def __init__(self,cfg: Config):
        self.cfg=cfg; self._thread=None; self._stop=threading.Event(); self._online=False
        self._last_alert_bar={}; self._lock=threading.Lock()
        self._errlog_path="scanner_errors.log"; self._siglog_path="signals_log.jsonl"

    def is_online(self)->bool: return self._online and self._thread is not None and self._thread.is_alive()

    def start(self):
        if self.is_online(): return
        self._stop.clear(); self._thread=threading.Thread(target=self._run_loop,daemon=True); self._thread.start()

    def stop(self):
        if not self.is_online(): return
        self._stop.set(); self._thread.join(timeout=10)

    def _run_loop(self):
        self._online=True; tg_send(self.cfg.telegram_token,self.cfg.telegram_chat_id,"Start scanning....")
        print(f"[{now_bkk_str()}] Start scanning loop (every {self.cfg.timescan_minutes} min)")
        try:
            while not self._stop.is_set():
                t0=time.time(); self._do_one_round()
                wait=max(1.0, self.cfg.timescan_minutes*60 - (time.time()-t0))
                for _ in range(int(wait)):
                    if self._stop.is_set(): break
                    time.sleep(1)
        finally:
            self._online=False; tg_send(self.cfg.telegram_token,self.cfg.telegram_chat_id,"Stop scanning....")

    def _symbols_to_scan(self,cfg: Config):
        syms=list_usdt_symbols(cfg); qv_map=get_24h_qv_map_usdt(cfg)
        arr=[(s,qv_map.get(s,0.0)) for s in syms if qv_map.get(s,0.0)>=cfg.settings.min_24h_qv_usdt]
        arr.sort(key=lambda x:x[1], reverse=True)
        top=[s for s,_ in arr[:min(cfg.top_vol_limit,120)]]
        return top,qv_map

    def _log_err(self,line:str):
        try:
            with open(self._errlog_path,"a",encoding="utf-8") as f:
                f.write(f"[{now_bkk_str()}] {line}\n")
        except Exception: pass

    def _log_signal_json(self,sig: Signal):
        row={"ts_bkk":now_bkk_str(),"symbol":sig.symbol,"entry":sig.entry,"t1":sig.t1,"t2":sig.t2,"t3":sig.t3,
             "stop":sig.stop,"mode":sig.mode_used,"bar_qv":sig.bar_qv,"bar_qv_ma":sig.bar_qv_ma,"day_qv":sig.day_qv,
             "candle_close_ms":sig.candle_close_ms}
        try:
            with open(self._siglog_path,"a",encoding="utf-8") as f: f.write(json.dumps(row,ensure_ascii=False)+"\n")
        except Exception as e: self._log_err(f"signal log write failed: {e}")

    def _alert(self,sig: Signal):
        dt=now_bkk_str()
        if sig.bar_qv_ma and sig.bar_qv_ma>0:
            ratio=sig.bar_qv/sig.bar_qv_ma
            vol_line=f"📊Vol(bar): {fmt_money(sig.bar_qv)} USDT (MA{self.cfg.settings.vol_ma_period}×{ratio:.2f})\n"
        else:
            vol_line=f"📊Vol(bar): {fmt_money(sig.bar_qv)} USDT\n"
        day_line=f"💧Vol(24h): {fmt_money(sig.day_qv)} USDT\n" if sig.day_qv and sig.day_qv>0 else ""
        mode_line=f"⚙️Mode: {sig.mode_used}\n"
        p1=pct(sig.t1,sig.entry); p2=pct(sig.t2,sig.entry); p3=pct(sig.t3,sig.entry)
        msg=(f"🔔BUY SIGNAL วันที่/เวลา: {dt}\n"
             f"🪙เหรียญ <b>{sig.symbol}</b>\n"
             f"🟢BUY :{fmt_price(sig.entry)}\n"
             f"🟡SELL :{fmt_price(sig.t3)}\n"
             f"🎯T1: {fmt_price(sig.t1)} ({p1:+.2f}%)\n"
             f"🎯T2: {fmt_price(sig.t2)} ({p2:+.2f}%)\n"
             f"🎯T3: {fmt_price(sig.t3)} ({p3:+.2f}%)\n"
             f"🔴STOP LOSS  :{fmt_price(sig.stop)}\n"
             f"{vol_line}{day_line}{mode_line}")
        ok=tg_send(self.cfg.telegram_token,self.cfg.telegram_chat_id,msg)
        if ok and self.cfg.log_signals: self._log_signal_json(sig)

    def _maybe_alert(self,sig: Optional[Signal]):
        if not sig: return
        with self._lock:
            last=self._last_alert_bar.get(sig.symbol)
            if last==sig.candle_close_ms: return
            self._last_alert_bar[sig.symbol]=sig.candle_close_ms
        self._alert(sig)

    def _do_one_round(self):
        cfg=self.cfg
        try:
            symbols,qv_map=self._symbols_to_scan(cfg)
        except Exception as e:
            self._log_err(f"symbols_to_scan failed: {e}"); return
        print(f"[{now_bkk_str()}] Scanning {len(symbols)} symbols ({cfg.settings.timeframe})...")
        def work(sym):
            try:
                sig=evaluate_signal_for_symbol(sym,cfg,day_qv_map=qv_map)
                time.sleep(cfg.rate_limit_sleep)
                return sym,sig,None
            except Exception as ex:
                return sym,None,str(ex)
        if cfg.workers and cfg.workers>1:
            with ThreadPoolExecutor(max_workers=cfg.workers) as ex:
                futs={ex.submit(work,s):s for s in symbols}
                for fut in as_completed(futs):
                    sym,sig,err=fut.result()
                    if err: self._log_err(f"{sym}: {err}"); continue
                    self._maybe_alert(sig)
        else:
            for sym in symbols:
                sym,sig,err=work(sym)
                if err: self._log_err(f"{sym}: {err}"); continue
                self._maybe_alert(sig)

# -------------- Menu UI -------------- #
def show_menu(cfg: Config, scanner: Scanner):
    print("\n=== binance_scanner_v1_2 ===")
    print("1) TimeScan       - Set scan interval in minutes [default: %d]" % cfg.timescan_minutes)
    print("2) TOP_VOL        - Number of symbols to scan (max 120) [default: %d]" % cfg.top_vol_limit)
    print("3) Setting        - Configure signal params + Early Signal options")
    print("4) Status         - Show Online/Offline status")
    print("5) Start scanning - Start the scanner thread")
    print("6) Stop scanning  - Stop the scanner thread")
    print("7) Exit           - Exit program")

def menu_loop(cfg: Config):
    scanner=Scanner(cfg)
    def handle_sigint(signum,frame):
        if scanner.is_online(): scanner.stop()
        print("\n[INFO] Exiting..."); sys.exit(0)
    signal.signal(signal.SIGINT,handle_sigint)

    while True:
        show_menu(cfg,scanner)
        choice=input("Select menu (1-7): ").strip()
        if choice=="1":
            try:
                val=int(input("Minutes (e.g. 5): ").strip())
                if val<1: print("[ERR] Must be >= 1 minute")
                else:
                    cfg.timescan_minutes=val; cfg.save(); print("[OK] Saved.")
            except ValueError: print("[ERR] Please enter a number")
        elif choice=="2":
            try:
                val=int(input("Symbols count (max 120): ").strip())
                if val<1: print("[ERR] Must be >= 1")
                else:
                    cfg.top_vol_limit=min(val,120); cfg.save(); print("[OK] Saved.")
            except ValueError: print("[ERR] Please enter a number")
        elif choice=="3":
            edit_settings(cfg)
        elif choice=="4":
            print(f"Status: {'Online' if scanner.is_online() else 'Offline'}  | Time: {now_bkk_str()}")
        elif choice=="5":
            if not cfg.telegram_token or not cfg.telegram_chat_id:
                print("[WARN] Telegram token/chat_id not set (Menu 3 -> Telegram)")
            scanner.start()
        elif choice=="6":
            scanner.stop()
        elif choice=="7":
            if scanner.is_online(): scanner.stop()
            print("Bye 👋"); break
        else:
            print("[ERR] Choose 1-7 only")

def edit_settings(cfg: Config):
    s=cfg.settings; e=cfg.early
    while True:
        print("\n--- Setting ---")
        print(f"Mode              : {s.mode}  [breakout/pullback/auto]")
        print(f"TF (timeframe)    : {s.timeframe}  [options: {', '.join(ALLOWED_INTERVALS)}]")
        print(f"EMA fast/slow     : {s.ema_fast}/{s.ema_slow}")
        print(f"RSI period/min    : {s.rsi_period}/{s.rsi_min}")
        print(f"ATR period/mul    : {s.atr_period}/{s.stop_atr_mult}")
        print(f"Target RR         : {s.rr_target}")
        print(f"Breakout lookback : {s.breakout_lookback}")
        print(f"Min candles       : {s.min_candles}")
        print(f"ADX period/min    : {s.adx_period}/{s.adx_min}")
        print(f"VWAP dist (ATR)   : {s.vwap_dist_atr}")
        print(f"Min 24h QV (USDT) : {int(s.min_24h_qv_usdt)}")
        print(f"Min bar QV (USDT) : {int(s.min_bar_qv_usdt)}")
        print(f"Vol MA / Spike x  : {s.vol_ma_period} / {s.vol_spike_mult}")
        print(f"ATR%% min/max     : {s.atr_pct_min:.4f} / {s.atr_pct_max:.2f}")
        print(f"Telegram token    : {'SET' if cfg.telegram_token else 'NOT SET'}")
        print(f"Telegram chat     : {cfg.telegram_chat_id or 'NOT SET'}")
        print("--- Early Signal ---")
        print(f"Use partial candle : {e.use_partial_candle}")
        print(f"Grace seconds      : {e.partial_grace_sec}")
        print(f"Pre-breakout       : {e.pre_breakout_enable}")
        print("A) Toggle exclude leveraged tokens (current: %s)" % ("ON" if cfg.exclude_leveraged else "OFF"))
        print("W) Worker threads (current: %d)" % cfg.workers)
        print("9) Telegram settings")
        print("0) Back to main menu")
        k=input("Choose (mode/tf/ema/rsi/atr/rr/look/min/adx/vwap/min24/minbar/vol/atrp/early/A/W/9/0): ").strip().lower()
        if k=="0":
            cfg.save(); print("[OK] Saved. Back to main menu."); return
        elif k=="mode":
            m=input("Set mode (breakout/pullback/auto): ").strip().lower()
            if m in ("breakout","pullback","auto"): s.mode=m
            else: print("[ERR] Invalid mode")
        elif k=="tf":
            tf=input(f"Set TF ({'/'.join(ALLOWED_INTERVALS)}): ").strip()
            if tf in ALLOWED_INTERVALS: s.timeframe=tf
            else: print("[ERR] Invalid TF")
        elif k=="ema":
            try:
                f=int(input("EMA fast: ").strip()); sl=int(input("EMA slow: ").strip())
                if f>=1 and sl>f: s.ema_fast, s.ema_slow = f, sl
                else: print("[ERR] slow must be > fast and fast >= 1")
            except ValueError: print("[ERR] Numbers only")
        elif k=="rsi":
            try:
                p=int(input("RSI period: ").strip()); m=float(input("RSI min (e.g. 55): ").strip())
                if p>=2 and 0<=m<=100: s.rsi_period, s.rsi_min = p, m
                else: print("[ERR] Invalid values")
            except ValueError: print("[ERR] Numbers only")
        elif k=="atr":
            try:
                p=int(input("ATR period: ").strip()); mul=float(input("Stop ATR mult (e.g. 1.3): ").strip())
                if p>=1 and mul>0: s.atr_period, s.stop_atr_mult = p, mul
                else: print("[ERR] Invalid values")
            except ValueError: print("[ERR] Numbers only")
        elif k=="rr":
            try:
                rr=float(input("Target RR (e.g. 2.0): ").strip())
                if rr>0: s.rr_target=rr
                else: print("[ERR] Must be > 0")
            except ValueError: print("[ERR] Numbers only")
        elif k=="look":
            try:
                n=int(input("Breakout lookback (e.g. 20): ").strip())
                if n>=2: s.breakout_lookback=n
                else: print("[ERR] Must be >= 2")
            except ValueError: print("[ERR] Numbers only")
        elif k=="min":
            try:
                n=int(input("Min candles to fetch (>=100): ").strip())
                if n>=100: s.min_candles=n
                else: print("[ERR] Must be >= 100")
            except ValueError: print("[ERR] Numbers only")
        elif k=="adx":
            try:
                p=int(input("ADX period (e.g. 14): ").strip()); amin=float(input("ADX min (e.g. 25): ").strip())
                if p>=5 and amin>=0: s.adx_period, s.adx_min = p, amin
                else: print("[ERR] Invalid values")
            except ValueError: print("[ERR] Numbers only")
        elif k=="vwap":
            try:
                dist=float(input("VWAP distance in ATR (e.g. 0.4): ").strip())
                if dist>0: s.vwap_dist_atr=dist
                else: print("[ERR] Must be > 0")
            except ValueError: print("[ERR] Numbers only")
        elif k=="min24":
            try:
                val=float(input("Min 24h quote volume in USDT (e.g. 100000000): ").strip())
                if val>=0: s.min_24h_qv_usdt=val
                else: print("[ERR] Must be >= 0")
            except ValueError: print("[ERR] Numbers only")
        elif k=="minbar":
            try:
                val=float(input("Min per-candle quote volume in USDT (e.g. 2000000): ").strip())
                if val>=0: s.min_bar_qv_usdt=val
                else: print("[ERR] Must be >= 0")
            except ValueError: print("[ERR] Numbers only")
        elif k=="vol":
            try:
                p=int(input("Volume MA period (e.g. 20): ").strip()); m=float(input("Spike multiplier (e.g. 1.2): ").strip())
                if p>=2 and m>0: s.vol_ma_period, s.vol_spike_mult = p, m
                else: print("[ERR] Invalid values")
            except ValueError: print("[ERR] Numbers only")
        elif k=="atrp":
            try:
                amin=float(input("ATR%% min (e.g. 0.002 -> 0.2%): ").strip()); amax=float(input("ATR%% max (e.g. 0.15 -> 15%): ").strip())
                if amin>=0 and amax>0 and amax>amin: s.atr_pct_min, s.atr_pct_max = amin, amax
                else: print("[ERR] Invalid values")
            except ValueError: print("[ERR] Numbers only")
        elif k=="9":
            token=input("Telegram BOT token: ").strip(); chat=input("Telegram chat_id: ").strip()
            if token: cfg.telegram_token=token
            if chat: cfg.telegram_chat_id=chat
        elif k=="a":
            cfg.exclude_leveraged = not cfg.exclude_leveraged; print(f"exclude_leveraged = {cfg.exclude_leveraged}")
        elif k=="w":
            try:
                w=int(input("Worker threads (1-16): ").strip())
                if 1<=w<=16: cfg.workers=w
                else: print("[ERR] 1..16 only")
            except ValueError: print("[ERR] Numbers only")
        elif k=="early":
            sub=input("Early menu (toggle/grace/pre): ").strip().lower()
            if sub=="toggle":
                e.use_partial_candle = not e.use_partial_candle
                print(f"use_partial_candle = {e.use_partial_candle}")
            elif sub=="grace":
                try:
                    sec=int(input("Grace seconds before close (e.g. 120): ").strip())
                    if sec>=0: e.partial_grace_sec=sec
                    else: print("[ERR] >=0 only")
                except ValueError: print("[ERR] Numbers only")
            elif sub=="pre":
                e.pre_breakout_enable = not e.pre_breakout_enable
                print(f"pre_breakout_enable = {e.pre_breakout_enable}")
            else:
                print("Options: toggle/grace/pre")
        else:
            print("[ERR] Unknown option")

# -------------- Entry -------------- #
def main():
    parser=argparse.ArgumentParser(description="binance_scanner_v1_2")
    parser.add_argument("--autostart",action="store_true")
    parser.add_argument("--minutes",type=int)
    parser.add_argument("--top",type=int)
    parser.add_argument("--tf",choices=ALLOWED_INTERVALS)
    parser.add_argument("--mode",choices=["breakout","pullback","auto"])
    args=parser.parse_args()

    cfg=Config.load(DEFAULT_CONFIG_PATH)
    if args.minutes: cfg.timescan_minutes=max(1,args.minutes)
    if args.top: cfg.top_vol_limit=min(max(1,args.top),120)
    if args.tf: cfg.settings.timeframe=args.tf
    if args.mode: cfg.settings.mode=args.mode
    cfg.save()

    if args.autostart:
        scanner=Scanner(cfg)
        def handle_sigint(signum,frame):
            if scanner.is_online(): scanner.stop()
            print("\n[INFO] Exiting..."); sys.exit(0)
        signal.signal(signal.SIGINT,handle_sigint); signal.signal(signal.SIGTERM,handle_sigint)
        scanner.start()
        try:
            while True: time.sleep(1)
        except KeyboardInterrupt:
            handle_sigint(None,None)
    else:
        print("Loaded config from:", cfg._config_path)
        menu_loop(cfg)

if __name__=="__main__":
    main()
