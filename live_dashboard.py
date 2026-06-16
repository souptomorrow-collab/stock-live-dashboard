# -*- coding: utf-8 -*-
"""
即時股票分析儀表板 (FastAPI)
- 台股: 證交所 MIS 即時報價 API(盤中約 5 秒更新)
- 美股/加密貨幣: Yahoo Finance 即時報價
- 背景每 10 秒更新報價,用「歷史日線 + 即時價」重算指標與訊號
用法: python live_dashboard.py  →  瀏覽器開 http://127.0.0.1:8000
"""
import io
import json
import os
import sys
import threading
import time
from datetime import datetime

import pandas as pd
import requests
import yfinance as yf
from fastapi import FastAPI
from fastapi.responses import HTMLResponse, JSONResponse

sys.stdout.reconfigure(encoding="utf-8")

# ===== 自選股(台股用 MIS 即時源;美股/幣用 Yahoo) =====
TW_STOCKS = {  # code: (名稱, tse|otc)
    "2330": ("台積電", "tse"),
    "2317": ("鴻海", "tse"),
    "2454": ("聯發科", "tse"),
    "2308": ("台達電", "tse"),
    "0050": ("元大台灣50", "tse"),
}
YF_TICKERS = {
    "AAPL": "Apple", "NVDA": "NVIDIA", "MSFT": "Microsoft",
    "GOOGL": "Alphabet", "TSLA": "Tesla", "SPCX": "SpaceX",
    "BTC-USD": "Bitcoin", "ETH-USD": "Ethereum",
}

POLL_SEC = 5   # 台股輪詢間隔(秒)= MIS 源約 5 秒刷新一次,設更低無新資料且有封鎖風險
YF_POLL_SEC = 5    # 美股/加密輪詢(秒):全市場掃描已放慢讓出 Yahoo 額度,自選股 8 檔可跑 5 秒
app = FastAPI()
STATE = {"quotes": {}, "updated": None}
HISTORY: dict[str, pd.Series] = {}   # ticker -> 日線收盤序列(不含今日)
VOLHIST: dict[str, float] = {}       # ticker -> 20日均量

DATA_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "data")
WATCHLIST_FILE = os.path.join(DATA_DIR, "watchlist.json")  # 使用者自選股(可動態增減,程式內為預設種子)
_TW_NAMES = None


# ===== 自選股清單持久化(存檔,讓使用者可自行增減) =====
def save_watchlist():
    os.makedirs(DATA_DIR, exist_ok=True)
    with open(WATCHLIST_FILE, "w", encoding="utf-8") as f:
        json.dump({"tw": {k: [v[0], v[1]] for k, v in TW_STOCKS.items()},
                   "yf": YF_TICKERS}, f, ensure_ascii=False, indent=2)


def load_watchlist():
    if not os.path.exists(WATCHLIST_FILE):
        save_watchlist()       # 首次:把程式內預設清單寫成可編輯檔
        return
    try:
        with open(WATCHLIST_FILE, encoding="utf-8") as f:
            d = json.load(f)
        TW_STOCKS.clear()
        TW_STOCKS.update({k: (v[0], v[1]) for k, v in d.get("tw", {}).items()})
        YF_TICKERS.clear()
        YF_TICKERS.update(d.get("yf", {}))
    except Exception as e:
        print(f"[watchlist] 載入失敗,沿用預設: {e}")


def _tw_name(code):
    global _TW_NAMES
    if _TW_NAMES is None:
        _TW_NAMES = {}
        p = os.path.join(DATA_DIR, "tickers.csv")
        if os.path.exists(p):
            try:
                df = pd.read_csv(p, dtype={"code": str})
                _TW_NAMES = dict(zip(df["code"], df["name"]))
            except Exception:
                pass
    return _TW_NAMES.get(code)


def _load_one_history(yf_sym):
    """用 Yahoo chart 端點抓單一標的 6 個月日線(較不易被限流),回傳 (close, vol20) 或 None。"""
    try:
        r = YF_SESSION.get(f"https://query1.finance.yahoo.com/v8/finance/chart/{yf_sym}",
                           params={"range": "6mo", "interval": "1d"}, timeout=10)
        res = r.json()["chart"]["result"][0]
        q = res["indicators"]["quote"][0]
        df = pd.DataFrame({"Close": q["close"], "Volume": q["volume"]},
                          index=pd.to_datetime(res["timestamp"], unit="s")).dropna(subset=["Close"])
        df = df[df.index.normalize() < pd.Timestamp.now().normalize()]   # 去掉今日未完成 bar
        if len(df) < 1:
            return None
        vol20 = df["Volume"].tail(20).mean()
        return df["Close"], (float(vol20) if pd.notna(vol20) else 0.0)
    except Exception:
        return None


def add_ticker(raw):
    raw = (raw or "").strip().upper()
    if not raw:
        return {"ok": False, "msg": "請輸入代碼"}
    if raw.isdigit() and len(raw) == 4:        # 台股:4 碼數字
        if raw in TW_STOCKS:
            return {"ok": False, "msg": f"{raw} 已在自選股"}
        for suffix, mis in ((".TW", "tse"), (".TWO", "otc")):
            h = _load_one_history(raw + suffix)
            if h:
                name = _tw_name(raw) or raw
                HISTORY[raw + suffix], VOLHIST[raw + suffix] = h
                TW_STOCKS[raw] = (name, mis)
                save_watchlist()
                return {"ok": True, "msg": f"已新增台股 {raw} {name}"}
        return {"ok": False, "msg": f"找不到台股 {raw}(上市/上櫃皆查無)"}
    sym = raw                                  # 美股 / 加密貨幣
    if sym in YF_TICKERS:
        return {"ok": False, "msg": f"{sym} 已在自選股"}
    h = _load_one_history(sym)
    if not h:
        return {"ok": False, "msg": f"找不到 {sym}(Yahoo 無資料)"}
    HISTORY[sym], VOLHIST[sym] = h
    YF_TICKERS[sym] = sym
    save_watchlist()
    return {"ok": True, "msg": f"已新增 {sym}"}


def remove_ticker(raw):
    raw = (raw or "").strip().upper()
    if raw in TW_STOCKS:
        m = TW_STOCKS.pop(raw)[1]
        HISTORY.pop(raw + (".TW" if m == "tse" else ".TWO"), None)
    elif raw in YF_TICKERS:
        YF_TICKERS.pop(raw)
        HISTORY.pop(raw, None)
    else:
        return {"ok": False, "msg": f"{raw} 不在自選股"}
    STATE["quotes"].pop(raw, None)
    save_watchlist()
    return {"ok": True, "msg": f"已移除 {raw}"}


# ===== 啟動時抓歷史日線(逐檔走 chart 端點,較不易被限流、不會整批漏抓) =====
def load_history():
    load_watchlist()           # 先載入(含使用者新增的)自選股清單
    syms = [c + (".TW" if m == "tse" else ".TWO") for c, (_, m) in TW_STOCKS.items()] \
        + list(YF_TICKERS)
    print("載入歷史日線...")
    for s in syms:
        h = _load_one_history(s)
        if h:
            HISTORY[s], VOLHIST[s] = h
        else:
            print(f"  [警告] {s} 歷史載入失敗")
        time.sleep(0.3)        # 輕微間隔,避免被 Yahoo 限流
    print(f"  完成,共 {len(HISTORY)} 檔")


# ===== 即時指標 + 評分(歷史收盤 + 現價) =====
def live_score(hist: pd.Series, price: float, vol_ratio: float | None) -> dict:
    if len(hist) < 60:  # 歷史不足(如剛上市新股),技術指標不可靠,不給誤導性訊號
        return {"rsi": "-", "score": 0, "signal": "資料不足", "css": "hold",
                "reasons": f"上市未滿,僅 {len(hist)} 日歷史,技術指標待累積"}
    close = pd.concat([hist, pd.Series([price])], ignore_index=True)
    ma5 = close.rolling(5).mean().iloc[-1]
    ma20 = close.rolling(20).mean().iloc[-1]
    ma60 = close.rolling(60).mean().iloc[-1]
    delta = close.diff()
    gain = delta.clip(lower=0).ewm(alpha=1 / 14, adjust=False).mean()
    loss = (-delta.clip(upper=0)).ewm(alpha=1 / 14, adjust=False).mean()
    rsi = float((100 - 100 / (1 + gain / loss)).iloc[-1])
    ema12 = close.ewm(span=12, adjust=False).mean()
    ema26 = close.ewm(span=26, adjust=False).mean()
    macd = ema12 - ema26
    hist_line = macd - macd.ewm(span=9, adjust=False).mean()
    std20 = close.rolling(20).std().iloc[-1]

    score, reasons = 0, []
    if ma5 > ma20 > ma60: score += 2; reasons.append("均線多頭")
    elif ma5 < ma20 < ma60: score -= 2; reasons.append("均線空頭")
    if price > ma20: score += 1; reasons.append("站上月線")
    else: score -= 1; reasons.append("低於月線")
    if rsi < 30: score += 2; reasons.append(f"RSI{rsi:.0f}超賣")
    elif rsi > 70: score -= 2; reasons.append(f"RSI{rsi:.0f}超買")
    if hist_line.iloc[-2] <= 0 < hist_line.iloc[-1]: score += 2; reasons.append("MACD金叉")
    elif hist_line.iloc[-2] >= 0 > hist_line.iloc[-1]: score -= 2; reasons.append("MACD死叉")
    elif hist_line.iloc[-1] > 0: score += 1; reasons.append("MACD偏多")
    else: score -= 1; reasons.append("MACD偏空")
    if price < ma20 - 2 * std20: score += 1; reasons.append("破布林下軌")
    elif price > ma20 + 2 * std20: score -= 1; reasons.append("破布林上軌")
    if vol_ratio and vol_ratio > 1.5:
        prev = float(hist.iloc[-1])
        if price > prev: score += 1; reasons.append(f"放量上漲{vol_ratio:.1f}x")
        else: score -= 1; reasons.append(f"放量下跌{vol_ratio:.1f}x")

    if score >= 4: sig, css = "強力買進", "strong-buy"
    elif score >= 2: sig, css = "買進", "buy"
    elif score <= -4: sig, css = "強力賣出", "strong-sell"
    elif score <= -2: sig, css = "賣出", "sell"
    else: sig, css = "觀望", "hold"
    return {"rsi": round(rsi, 0), "score": score, "signal": sig,
            "css": css, "reasons": " / ".join(reasons)}


# ===== 台股 MIS 即時報價 =====
MIS_SESSION = requests.Session()
MIS_SESSION.headers["User-Agent"] = "Mozilla/5.0"

def fetch_tw_quotes() -> dict:
    items = list(TW_STOCKS.items())
    if not items:
        return {}
    ex_ch = "|".join(f"{m}_{c}.tw" for c, (_, m) in items)
    url = "https://mis.twse.com.tw/stock/api/getStockInfo.jsp"
    r = MIS_SESSION.get(url, params={"ex_ch": ex_ch, "json": "1", "delay": "0",
                                     "_": str(int(time.time() * 1000))}, timeout=10)
    out = {}
    for m in r.json().get("msgArray", []):
        code = m.get("c")
        z = m.get("z", "-")
        if z in ("-", "", None):  # 無成交時用最佳買賣價中點,再退而求其次用昨收
            try:
                b = float(m["b"].split("_")[0]); a = float(m["a"].split("_")[0])
                z = (a + b) / 2
            except Exception:
                z = m.get("y", "-")
        try:
            price = float(z)
            y = float(m["y"])
            out[code] = {"price": price, "prev": y, "vol": float(m.get("v", 0)) * 1000,
                         "high": m.get("h"), "low": m.get("l"),
                         "time": m.get("t", "")}
        except Exception:
            continue
    return out


# ===== Yahoo 即時報價(官方 chart 端點:現價/昨收/最高/最低一次到位,漲跌幅正確) =====
YF_SESSION = requests.Session()
YF_SESSION.headers["User-Agent"] = "Mozilla/5.0"


def fetch_yf_quotes() -> dict:
    out = {}
    for t in list(YF_TICKERS):
        try:
            r = YF_SESSION.get(f"https://query1.finance.yahoo.com/v8/finance/chart/{t}", timeout=8)
            m = r.json()["chart"]["result"][0]["meta"]
            price = m.get("regularMarketPrice")
            prev = m.get("previousClose") or m.get("chartPreviousClose")
            if price is None or not prev:
                continue
            out[t] = {"price": float(price), "prev": float(prev),
                      "vol": float(m.get("regularMarketVolume") or 0),
                      "high": m.get("regularMarketDayHigh"),
                      "low": m.get("regularMarketDayLow"),
                      "time": datetime.now().strftime("%H:%M:%S")}
        except Exception:
            continue
    return out


# ===== 背景更新迴圈 =====
YF_CACHE: dict = {}   # 美股/加密最新報價,由 yf_updater 執行緒每 5 秒刷新


def yf_updater():     # 獨立執行緒:美股+加密每 20 秒刷新(Yahoo 限流,不能更快)
    global YF_CACHE
    while True:
        try:
            YF_CACHE = fetch_yf_quotes()
        except Exception as e:
            print(f"[Yahoo錯誤] {e}")
        time.sleep(YF_POLL_SEC)


def updater():        # 台股主迴圈:每 5 秒
    while True:
        quotes = {}
        try:
            tw = fetch_tw_quotes()
        except Exception as e:
            print(f"[MIS錯誤] {e}"); tw = {}
        for code, (name, m) in list(TW_STOCKS.items()):
            q = tw.get(code)
            sym = code + (".TW" if m == "tse" else ".TWO")
            if not q or sym not in HISTORY:
                continue
            vr = q["vol"] / VOLHIST[sym] if VOLHIST.get(sym) else None
            r = live_score(HISTORY[sym], q["price"], vr)
            quotes[code] = {"name": name, "group": "台股", **q,
                            "chg": round((q["price"] / q["prev"] - 1) * 100, 2), **r}
        for t, name in list(YF_TICKERS.items()):
            q = YF_CACHE.get(t)
            if not q or t not in HISTORY:
                continue
            grp = "加密貨幣" if t.endswith("-USD") else "美股"
            r = live_score(HISTORY[t], q["price"], None)
            quotes[t] = {"name": name, "group": grp, **q,
                         "chg": round((q["price"] / q["prev"] - 1) * 100, 2), **r}

        if quotes:
            STATE["quotes"] = quotes
            STATE["updated"] = datetime.now().strftime("%H:%M:%S")
        time.sleep(POLL_SEC)


@app.get("/api/quotes")
def api_quotes():
    return JSONResponse(STATE)


@app.post("/api/quotes/add")
def api_add(ticker: str):
    return JSONResponse(add_ticker(ticker))


@app.post("/api/quotes/remove")
def api_remove(ticker: str):
    return JSONResponse(remove_ticker(ticker))


PAGE = """<!DOCTYPE html>
<html lang="zh-Hant"><head>
<meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>即時股票分析</title>
<style>
:root{color-scheme:dark}
body{font-family:'Microsoft JhengHei',system-ui,sans-serif;background:#12141a;color:#e8eaf0;margin:0;padding:24px}
h1{margin:0 0 4px} .sub{color:#8a90a0;margin-bottom:18px}
.live{display:inline-block;width:9px;height:9px;border-radius:50%;background:#3ddc84;margin-right:6px;animation:pulse 1.6s infinite}
@keyframes pulse{50%{opacity:.3}}
h2{border-left:4px solid #4f9cff;padding-left:10px;margin:26px 0 10px;font-size:17px}
table{width:100%;border-collapse:collapse;background:#1b1e27;border-radius:12px;overflow:hidden}
th,td{padding:9px 12px;text-align:left;border-bottom:1px solid #262a36;font-size:14px;white-space:nowrap}
th{background:#222633;color:#9aa2b8}
.num{text-align:right;font-variant-numeric:tabular-nums}
.up{color:#ff5d6c} .down{color:#3ddc84}
.badge{padding:2px 9px;border-radius:99px;font-size:12px;font-weight:700}
.strong-buy{background:#7a1f2b;color:#ffb3bc} .buy{background:#5c2a33;color:#ff9aa6}
.hold{background:#3a3f4d;color:#cfd5e4}
.sell{background:#1f4d35;color:#9be8c0} .strong-sell{background:#155c38;color:#7df0b4}
.rsn{color:#8a90a0;font-size:12px}
td.flash-up{animation:fu .8s} td.flash-dn{animation:fd .8s}
@keyframes fu{0%{background:#5c2a33}100%{background:transparent}}
@keyframes fd{0%{background:#1f4d35}100%{background:transparent}}
.note{color:#666c7e;font-size:12px;margin-top:20px}
.addbar{display:flex;gap:8px;align-items:center;flex-wrap:wrap;margin:4px 0 18px}
.addbar input{background:#1b1e27;border:1px solid #2a2e3a;color:#e8eaf0;border-radius:8px;padding:8px 12px;font-size:14px;width:340px}
.addbar button{background:#2a3550;border:1px solid #4f9cff;color:#e8eaf0;border-radius:8px;padding:8px 16px;font-size:14px;cursor:pointer}
.addbar button:hover{background:#34406a}
.del{color:#ff5d6c;cursor:pointer;font-weight:700;padding:0 8px}.del:hover{color:#ff8b96}
</style></head><body>
<h1>📡 即時股票分析</h1>
<div class="sub"><span class="live"></span>台股 / 美股 / 加密每 5 秒自動更新 | 最後更新:<span id="ts">-</span> | 紅漲綠跌</div>
<div class="addbar">
  <input id="newt" placeholder="輸入代碼新增,例:2454(台股)/ TSLA(美股)/ DOGE-USD(幣)" onkeydown="if(event.key==='Enter')addTicker()">
  <button onclick="addTicker()">＋ 新增自選股</button>
  <span id="addmsg" style="font-size:13px"></span>
</div>
<div id="root">載入中...</div>
<div class="note">⚠️ 台股報價來自證交所 MIS(盤中即時,收盤後顯示收盤價);美股/加密貨幣來自 Yahoo Finance。訊號由技術指標即時計算,僅供參考。</div>
<script>
const prev={};
async function refresh(){
  try{
    const r=await fetch('/api/quotes'); const s=await r.json();
    document.getElementById('ts').textContent=s.updated||'-';
    const groups={};
    for(const [k,q] of Object.entries(s.quotes)) (groups[q.group]=groups[q.group]||[]).push([k,q]);
    let html='';
    for(const g of ['台股','美股','加密貨幣']){
      if(!groups[g]) continue;
      html+=`<h2>${g}</h2><table><tr><th>名稱</th><th>代碼</th><th class=num>成交價</th><th class=num>漲跌幅</th><th class=num>最高</th><th class=num>最低</th><th class=num>RSI</th><th>訊號</th><th class=num>評分</th><th>理由</th><th>報價時間</th><th></th></tr>`;
      for(const [k,q] of groups[g]){
        const cls=q.chg>=0?'up':'down';
        const dir=prev[k]===undefined?'':(q.price>prev[k]?'flash-up':(q.price<prev[k]?'flash-dn':''));
        prev[k]=q.price;
        html+=`<tr><td>${q.name}</td><td style="color:#8a90a0">${k}</td>
        <td class="num ${dir}"><b>${q.price.toLocaleString(undefined,{maximumFractionDigits:2})}</b></td>
        <td class="num ${cls}">${q.chg>=0?'▲':'▼'} ${q.chg.toFixed(2)}%</td>
        <td class=num>${q.high??'-'}</td><td class=num>${q.low??'-'}</td>
        <td class=num>${q.rsi}</td><td><span class="badge ${q.css}">${q.signal}</span></td>
        <td class=num>${q.score>0?'+':''}${q.score}</td><td class=rsn>${q.reasons}</td><td style="color:#8a90a0">${q.time}</td>
        <td><span class="del" title="移除自選股" onclick="delTicker('${k}')">✕</span></td></tr>`;
      }
      html+='</table>';
    }
    document.getElementById('root').innerHTML=html;
  }catch(e){console.error(e);}
}
async function addTicker(){
  const el=document.getElementById('newt'), v=el.value.trim(), msg=document.getElementById('addmsg');
  if(!v) return;
  msg.style.color='#8a90a0'; msg.textContent='新增中(載入歷史資料)...';
  try{
    const r=await fetch('/api/quotes/add?ticker='+encodeURIComponent(v),{method:'POST'});
    const d=await r.json();
    msg.style.color=d.ok?'#3ddc84':'#ff5d6c'; msg.textContent=d.msg;
    if(d.ok){ el.value=''; setTimeout(refresh,800); }
  }catch(e){ msg.style.color='#ff5d6c'; msg.textContent='新增失敗'; }
}
async function delTicker(code){
  if(!confirm('從自選股移除 '+code+'?')) return;
  try{ await fetch('/api/quotes/remove?ticker='+encodeURIComponent(code),{method:'POST'}); refresh(); }
  catch(e){}
}
refresh(); setInterval(refresh,5000);
</script></body></html>"""


@app.get("/", response_class=HTMLResponse)
def index():
    return PAGE


if __name__ == "__main__":
    import uvicorn
    load_history()
    threading.Thread(target=updater, daemon=True).start()
    threading.Thread(target=yf_updater, daemon=True).start()
    print("即時儀表板: http://127.0.0.1:8000")
    uvicorn.run(app, host="127.0.0.1", port=8000, log_level="warning")
