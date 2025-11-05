# app_definedge_sdk_masterless_trading.py
import io, zipfile, requests, datetime as dt, threading, json, time
import pandas as pd
import numpy as np
import streamlit as st
from queue import Queue

# Definedge SDK
# Note: The package is installed as 'pyintegrate', but the module is imported as 'integrate'.
from integrate import ConnectToIntegrate, IntegrateWebSocket, IntegrateOrders, IntegrateData

st.set_page_config(page_title="P&F Options (Definedge SDK)", layout="wide")
st.title("NIFTY/BANKNIFTY P&F Options — Realtime Features & Trading (Definedge SDK)")

# ----------------- Constants -----------------
MASTER_URL = "https://app.definedgesecurities.com/public/nsefno.zip"
INDEXES = ["NIFTY", "BANKNIFTY"]
LOT_SIZE = {"NIFTY": 65, "BANKNIFTY": 35}

BOX_PCT = 0.005
REVERSAL_BOXES = 3
ENTRY_COLUMN_BOXES = 15
SL_BOXES = 15
TMA_PERIODS = (10, 15, 20)
MAST_ATR_PERIOD = 10
MAST_MULTIPLIER = 3.0

# Thread-safe tick queue (WS -> main thread)
TICK_QUEUE: Queue = Queue()

# ----------------- Session State -----------------
ss = st.session_state
ss.setdefault("creds", {"api_token":"", "api_secret":"", "api_session_key":""})
ss.setdefault("conn", None); ss.setdefault("iws", None); ss.setdefault("io", None)
ss.setdefault("is_connected", False)

ss.setdefault("master_df", pd.DataFrame()); ss.setdefault("master_cache_date", None)
ss.setdefault("tick_df", pd.DataFrame(columns=["ts","symbol","strike","opt_type","expiry","ltp","oi","volume","high","low"]))
ss.setdefault("positions", [])
ss.setdefault("trade_log", [])
ss.setdefault("auto_entry_enabled", False)
ss.setdefault("one_auto_order_per_index", True)

# Broker status indicator
st.markdown(f"**Broker status:** {'🟢 Connected' if ss.get('is_connected') else '🔴 Disconnected'}")

# ----------------- Indicators -----------------
def vol_gt_10pct_above_prev5(df: pd.DataFrame) -> pd.DataFrame:
    df = df.sort_values("ts").copy()
    df["session"] = pd.to_datetime(df["ts"]).dt.date
    df["vol_roll5"] = df.groupby(["symbol","strike","opt_type","expiry","session"])["volume"]\
    .transform(lambda s: s.shift(1).rolling(5, min_periods=5).mean())
    df["vol_gt10"] = df["volume"] > (1.10 * df["vol_roll5"])
    return df

def atr(df: pd.DataFrame, period=14):
    high, low, close = df["high"], df["low"], df["ltp"]
    prev_close = close.shift(1)
    tr = pd.concat([(high-low),(high-prev_close).abs(),(low-prev_close).abs()], axis=1).max(axis=1)
    return tr.rolling(period).mean()

def supertrend(df: pd.DataFrame, atr_period=10, multiplier=3.0):
    hl2 = (df["high"] + df["low"]) / 2.0
    _atr = atr(df, atr_period)
    upper = hl2 + multiplier * _atr
    lower = hl2 - multiplier * _atr
    st_dir = pd.Series(index=df.index, dtype=int)
    st_line = pd.Series(index=df.index, dtype=float)
    st_dir.iloc[0] = 1; st_line.iloc[0] = lower.iloc[0]
    for i in range(1, len(df)):
        prev_dir = st_dir.iloc[i-1]; prev_line = st_line.iloc[i-1]
        cur_upper = upper.iloc[i]; cur_lower = lower.iloc[i]
        px = df["ltp"].iloc[i]
        if prev_dir == 1:
            st_line.iloc[i] = max(cur_lower, prev_line) if px >= prev_line else cur_upper
            st_dir.iloc[i] = 1 if px >= prev_line else -1
        else:
            st_line.iloc[i] = min(cur_upper, prev_line) if px <= prev_line else cur_lower
            st_dir.iloc[i] = -1 if px <= prev_line else 1
    return st_line, st_dir

def mast_flags(df_in: pd.DataFrame, ma_period=10, atr_period=10, mult=3.0):
    df = df_in.copy()
    df["ma"] = df["ltp"].rolling(ma_period).mean()
    st_line, st_dir = supertrend(df, atr_period, mult)
    df["st_line"] = st_line
    df["mast_above"] = df["ma"] > df["st_line"]
    df["mast_below"] = df["ma"] < df["st_line"]
    df["mast_cross_up"] = (df["ma"].shift(1) <= df["st_line"].shift(1)) & (df["ma"] > df["st_line"])
    df["mast_cross_dn"] = (df["ma"].shift(1) >= df["st_line"].shift(1)) & (df["ma"] < df["st_line"])
    return df

def classify_oi_build_up(df: pd.DataFrame) -> pd.DataFrame:
    df = df.sort_values("ts").copy()
    df["price_chg"] = df.groupby(["symbol","strike","opt_type","expiry"])["ltp"].diff()
    df["oi_chg"] = df.groupby(["symbol","strike","opt_type","expiry"])["oi"].diff()
    def label(row):
        if pd.isna(row["price_chg"]) or pd.isna(row["oi_chg"]): return None
        up = row["price_chg"] > 0; oiup = row["oi_chg"] > 0
        if up and oiup: return "long_buildup"
        if (not up) and oiup: return "short_buildup"
        if up and (not oiup): return "short_covering"
        if (not up) and (not oiup): return "long_unwinding"
        return None
    df["oi_phase"] = df.apply(label, axis=1)
    return df

def pnf_current_col_and_len(close_series, box_pct=0.005, reversal_boxes=3):
    if close_series is None or len(close_series) < 2: return None, 0
    prices = np.asarray(close_series, dtype=float)
    col = None; col_start = prices[0]; extreme = prices[0]
    for p in prices[1:]:
        box = p * box_pct
        if col is None:
            if p >= extreme + box: col = 'X'; col_start = extreme; extreme = p
            elif p <= extreme - box: col = 'O'; col_start = extreme; extreme = p
            else: continue
        elif col == 'X':
            if p >= extreme + box: extreme = p
            elif p <= extreme - reversal_boxes*box: col = 'O'; col_start = p; extreme = p
        else:
            if p <= extreme - box: extreme = p
            elif p >= extreme + reversal_boxes*box: col = 'X'; col_start = p; extreme = p
    last = prices[-1]; b = last * box_pct if last != 0 else 1e-9
    length = int(abs(extreme - col_start) / b)
    return (col if col else None, max(length, 0))

# ----------------- Master fetch -----------------
def download_and_filter_master() -> pd.DataFrame:
    r = requests.get(MASTER_URL, timeout=45)
    r.raise_for_status()
    z = zipfile.ZipFile(io.BytesIO(r.content))
    csv_name = next((n for n in z.namelist() if n.lower().endswith(".csv")), None)
    if not csv_name:
        raise RuntimeError("No CSV found inside nsefno.zip")

    raw = pd.read_csv(z.open(csv_name), low_memory=False)
    # If header row looks like data, re-read headerless
    if set(str(c).upper() for c in raw.columns) & {"NFO","OPTIDX","OPTSTK","FUTIDX","FUTSTK"}:
        raw = pd.read_csv(z.open(csv_name), header=None, low_memory=False)
        if raw.shape[1] == 10:
            raw.columns = ["SEGMENT","TOKEN","SYMBOL","TRADINGSYM","INSTRUMENT TYPE","EXPIRY","TICKSIZE","LOTSIZE","OPTIONTYPE","STRIKE"]
        else:
            cols = list(range(raw.shape[1]))
            raw.columns = [f"C{i}" for i in cols]
            raw["SEGMENT"] = raw.iloc[:,0]; raw["TOKEN"] = raw.iloc[:,1]
            raw["SYMBOL"] = raw.iloc[:,2]; raw["TRADINGSYM"] = raw.iloc[:,3]
            raw["INSTRUMENT TYPE"] = raw.iloc[:,4]; raw["EXPIRY"] = raw.iloc[:,5]
            raw["LOTSIZE"] = raw.iloc[:,7] if raw.shape[1] > 7 else np.nan
            raw["OPTIONTYPE"] = raw.iloc[:,8] if raw.shape[1] > 8 else ""
            raw["STRIKE"] = raw.iloc[:,9] if raw.shape[1] > 9 else np.nan
        seg, token, tsym, itype, expiry, opttp, strike, lots = \
            "SEGMENT","TOKEN","TRADINGSYM","INSTRUMENT TYPE","EXPIRY","OPTIONTYPE","STRIKE","LOTSIZE"
    else:
        def pick(colnames, candidates):
            cols_up = {c.strip().upper(): c for c in colnames}
            for cand in candidates:
                cu = cand.upper()
                if cu in cols_up: return cols_up[cu]
            for c in cols_up:
                for cand in candidates:
                    if cand.upper() in c: return cols_up[c]
            return None
        seg = pick(raw.columns, ["SEGMENT","EXCHANGE","SEGMENT NAME"])
        token = pick(raw.columns, ["TOKEN","INSTRUMENTTOKEN","SYMBOLTOKEN","TOKEN ID"])
        tsym = pick(raw.columns, ["TRADINGSYM","TRADING SYMBOL","TRADINGSYMBOL","TRD_SYM","SYMBOL NAME"])
        itype = pick(raw.columns, ["INSTRUMENT TYPE","INSTRUMENT","SECURITY TYPE","INST TYPE"])
        expiry= pick(raw.columns, ["EXPIRY","EXPIRY DATE","EXPIRYDATE","EXPIRE ON"])
        opttp = pick(raw.columns, ["OPTIONTYPE","OPT_TYPE","OPTION TYPE","OPT"])
        strike= pick(raw.columns, ["STRIKE","STRIKE PRICE","STRIKEPRICE"])
        lots = pick(raw.columns, ["LOTSIZE","LOT SIZE"])
        if any(v is None for v in [seg, token, tsym, itype, expiry, opttp, strike]):
            raise RuntimeError(f"Unexpected master CSV columns; first row sample: {raw.iloc[0].tolist()[:10]}")

    df = raw.copy()
    df = df[df[seg].astype(str).str.strip().str.upper().eq("NFO")]
    df = df[df[itype].astype(str).str.upper().str.contains("OPT", na=False)]

    ts_upper = df[tsym].astype(str).str.upper()
    df["symbol_base"] = np.where(ts_upper.str.startswith("BANKNIFTY"), "BANKNIFTY",
                                np.where(ts_upper.str.startswith("NIFTY"), "NIFTY", ""))
    df = df[df["symbol_base"].isin(INDEXES)]

    exp_raw = df[expiry].astype(str).str.strip()
    exp_dt = pd.to_datetime(exp_raw, errors="coerce", format="%d%m%Y")
    if exp_dt.isna().any():
        exp_dt = exp_dt.fillna(pd.to_datetime(exp_raw, errors="coerce", format="%d-%b-%Y"))
    if exp_dt.isna().any():
        exp_dt = exp_dt.fillna(pd.to_datetime(exp_raw, errors="coerce", dayfirst=True))
    exp_short = exp_dt.dt.strftime("%d%b").str.upper()

    out = pd.DataFrame({
        "token": df[token].astype(str),
        "tradingsymbol": df[tsym].astype(str),
        "symbol_base": df["symbol_base"].astype(str),
        "option_type": df[opttp].astype(str),
        "strike": pd.to_numeric(df[strike], errors="coerce"),
        "expiry": exp_short,
        "lot_size": pd.to_numeric(df.get(lots, np.nan), errors="coerce")
    }).dropna(subset=["strike"])
    return out

def ensure_master_cached(force=False):
    today = dt.date.today()
    if (not force) and ss.master_cache_date == today and not ss.master_df.empty:
        return
    m = download_and_filter_master()
    ss.master_df = m; ss.master_cache_date = today
    st.toast(f"Master refreshed: {len(m)} rows", icon="✅")

# ----------------- REST History helpers (Definedge SDS) -----------------
HISTORY_BASE = "https://data.definedgesecurities.com/sds/history"

def _extract_session_key(conn):
    manual = ss.creds.get('api_session_key')
    if manual: return manual
    cand_attrs = ["api_session_key","session_key","access_token","auth_token","jwt","token",
                  "_session_key","_access_token","_authToken","api_key"]
    for nm in cand_attrs:
        val = getattr(conn, nm, None)
        if isinstance(val, str) and len(val) > 10:
            return val
    for nm in ["get_session","get_tokens","dump_tokens"]:
        fn = getattr(conn, nm, None)
        if callable(fn):
            try:
                d = fn()
                if isinstance(d, dict):
                    for k in ["api_session_key","session_key","access_token","auth_token","jwt","token"]:
                        if isinstance(d.get(k), str) and len(d[k])>10:
                            return d[k]
            except Exception:
                pass
    return None

def _fmt_ist_ddmmyyyyHHMM(dt_obj: dt.datetime) -> str:
    ist = dt.timezone(dt.timedelta(hours=5, minutes=30))
    if dt_obj.tzinfo is None:
        dt_obj = dt_obj.replace(tzinfo=ist)
    else:
        dt_obj = dt_obj.astimezone(ist)
    return dt_obj.strftime("%d%m%Y%H%M")

def _rest_history_csv(session_key: str, segment: str, token: str, timeframe: str,
                      start_dt: dt.datetime, end_dt: dt.datetime) -> pd.DataFrame:
    headers = {"Authorization": session_key.strip()}
    _from = _fmt_ist_ddmmyyyyHHMM(start_dt)
    _to = _fmt_ist_ddmmyyyyHHMM(end_dt)
    url = f"{HISTORY_BASE}/{segment}/{token}/{timeframe}/{_from}/{_to}"
    r = requests.get(url, headers=headers, timeout=30)
    if not r.ok or not r.text:
        return pd.DataFrame()
    df = pd.read_csv(io.StringIO(r.text), header=None)
    if timeframe in ("day","minute"):
        if df.shape[1] >= 7:
            df = df.iloc[:, :7]
            df.columns = ["ts_raw","open","high","low","close","volume","oi"]
        elif df.shape[1] >= 5:
            df.columns = ["ts_raw","open","high","low","close"] + [f"c{i}" for i in range(df.shape[1]-5)]
            df["volume"] = pd.to_numeric(df.get("c0", 0), errors="coerce").fillna(0.0)
            df["oi"] = pd.to_numeric(df.get("c1", 0), errors="coerce").fillna(0.0)
            df = df[["ts_raw","open","high","low","close","volume","oi"]]
        else:
            return pd.DataFrame()
        df["ts"] = pd.to_datetime(df["ts_raw"], format="%d%m%Y%H%M", errors="coerce")
        out = pd.DataFrame({
            "ts": df["ts"],
            "ltp": pd.to_numeric(df["close"], errors="coerce"),
            "high": pd.to_numeric(df["high"], errors="coerce"),
            "low": pd.to_numeric(df["low"], errors="coerce"),
            "volume": pd.to_numeric(df["volume"], errors="coerce").fillna(0.0),
            "oi": pd.to_numeric(df["oi"], errors="coerce").fillna(0.0),
        }).dropna(subset=["ts","ltp"])
        return out
    # tick
    if df.shape[1] >= 4:
        df = df.iloc[:, :4]; df.columns = ["utc","ltp","ltq","oi"]
        df["ts"] = pd.to_datetime(df["utc"], unit="s", utc=True)\
            .dt.tz_convert("Asia/Kolkata").dt.tz_localize(None)
        out = pd.DataFrame({
            "ts": df["ts"],
            "ltp": pd.to_numeric(df["ltp"], errors="coerce"),
            "high": pd.to_numeric(df["ltp"], errors="coerce"),
            "low": pd.to_numeric(df["ltp"], errors="coerce"),
            "volume": pd.to_numeric(df["ltq"], errors="coerce").fillna(0.0),
            "oi": pd.to_numeric(df["oi"], errors="coerce").fillna(0.0),
        }).dropna(subset=["ts","ltp"])
        return out
    return pd.DataFrame()

# ----------------- SDK Historical helper -----------------
def fetch_prev_session_1min_sdk(conn, tradingsymbol: str, start: dt.datetime, end: dt.datetime) -> pd.DataFrame:
    """
    Fetches 1-minute historical data using the official SDK method.
    """
    try:
        ic = IntegrateData(conn)
        history = ic.historical_data(
            exchange=conn.EXCHANGE_TYPE_NFO,
            trading_symbol=tradingsymbol,
            timeframe=conn.TIMEFRAME_TYPE_MIN,
            start=start,
            end=end,
        )
        data_list = list(history)
        return normalize_hist_df(data_list)
    except Exception:
        return pd.DataFrame()

def normalize_hist_df(x):
    if x is None: return pd.DataFrame()
    if isinstance(x, pd.DataFrame): df = x.copy()
    elif isinstance(x, list): df = pd.DataFrame(x)
    elif isinstance(x, dict) and "data" in x: df = pd.DataFrame(x["data"])
    else:
        try: df = pd.DataFrame(x)
        except Exception: return pd.DataFrame()
    cols = {c.lower(): c for c in df.columns}
    def pick(*names):
        for n in names:
            if n in cols: return cols[n]
        for c in cols:
            for n in names:
                if n in c: return cols[c]
        return None
    tcol = pick("time","timestamp","ts","date","datetime")
    pcol = pick("close","lp","price","ltp","c")
    hcol = pick("high","h"); lcol = pick("low","l")
    vcol = pick("volume","vol","v"); oicol= pick("oi","openinterest","open_interest")
    if not tcol or not pcol: return pd.DataFrame()
    out = pd.DataFrame({
        "ts": pd.to_datetime(df[tcol]),
        "ltp": pd.to_numeric(df[pcol], errors="coerce"),
        "high": pd.to_numeric(df[hcol], errors="coerce") if hcol else pd.to_numeric(df[pcol], errors="coerce"),
        "low": pd.to_numeric(df[lcol], errors="coerce") if lcol else pd.to_numeric(df[pcol], errors="coerce"),
        "volume": pd.to_numeric(df[vcol], errors="coerce") if vcol else 0.0,
        "oi": pd.to_numeric(df[oicol], errors="coerce") if oicol else 0.0,
    }).dropna(subset=["ts","ltp"])
    return out

# ----------------- Features -----------------
def build_feature_frame(df_all: pd.DataFrame):
    if df_all.empty: return df_all
    df_all = df_all.copy()
    df_all["ts"] = pd.to_datetime(df_all["ts"])
    # The original resampling logic was dropping pre-aggregated historical data.
    # This new approach uses groupby with pd.Grouper to correctly aggregate live ticks
    # into 1-minute bars while preserving the already-aggregated historical bars.
    ohlc = (df_all.groupby(["symbol", "strike", "opt_type", "expiry", pd.Grouper(key="ts", freq="1min")])
            .agg(ltp=("ltp", "last"),
                 high=("high", "max"),
                 low=("low", "min"),
                 volume=("volume", "sum"),
                 oi=("oi", "last"))
            .dropna(subset=["ltp"])
            .reset_index())
    ohlc["high"] = ohlc["high"].fillna(ohlc["ltp"])
    ohlc["low"] = ohlc["low"].fillna(ohlc["ltp"])
    ohlc = ohlc.sort_values("ts")

    ohlc = vol_gt_10pct_above_prev5(ohlc)
    grp = ohlc.groupby(["symbol","strike","opt_type","expiry"], group_keys=False)
    ohlc["tma10"] = grp["ltp"].transform(lambda s: s.rolling(TMA_PERIODS[0]).mean())
    ohlc["tma15"] = grp["ltp"].transform(lambda s: s.rolling(TMA_PERIODS[1]).mean())
    ohlc["tma20"] = grp["ltp"].transform(lambda s: s.rolling(TMA_PERIODS[2]).mean())
    ohlc["above_tma"] = (ohlc["ltp"] > ohlc["tma10"]) & (ohlc["ltp"] > ohlc["tma15"]) & (ohlc["ltp"] > ohlc["tma20"])
    ohlc["below_tma"] = (ohlc["ltp"] < ohlc["tma10"]) & (ohlc["ltp"] < ohlc["tma15"]) & (ohlc["ltp"] < ohlc["tma20"])

    parts = []
    for key, g in ohlc.groupby(["symbol","strike","opt_type","expiry"], group_keys=False):
        mg = g.copy()
        mg2 = mast_flags(mg[["ltp","high","low"]].assign(ts=mg["ts"]).set_index("ts").sort_index(),
                         ma_period=MAST_ATR_PERIOD, atr_period=MAST_ATR_PERIOD, mult=MAST_MULTIPLIER)
        mg = mg.set_index("ts").join(mg2[["ma","st_line","mast_above","mast_below","mast_cross_up","mast_cross_dn"]]).reset_index()
        parts.append(mg)
    ohlc = pd.concat(parts, ignore_index=True)

    ohlc = classify_oi_build_up(ohlc)

    frames = []
    for key, g in ohlc.groupby(["symbol","strike","opt_type","expiry"], group_keys=False):
        prices = g.sort_values("ts")["ltp"]
        col, clen = pnf_current_col_and_len(prices, BOX_PCT, REVERSAL_BOXES)
        gg = g.copy(); gg["pnf_col"] = col; gg["pnf_len"] = clen
        frames.append(gg)
    ohlc = pd.concat(frames, ignore_index=True)
    return ohlc

def latest_snapshot(ohlc: pd.DataFrame) -> pd.DataFrame:
    if ohlc is None or ohlc.empty: return pd.DataFrame()
    return (ohlc.sort_values("ts").groupby(["symbol","strike","opt_type","expiry"], as_index=False).tail(1))
def rank_and_filter(latest: pd.DataFrame, index_choice: str, expiry_choice: str|None):
    if latest.empty: return {}
    latest = latest[latest["symbol"]==index_choice].copy()
    if expiry_choice and expiry_choice != "All": latest = latest[latest["expiry"]==expiry_choice]
    latest["strength"] = latest["oi"].diff().abs().fillna(0)
    cond_long = (latest["oi_phase"]=="long_buildup") & latest["vol_gt10"] & latest["mast_above"] & latest["above_tma"] & (latest["pnf_col"]=="X") & (latest["pnf_len"]>=ENTRY_COLUMN_BOXES)
    cond_short = (latest["oi_phase"]=="short_buildup") & latest["vol_gt10"] & latest["mast_below"] & latest["below_tma"] & (latest["pnf_col"]=="O") & (latest["pnf_len"]>=ENTRY_COLUMN_BOXES)
    cond_sc = (latest["oi_phase"]=="short_covering") & latest["vol_gt10"] & latest["mast_above"] & latest["above_tma"] & (latest["pnf_col"]=="X") & (latest["pnf_len"]>=ENTRY_COLUMN_BOXES)
    cond_lu = (latest["oi_phase"]=="long_unwinding") & latest["vol_gt10"] & latest["mast_below"] & latest["below_tma"] & (latest["pnf_col"]=="O") & (latest["pnf_len"]>=ENTRY_COLUMN_BOXES)
    def top(mask, n=10): return latest[mask].sort_values("strength", ascending=False).head(n)
    return {
        "short_buildup_sell": top(cond_short),
        "long_buildup_buy": top(cond_long),
        "short_covering_buy": top(cond_sc),
        "long_unwinding_sell":top(cond_lu),
    }

# ----------------- Exit Engine -----------------
def evaluate_and_exit_positions(ohlc_latest: pd.DataFrame):
    if ohlc_latest is None or ohlc_latest.empty: return
    remain = []
    for pos in ss.positions:
        mask = (
            (ohlc_latest["symbol"]==pos["symbol"]) &
            (ohlc_latest["strike"]==pos["strike"]) &
            (ohlc_latest["opt_type"]==pos["opt_type"]) &
            (ohlc_latest["expiry"]==pos["expiry"])
        )
        rowset = ohlc_latest[mask]
        if rowset.empty:
            remain.append(pos); continue
        r = rowset.iloc[0]; ltp = float(r["ltp"])
        hard_sl = pos["entry_price"] - pos["sl_points"] if pos["side"]=="BUY" else pos["entry_price"] + pos["sl_points"]
        hard_hit = (ltp <= hard_sl) if pos["side"]=="BUY" else (ltp >= hard_sl)
        trail_hit_buy = bool(r.get("below_tma", False) and r.get("mast_below", False) and r.get("mast_cross_dn", False))
        trail_hit_sell = bool(r.get("above_tma", False) and r.get("mast_above", False) and r.get("mast_cross_up", False))
        trail_hit = trail_hit_buy if pos["side"]=="BUY" else trail_hit_sell
        if hard_hit or trail_hit:
            reason = "HARD_SL" if hard_hit else "TRAIL_MAST_TMA"
            ss.trade_log.append({
                "ts": dt.datetime.now(), "action":"EXIT", "reason": reason,
                "side": pos["side"], "symbol": pos["symbol"], "strike": pos["strike"],
                "opt_type": pos["opt_type"], "expiry": pos["expiry"],
                "entry": pos["entry_price"], "exit": ltp,
                "pnl_points": (ltp - pos["entry_price"]) * (1 if pos["side"]=="BUY" else -1),
                "lots": pos["lots"], "order_id": pos.get("order_id","NA")
            })
        else:
            remain.append(pos)
    ss.positions = remain

def can_auto_open(index_symbol: str) -> bool:
    if not ss.one_auto_order_per_index: return True
    for p in ss.positions:
        if p["symbol"] == index_symbol: return False
    return True

def place_and_record(side: str, r: pd.Series, lots_int: int):
    if ss.io is None:
        st.error("Not connected (SDK)."); return
    hit = ss.master_df[(ss.master_df["symbol_base"]==r["symbol"]) &
                       (ss.master_df["strike"]==r["strike"]) &
                       (ss.master_df["option_type"]==r["opt_type"]) &
                       (ss.master_df["expiry"]==r["expiry"])]
    if hit.empty:
        st.warning("Tradingsymbol not found in master."); return
    tradingsymbol = hit.iloc[0]["tradingsymbol"]
    qty = LOT_SIZE[r["symbol"]] * lots_int
    try:
        order = ss.io.place_order(
            exchange=ss.conn.EXCHANGE_TYPE_NFO,
            order_type=ss.conn.ORDER_TYPE_BUY if side=="BUY" else ss.conn.ORDER_TYPE_SELL,
            price=0, price_type=ss.conn.PRICE_TYPE_MARKET,
            product_type=ss.conn.PRODUCT_TYPE_NORMAL,
            quantity=int(qty), tradingsymbol=tradingsymbol,
        )
        order_id = str(order)
        st.success(f"Order placed: {order_id}")
    except Exception as e:
        st.error(f"Order failed: {e}"); return
    init_sl_pts = float(r["ltp"]) * BOX_PCT * SL_BOXES
    ss.trade_log.append({
        "ts": dt.datetime.now(), "action": side,
        "symbol": r["symbol"], "strike": int(r["strike"]), "opt_type": r["opt_type"], "expiry": r["expiry"],
        "lots": lots_int, "ltp": float(r["ltp"]), "init_sl_points": init_sl_pts, "order_id": order_id
    })
    ss.positions.append({
        "ts": dt.datetime.now(), "side": side, "symbol": r["symbol"],
        "strike": int(r["strike"]), "opt_type": r["opt_type"], "expiry": r["expiry"],
        "entry_price": float(r["ltp"]), "sl_points": init_sl_pts, "lots": lots_int, "order_id": order_id
    })

# ----------------- UI: Connect + Master -----------------
with st.expander("🔌 Connect (SDK) & Master", expanded=True):
    c1, c2 = st.columns(2)
    ss.creds["api_token"] = c1.text_input("API Token", value=ss.creds.get("api_token",""))
    ss.creds["api_secret"] = c2.text_input("API Secret", value=ss.creds.get("api_secret",""), type="password")
    ss.creds["api_session_key"] = st.text_input("API Session Key (optional for REST backfill)", value=ss.creds.get("api_session_key",""))
    b1, b2, b3, b4 = st.columns(4)
    if b1.button("Connect SDK"):
        try:
            conn = ConnectToIntegrate()
            conn.login(api_token=ss.creds["api_token"], api_secret=ss.creds["api_secret"])
            ss.conn = conn; ss.io = IntegrateOrders(conn); ss.is_connected = True
            st.success("Connected to Definedge via SDK.")
            st.rerun()
        except Exception as e:
            st.error(f"SDK login failed: {e}")
    if b2.button("Refresh Master"):
        ensure_master_cached(force=True)
    autorefresh = b3.toggle("Auto-refresh master daily", value=True)
    if autorefresh: ensure_master_cached(force=False)
    ss.auto_entry_enabled = b4.toggle("Auto-entries (enforce one per index)", value=ss.auto_entry_enabled)

# ----------------- Controls -----------------
bar1, bar2, bar3, bar4 = st.columns([2,1,1,2])
index_choice = bar1.selectbox("Index", INDEXES, index=0)
lots = bar2.number_input("Lots", min_value=1, max_value=200, value=1, step=1)
toggle_ws = bar3.toggle("Start Live Feed", value=False)
expiry_options = ["All"] + (sorted(ss.master_df["expiry"].dropna().unique().tolist()) if not ss.master_df.empty else [])
expiry_choice = bar4.selectbox("Expiry", options=expiry_options, index=0)

cba, cbb = st.columns([2,2])
bf_toggle = cba.toggle("Backfill prev session OI bars when no live data", value=True)
bf_now = cbb.button("Backfill now")

# Backfill now button
if bf_now and ss.is_connected:
    # Compute prev session IST 09:15-15:30
    ist = dt.timezone(dt.timedelta(hours=5, minutes=30))
    now = dt.datetime.now(ist); prev = now - dt.timedelta(days=1)
    while prev.weekday() >= 5: prev -= dt.timedelta(days=1)
    start = prev.replace(hour=9, minute=15, second=0, microsecond=0)
    end = prev.replace(hour=15, minute=30, second=0, microsecond=0)

    # choose tokens
    st.info("Backfilling active contracts for the selected index, ignoring the expiry date choice to ensure data availability.")
    m = ss.master_df[ss.master_df["symbol_base"]==index_choice].copy()
    use = m.head(60).copy()

    landed = 0
    all_history = []
    for r in use.itertuples(index=False):
        dfh = fetch_prev_session_1min_sdk(ss.conn, str(r.tradingsymbol), start, end)
        if dfh is None or dfh.empty:
            sess_key = _extract_session_key(ss.conn)
            if sess_key:
                dfh = _rest_history_csv(sess_key, "NFO", str(r.token), "minute", start, end) # REST fallback still needs token
        if dfh is None or dfh.empty:
            continue

        dfh["symbol"] = r.symbol_base
        dfh["strike"] = float(r.strike)
        dfh["opt_type"] = r.option_type
        dfh["expiry"] = r.expiry
        all_history.append(dfh)
        landed += 1

    if all_history:
        ss.tick_df = pd.concat([ss.tick_df] + all_history, ignore_index=True)
    st.toast(f"Backfilled {landed} strikes from previous session.", icon="♻️")

# ----------------- WS handlers -----------------
def iws_on_login(iws: IntegrateWebSocket) -> None:
    tokens = getattr(iws, "_startup_tokens", [])
    if tokens:
        iws.subscribe(iws.c2i.SUBSCRIPTION_TYPE_TICK, tokens)

def iws_on_tick(iws: IntegrateWebSocket, tick: dict[str,str]) -> None:
    tk = str(tick.get("tk","")); ltp = float(tick.get("lp") or 0.0)
    v = float(tick.get("v") or 0.0); h = float(tick.get("h") or ltp); l = float(tick.get("l") or ltp)
    oi = float(tick.get("oi") or 0.0)
    ts_map = getattr(iws, "_token_map", None)
    if ts_map is None or tk not in ts_map: return
    meta = ts_map[tk]
    now = dt.datetime.now()
    TICK_QUEUE.put({
        "ts": now, "symbol": meta["symbol_base"], "strike": float(meta["strike"]),
        "opt_type": meta["option_type"], "expiry": meta["expiry"],
        "ltp": ltp, "oi": oi, "volume": v, "high": h, "low": l
    })

def iws_on_ack(iws: IntegrateWebSocket, ack: dict[str,str]) -> None: ...
def iws_on_exc(iws: IntegrateWebSocket, e: Exception) -> None: ...
def iws_on_close(iws: IntegrateWebSocket, code: int, reason: str) -> None: ...

# Start/stop WS
if toggle_ws and ss.is_connected and ss.get("iws") is None:
    if ss.master_df.empty:
        st.warning("Master is empty; refresh it first.")
    else:
        m = ss.master_df[ss.master_df["symbol_base"]==index_choice].copy()
        if expiry_choice != "All": m = m[m["expiry"]==expiry_choice]
        tokens = [(ss.conn.EXCHANGE_TYPE_NFO, t) for t in m["token"].astype(str).tolist()][:500]
        token_map = {str(r.token): {"symbol_base": r.symbol_base, "strike": float(r.strike),
                                    "option_type": r.option_type, "expiry": r.expiry}
                     for r in m.itertuples(index=False)}
        iws = IntegrateWebSocket(ss.conn)
        iws._startup_tokens = tokens; iws._token_map = token_map
        iws.on_login = iws_on_login; iws.on_tick_update = iws_on_tick
        iws.on_acknowledgement = iws_on_ack; iws.on_exception = iws_on_exc; iws.on_close = iws_on_close
        ss.iws = iws
        def _connect_ws():
            try: iws.connect(install_signal_handlers=False)
            except TypeError: iws.connect()
        threading.Thread(target=_connect_ws, name="connect", daemon=True).start()
elif (not toggle_ws) and ss.get("iws") is not None:
    try: ss.iws.close_on_exception("User toggled off")
    except Exception: pass
    ss.iws = None

# ----------------- Build / Features -----------------
st.subheader("Build / Refresh (Realtime)")
st.button("Build Features Now", on_click=lambda: None)

# Drain queue
while not TICK_QUEUE.empty():
    row = TICK_QUEUE.get()
    ss.tick_df = pd.concat([ss.tick_df, pd.DataFrame([row])], ignore_index=True)

# Auto-backfill if nothing yet
if bf_toggle and ss.is_connected and ss.tick_df.empty:
    ist = dt.timezone(dt.timedelta(hours=5, minutes=30))
    now = dt.datetime.now(ist); prev = now - dt.timedelta(days=1)
    while prev.weekday() >= 5: prev -= dt.timedelta(days=1)
    start = prev.replace(hour=9, minute=15, second=0, microsecond=0)
    end = prev.replace(hour=15, minute=30, second=0, microsecond=0)
    m = ss.master_df[ss.master_df["symbol_base"]==index_choice].copy()
    use = m.head(40).copy()
    landed = 0
    all_history = []
    for r in use.itertuples(index=False):
        dfh = fetch_prev_session_1min_sdk(ss.conn, str(r.tradingsymbol), start, end)
        if dfh is None or dfh.empty:
            sess_key = _extract_session_key(ss.conn)
            if sess_key:
                dfh = _rest_history_csv(sess_key, "NFO", str(r.token), "minute", start, end) # REST fallback still needs token
        if dfh is None or dfh.empty:
            continue

        dfh["symbol"] = r.symbol_base
        dfh["strike"] = float(r.strike)
        dfh["opt_type"] = r.option_type
        dfh["expiry"] = r.expiry
        all_history.append(dfh)
        landed += 1

    if all_history:
        ss.tick_df = pd.concat([ss.tick_df] + all_history, ignore_index=True)
    if landed: st.toast(f"Auto-backfilled {landed} strikes.", icon="♻️")

ohlc = build_feature_frame(ss.tick_df.copy())
latest = latest_snapshot(ohlc)
buckets = rank_and_filter(latest, index_choice, None if expiry_choice=="All" else expiry_choice)

# Exit checks
evaluate_and_exit_positions(latest)

# ----------------- Visuals -----------------
st.subheader("OI Bars (latest 1‑min)")
if not latest.empty:
    latest_idx = latest[latest["symbol"]==index_choice]
    if not latest_idx.empty:
        show = latest_idx[["strike","opt_type","expiry","oi","ltp"]].sort_values("oi", ascending=False).head(15)
        st.bar_chart(show.set_index(show["strike"].astype(str) + show["opt_type"])["oi"])
    else:
        st.info("No latest bars for selected index/expiry.")
else:
    st.info("No data yet. Use Backfill or wait for live.")

def render_bucket(title, df, side_default):
    st.markdown(f"### {title}")
    if df is None or df.empty:
        st.info("No candidates right now."); return
    for _, r in df.iterrows():
        with st.container(border=True):
            c1, c2, c3, c4, c5, c6 = st.columns([2,2,2,2,2,3])
            c1.markdown(f"**{r['symbol']} {int(r['strike'])}{r['opt_type']}** \nExp: {r['expiry']}")
            c2.write(f"LTP: {r['ltp']:.2f}")
            c3.write(f"OI phase: {r['oi_phase']}")
            c4.write(f"P&F: {r['pnf_col']} ({int(r['pnf_len'])}b)")
            c5.write(f"Vol>10%: {'Yes' if r['vol_gt10'] else 'No'}")
            risk_val = (r["ltp"] * BOX_PCT * SL_BOXES) * (LOT_SIZE[r["symbol"]] * int(lots))
            c6.write(f"Init SL ₹: {risk_val:,.0f}")
            cb1, cb2, cb3 = st.columns([1,1,2])
            if cb1.button(f"{side_default}", key=f"{side_default}-{r['symbol']}-{r['strike']}-{r['opt_type']}-{r['expiry']}"):
                place_and_record(side_default, r, int(lots))
            if cb2.button("CLOSE", key=f"CLOSE-{r['symbol']}-{r['strike']}-{r['opt_type']}-{r['expiry']}"):
                keep = []
                for pos in ss.positions:
                    if pos["symbol"]==r["symbol"] and pos["strike"]==int(r["strike"]) and pos["opt_type"]==r["opt_type"] and pos["expiry"]==r["expiry"]:
                        ss.trade_log.append({"ts": dt.datetime.now(), "action":"CLOSE", "symbol": pos["symbol"], "strike": pos["strike"], "opt_type": pos["opt_type"], "expiry": pos["expiry"]})
                    else:
                        keep.append(pos)
                ss.positions = keep
            if ss.auto_entry_enabled and ss.one_auto_order_per_index and not can_auto_open(r["symbol"]):
                cb3.caption("Auto-entry blocked: an open position exists for this index (manual allowed).")

render_bucket("Short build-up → SELL candidates", buckets.get("short_buildup_sell"), "SELL")
render_bucket("Long build-up → BUY candidates", buckets.get("long_buildup_buy"), "BUY")
render_bucket("Short covering → BUY candidates", buckets.get("short_covering_buy"), "BUY")
render_bucket("Long unwinding → SELL candidates", buckets.get("long_unwinding_sell"), "SELL")

st.subheader("Open Positions (Realtime SL/Trail monitored)")
if ss.positions:
    pos_df = pd.DataFrame(ss.positions)
    pos_df["sl_price"] = np.where(pos_df["side"]=="BUY",
                                  pos_df["entry_price"] - pos_df["sl_points"],
                                  pos_df["entry_price"] + pos_df["sl_points"])
    st.dataframe(pos_df[["ts","side","symbol","strike","opt_type","expiry","entry_price","sl_price","lots","order_id"]])
else:
    st.write("No open positions.")

st.subheader("Trade Log")
if ss.trade_log:
    st.dataframe(pd.DataFrame(ss.trade_log))
else:
    st.write("No trades yet.")
