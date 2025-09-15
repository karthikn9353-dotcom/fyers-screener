# imbalance_screener.py
import streamlit as st
import pandas as pd
import datetime as dt
import hashlib, requests, time
from fyers_apiv3 import fyersModel
from streamlit_autorefresh import st_autorefresh

st.set_page_config(page_title="Imbalance Screener", layout="wide")
st.title("📊 Imbalance Screener (Fyers)")

# --------- Read secrets (set these in Streamlit Secrets) ----------
# In Streamlit Cloud: go to App -> Settings -> Secrets and paste a TOML like:
# [fyers]
# client_id = "APPID-100"
# secret_key = "YOUR_SECRET"
# access_token = "OPTIONAL_EXISTING_ACCESS_TOKEN"
# refresh_token = "OPTIONAL_REFRESH_TOKEN"
# pin = "OPTIONAL_PIN_FOR_REFRESH"

FY = st.secrets.get("fyers", {})
CLIENT_ID = FY.get("client_id")
SECRET_KEY = FY.get("secret_key")
STORED_ACCESS_TOKEN = FY.get("access_token")
REFRESH_TOKEN = FY.get("refresh_token")
PIN = FY.get("pin")  # optional, sometimes needed for refresh

if not CLIENT_ID or not SECRET_KEY:
    st.error("You must add fyers.client_id and fyers.secret_key to Streamlit Secrets.")
    st.stop()

# --------- helper: refresh access token using stored refresh_token ----------
def refresh_access_token(refresh_token, client_id, secret_key, pin=None):
    """
    Uses Fyers refresh endpoint to obtain a new access token.
    Returns (access_token, refresh_token) or (None, None) on failure.
    """
    if not refresh_token:
        return None, None
    appIdHash = hashlib.sha256((client_id + secret_key).encode()).hexdigest()
    payload = {
        "grant_type": "refresh_token",
        "appIdHash": appIdHash,
        "refresh_token": refresh_token
    }
    if pin:
        payload["pin"] = str(pin)

    # try the v3 endpoint (api-t1). If your app uses different base URL change this.
    url = "https://api-t1.fyers.in/api/v3/validate-refresh-token"
    try:
        r = requests.post(url, json=payload, timeout=10)
        r.raise_for_status()
        data = r.json()
        # response fields vary by fyers version; handle common keys:
        new_access = data.get("access_token") or data.get("data",{}).get("access_token")
        new_refresh = data.get("refresh_token") or data.get("data",{}).get("refresh_token")
        return new_access, new_refresh
    except Exception as e:
        st.warning(f"Refresh token failed: {e} - {r.text if 'r' in locals() else ''}")
        return None, None

# --------- get a working access_token (either stored or refreshed) ----------
access_token = STORED_ACCESS_TOKEN
if not access_token and REFRESH_TOKEN:
    access_token, new_refresh = refresh_access_token(REFRESH_TOKEN, CLIENT_ID, SECRET_KEY, PIN)
    if access_token:
        st.info("Access token obtained using refresh token (session-only).")

if not access_token:
    st.warning("No access token available. You can still deploy but you must supply `access_token` or `refresh_token` in Secrets for app to query Fyers.")
    # we do not stop so the UI still loads

# ---------- init fyers client ----------
def make_fyers_client(token):
    return fyersModel.FyersModel(client_id=CLIENT_ID, token=token, log_path="")

# ---------- get 1-min candles ----------
def get_1min_candles(symbol, days=1, fyers_client=None):
    try:
        if fyers_client is None:
            return pd.DataFrame()
        date_to = dt.date.today().strftime("%Y-%m-%d")
        date_from = (dt.date.today() - dt.timedelta(days=days)).strftime("%Y-%m-%d")
        data = {
            "symbol": f"NSE:{symbol}-EQ",
            "resolution": "1",
            "date_format": "1",
            "range_from": date_from,
            "range_to": date_to,
            "cont_flag": "1"
        }
        resp = fyers_client.history(data)
        if not resp or 'candles' not in resp:
            return pd.DataFrame()
        df = pd.DataFrame(resp['candles'], columns=["time","open","high","low","close","volume"])
        df[['open','high','low','close','volume']] = df[['open','high','low','close','volume']].apply(pd.to_numeric, errors='coerce')
        return df
    except Exception as e:
        st.write(f"Error fetching {symbol}: {e}")
        return pd.DataFrame()

# ---------- imbalance logic ----------
def check_imbalance_for_symbol(symbol, fyers_client, multiplier=10):
    df = get_1min_candles(symbol, days=2, fyers_client=fyers_client)
    if df.empty or len(df) < 6:
        return None
    df['value'] = df['close'] * df['volume']
    avg_val = df['value'].iloc[:-1].mean()
    last_val = df['value'].iloc[-1]
    if pd.isna(avg_val) or avg_val == 0:
        return None
    if last_val > multiplier * avg_val:
        pct_move = (df['close'].iloc[-1] - df['close'].iloc[-2]) / df['close'].iloc[-2] * 100
        return {
            "Symbol": symbol,
            "Last Value (₹ Cr)": round(last_val/1e7, 2),
            "Avg Value (₹ Cr)": round(avg_val/1e7, 2),
            "Close": df['close'].iloc[-1],
            "%Move (1m)": round(pct_move, 2)
        }
    return None

# ---------- UI ----------
symbols_input = st.text_area("Enter NSE symbols (comma separated)", "RELIANCE, TCS, HDFCBANK, INFY")
symbols = [s.strip().upper() for s in symbols_input.split(",") if s.strip()]
mult = st.slider("Imbalance multiplier (last candle vs avg)", 3, 30, 10)
autoref_ms = st.number_input("Auto-refresh interval (seconds)", min_value=15, value=60, step=5)

# start autorefresh (frontend triggers rerun)
count = st_autorefresh(interval=int(autoref_ms*1000), limit=None, key="dataref")

# Try to create a client (if we have an access token)
client = None
if access_token:
    client = make_fyers_client(access_token)

# If client exists, run screener
if client:
    results = []
    status = st.empty()
    for idx, s in enumerate(symbols):
        status.text(f"Checking {s} ({idx+1}/{len(symbols)}) ...")
        res = check_imbalance_for_symbol(s, client, multiplier=mult)
        if res:
            results.append(res)
    status.empty()
    if results:
        df = pd.DataFrame(results).sort_values("Last Value (₹ Cr)", ascending=False)
        st.success(f"Found {len(results)} imbalance stocks (updated {dt.datetime.now().strftime('%H:%M:%S')})")
        st.dataframe(df, use_container_width=True)
    else:
        st.info(f"No imbalance stocks right now (updated {dt.datetime.now().strftime('%H:%M:%S')}).")
else:
    st.info("App currently has no working access token. Add access_token or refresh_token to Secrets and reload.")
