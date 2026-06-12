# -*- coding: utf-8 -*-
"""
全市場即時分析儀表板 (FastAPI)
- 啟動時:爬證交所股票清單(快取) + 下載全部歷史日線當指標基底
- 背景:以證交所 MIS API 每批 50 檔輪掃全市場(約 1-2 分鐘一輪),即時重算訊號
用法: python live_all_dashboard.py  →  http://127.0.0.1:8001
"""
import io
import os
import sys
import threading
import time
from datetime import datetime

import numpy as np
import pandas as pd
import requests
import yfinance as yf
from fastapi import FastAPI
from fastapi.responses import HTMLResponse, JSONResponse

sys.stdout.reconfigure(encoding="utf-8")

OUT_DIR = os.path.dirname(os.path.abspath(__file__))
DATA_DIR = os.path.join(OUT_DIR, "data")
os.makedirs(DATA_DIR, exist_ok=True)
TICKER_CACHE = os.path.join(DATA_DIR, "tickers.csv")

MIS_BATCH = 50        # MIS API 每次查詢檔數
MIS_DELAY = 1.5       # 每批間隔秒數(避免被證交所封鎖)
MIN_TURNOVER = 5e6    # 流動性門檻(20日均成交金額)
HIST_CHUNK = 150

app = FastAPI()
STATE = {"quotes": {}, "updated": None, "sweep_sec": None, "n_total": 0}
TICKERS: pd.DataFrame = None
HIST: dict[str, dict] = {}  # code -> {close: np.array, vol20: float, name, market, industry}


# ===== 股票清單(快取一天) =====
def get_tickers() -> pd.DataFrame:
    if os.path.exists(TICKER_CACHE):
        age = time.time() - os.path.getmtime(TICKER_CACHE)
        if age < 86400:
            df = pd.read_csv(TICKER_CACHE, dtype={"code": str})
            print(f"  使用快取清單 {len(df)} 檔")
            return df
    frames = []
    for mode, market, suffix, mis in [(2, "上市", ".TW", "tse"), (4, "上櫃", ".TWO", "otc")]:
        url = f"https://isin.twse.com.tw/isin/C_public.jsp?strMode={mode}"
        r = requests.get(url, timeout=30, headers={"User-Agent": "Mozilla/5.0"})
        r.encoding = "ms950"
        df = pd.read_html(io.StringIO(r.text))[0]
        df.columns = df.iloc[0]
        df = df.iloc[1:]
        df = df[df["CFICode"] == "ESVUFR"].copy()
        parts = df["有價證券代號及名稱"].str.split("　", n=1, expand=True)
        df["code"] = parts[0].str.strip()
        df["name"] = parts[1].str.strip()
        df = df[df["code"].str.fullmatch(r"\d{4}")]
        df["market"] = market
        df["mis"] = mis
        df["yf"] = df["code"] + suffix
        df["industry"] = df.get("產業別", "").fillna("")
        frames.append(df[["code", "name", "market", "industry", "mis", "yf"]])
    out = pd.concat(frames, ignore_index=True)
    out.to_csv(TICKER_CACHE, index=False, encoding="utf-8-sig")
    print(f"  爬取清單 {len(out)} 檔")
    return out


# ===== 歷史日線基底 =====
def load_history(tickers: pd.DataFrame):
    yf_list = tickers["yf"].tolist()
    today = pd.Timestamp.now().normalize()
    kept = 0
    for start in range(0, len(yf_list), HIST_CHUNK):
        batch = yf_list[start:start + HIST_CHUNK]
        print(f"  歷史資料 {start + 1}-{min(start + HIST_CHUNK, len(yf_list))} / {len(yf_list)}", flush=True)
        try:
            data = yf.download(batch, period="6mo", auto_adjust=True,
                               group_by="ticker", threads=True, progress=False)
        except Exception as e:
            print(f"    批次失敗: {e}")
            continue
        for t in batch:
            try:
                df = data[t].dropna(subset=["Close"])
                df = df[df.index.tz_localize(None).normalize() < today]
                if len(df) < 70:
                    continue
                close = df["Close"]
                turnover = float((close * df["Volume"]).tail(20).mean())
                if turnover < MIN_TURNOVER:
                    continue
                row = tickers[tickers["yf"] == t].iloc[0]
                HIST[row["code"]] = {
                    "close": close.to_numpy(dtype=float),
                    "vol20": float(df["Volume"].tail(20).mean()),
                    "name": row["name"], "market": row["market"],
                    "industry": row["industry"], "mis": row["mis"],
                }
                kept += 1
            except Exception:
                continue
        time.sleep(0.5)
    print(f"  完成,納入分析 {kept} 檔(已排除流動性不足/資料不足)")


# ===== 即時評分(numpy 版,快) =====
def ema(arr, span):
    alpha = 2 / (span + 1)
    out = np.empty_like(arr)
    out[0] = arr[0]
    for i in range(1, len(arr)):
        out[i] = alpha * arr[i] + (1 - alpha) * out[i - 1]
    return out

def live_score(close: np.ndarray, price: float, vol_ratio) -> dict:
    c = np.append(close, price)
    ma5, ma20, ma60 = c[-5:].mean(), c[-20:].mean(), c[-60:].mean()
    delta = np.diff(c)
    alpha = 1 / 14
    g = l = 0.0
    for d in delta:  # Wilder RSI
        g = alpha * max(d, 0) + (1 - alpha) * g
        l = alpha * max(-d, 0) + (1 - alpha) * l
    rsi = 100.0 if l == 0 else 100 - 100 / (1 + g / l)
    macd = ema(c, 12) - ema(c, 26)
    hist_line = macd - ema(macd, 9)
    std20 = c[-20:].std(ddof=1)

    score, reasons = 0, []
    if ma5 > ma20 > ma60: score += 2; reasons.append("均線多頭")
    elif ma5 < ma20 < ma60: score -= 2; reasons.append("均線空頭")
    if price > ma20: score += 1; reasons.append("站上月線")
    else: score -= 1; reasons.append("低於月線")
    if rsi < 30: score += 2; reasons.append(f"RSI{rsi:.0f}超賣")
    elif rsi > 70: score -= 2; reasons.append(f"RSI{rsi:.0f}超買")
    if hist_line[-2] <= 0 < hist_line[-1]: score += 2; reasons.append("MACD金叉")
    elif hist_line[-2] >= 0 > hist_line[-1]: score -= 2; reasons.append("MACD死叉")
    elif hist_line[-1] > 0: score += 1; reasons.append("MACD偏多")
    else: score -= 1; reasons.append("MACD偏空")
    if price < ma20 - 2 * std20: score += 1; reasons.append("破布林下軌")
    elif price > ma20 + 2 * std20: score -= 1; reasons.append("破布林上軌")
    if vol_ratio and vol_ratio > 1.5:
        if price > close[-1]: score += 1; reasons.append(f"放量漲{vol_ratio:.1f}x")
        else: score -= 1; reasons.append(f"放量跌{vol_ratio:.1f}x")

    if score >= 4: sig, css = "強力買進", "strong-buy"
    elif score >= 2: sig, css = "買進", "buy"
    elif score <= -4: sig, css = "強力賣出", "strong-sell"
    elif score <= -2: sig, css = "賣出", "sell"
    else: sig, css = "觀望", "hold"
    return {"rsi": round(float(rsi)), "score": int(score), "signal": sig,
            "css": css, "reasons": "/".join(reasons)}


# ===== MIS 輪掃 =====
SESSION = requests.Session()
SESSION.headers["User-Agent"] = "Mozilla/5.0"

def sweep_loop():
    while True:
        codes = list(HIST.keys())
        t0 = time.time()
        for i in range(0, len(codes), MIS_BATCH):
            batch = codes[i:i + MIS_BATCH]
            ex_ch = "|".join(f"{HIST[c]['mis']}_{c}.tw" for c in batch)
            try:
                r = SESSION.get("https://mis.twse.com.tw/stock/api/getStockInfo.jsp",
                                params={"ex_ch": ex_ch, "json": "1", "delay": "0",
                                        "_": str(int(time.time() * 1000))}, timeout=10)
                msgs = r.json().get("msgArray", [])
            except Exception as e:
                print(f"[MIS錯誤] {e}")
                time.sleep(5)
                continue
            for m in msgs:
                code = m.get("c")
                h = HIST.get(code)
                if not h:
                    continue
                z = m.get("z", "-")
                if z in ("-", "", None):
                    try:
                        z = (float(m["b"].split("_")[0]) + float(m["a"].split("_")[0])) / 2
                    except Exception:
                        z = m.get("y", "-")
                try:
                    price = float(z); prev = float(m["y"])
                    if price <= 0 or prev <= 0:
                        continue
                except Exception:
                    continue
                vol = float(m.get("v") or 0) * 1000
                vr = vol / h["vol20"] if h["vol20"] else None
                res = live_score(h["close"], price, vr)
                STATE["quotes"][code] = {
                    "name": h["name"], "market": h["market"], "industry": h["industry"],
                    "price": price, "chg": round((price / prev - 1) * 100, 2),
                    "time": m.get("t", ""), **res,
                }
            STATE["updated"] = datetime.now().strftime("%H:%M:%S")
            time.sleep(MIS_DELAY)
        STATE["sweep_sec"] = round(time.time() - t0)
        STATE["n_total"] = len(STATE["quotes"])
        print(f"[輪掃完成] {len(codes)} 檔,{STATE['sweep_sec']} 秒")


@app.get("/api/quotes")
def api_quotes():
    return JSONResponse(STATE)


PAGE = """<!DOCTYPE html>
<html lang="zh-Hant"><head>
<meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>全市場即時分析</title>
<style>
:root{color-scheme:dark}
body{font-family:'Microsoft JhengHei',system-ui,sans-serif;background:#12141a;color:#e8eaf0;margin:0;padding:24px}
h1{margin:0 0 4px} .sub{color:#8a90a0;margin-bottom:14px}
.live{display:inline-block;width:9px;height:9px;border-radius:50%;background:#3ddc84;margin-right:6px;animation:pulse 1.6s infinite}
@keyframes pulse{50%{opacity:.3}}
.controls{display:flex;gap:10px;margin-bottom:12px;flex-wrap:wrap;align-items:center}
input,select{background:#1b1e27;border:1px solid #2a2e3a;color:#e8eaf0;border-radius:8px;padding:8px 12px;font-size:14px}
input{width:240px}
.chips{display:flex;gap:8px;flex-wrap:wrap;margin-bottom:12px}
.chip{background:#1b1e27;border:1px solid #2a2e3a;border-radius:99px;padding:4px 12px;font-size:13px;color:#aab}
table{width:100%;border-collapse:collapse;background:#1b1e27;border-radius:12px;overflow:hidden}
th,td{padding:6px 10px;text-align:left;border-bottom:1px solid #262a36;font-size:13px;white-space:nowrap}
th{background:#222633;color:#9aa2b8;cursor:pointer;user-select:none;position:sticky;top:0}
.num{text-align:right;font-variant-numeric:tabular-nums}
.up{color:#ff5d6c} .down{color:#3ddc84}
.badge{padding:2px 8px;border-radius:99px;font-size:11.5px;font-weight:700}
.strong-buy{background:#7a1f2b;color:#ffb3bc} .buy{background:#5c2a33;color:#ff9aa6}
.hold{background:#3a3f4d;color:#cfd5e4}
.sell{background:#1f4d35;color:#9be8c0} .strong-sell{background:#155c38;color:#7df0b4}
.rsn{color:#8a90a0;font-size:12px}
.note{color:#666c7e;font-size:12px;margin-top:16px}
</style></head><body>
<h1>📡 全市場即時分析</h1>
<div class="sub"><span class="live"></span>背景輪掃全市場(每輪約 1-2 分鐘) | 最後更新:<span id="ts">-</span> | 上輪耗時:<span id="sw">-</span> | 紅漲綠跌</div>
<div class="chips" id="chips"></div>
<div class="controls">
  <input id="q" placeholder="搜尋代碼 / 名稱 / 產業...">
  <select id="sig"><option value="">全部訊號</option><option>強力買進</option><option>買進</option><option>觀望</option><option>賣出</option><option>強力賣出</option></select>
  <select id="mkt"><option value="">上市+上櫃</option><option>上市</option><option>上櫃</option></select>
  <select id="srt">
    <option value="score_d">評分高→低</option><option value="score_a">評分低→高</option>
    <option value="chg_d">漲幅大→小</option><option value="chg_a">跌幅大→小</option>
    <option value="rsi_a">RSI 低→高</option><option value="rsi_d">RSI 高→低</option>
  </select>
  <span id="cnt" style="color:#8a90a0;font-size:13px"></span>
</div>
<table id="tbl"><thead><tr><th>代碼</th><th>名稱</th><th>市場</th><th>產業</th>
<th class=num>成交價</th><th class=num>漲跌幅</th><th class=num>RSI</th><th>訊號</th><th class=num>評分</th><th>理由</th><th>報價時間</th>
</tr></thead><tbody></tbody></table>
<div class="note">⚠️ 報價來自證交所 MIS,訊號由技術指標即時計算,僅供參考,不構成投資建議。已排除低流動性個股。預設只顯示前 300 筆,用搜尋/篩選縮小範圍。</div>
<script>
let DATA={};
function render(){
  const q=document.getElementById('q').value.toLowerCase();
  const sig=document.getElementById('sig').value, mkt=document.getElementById('mkt').value;
  const [sk,sd]=document.getElementById('srt').value.split('_');
  let rows=Object.entries(DATA).map(([k,v])=>({code:k,...v}));
  if(q) rows=rows.filter(r=>(r.code+r.name+r.industry).toLowerCase().includes(q));
  if(sig) rows=rows.filter(r=>r.signal===sig);
  if(mkt) rows=rows.filter(r=>r.market===mkt);
  const key={score:'score',chg:'chg',rsi:'rsi'}[sk];
  rows.sort((a,b)=>(sd==='d'?b[key]-a[key]:a[key]-b[key]));
  document.getElementById('cnt').textContent=`符合 ${rows.length} 檔`;
  const tb=document.querySelector('#tbl tbody');
  tb.innerHTML=rows.slice(0,300).map(r=>`<tr><td>${r.code}</td><td>${r.name}</td><td>${r.market}</td><td>${r.industry||''}</td>
  <td class=num><b>${r.price.toLocaleString(undefined,{maximumFractionDigits:2})}</b></td>
  <td class="num ${r.chg>=0?'up':'down'}">${r.chg>=0?'▲':'▼'} ${r.chg.toFixed(2)}%</td>
  <td class=num>${r.rsi}</td><td><span class="badge ${r.css}">${r.signal}</span></td>
  <td class=num>${r.score>0?'+':''}${r.score}</td><td class=rsn>${r.reasons}</td>
  <td style="color:#8a90a0">${r.time}</td></tr>`).join('');
}
function chips(){
  const counts={};
  for(const v of Object.values(DATA)) counts[v.signal]=(counts[v.signal]||0)+1;
  document.getElementById('chips').innerHTML=['強力買進','買進','觀望','賣出','強力賣出']
    .map(s=>`<span class=chip>${s} <b>${counts[s]||0}</b></span>`).join('');
}
async function refresh(){
  try{
    const r=await fetch('/api/quotes'); const s=await r.json();
    DATA=s.quotes;
    document.getElementById('ts').textContent=s.updated||'-';
    document.getElementById('sw').textContent=s.sweep_sec?s.sweep_sec+' 秒':'掃描中...';
    chips(); render();
  }catch(e){console.error(e);}
}
for(const id of ['q','sig','mkt','srt']) document.getElementById(id).addEventListener('input',render);
refresh(); setInterval(refresh,15000);
</script></body></html>"""


@app.get("/", response_class=HTMLResponse)
def index():
    return PAGE


if __name__ == "__main__":
    import uvicorn
    print("[1/2] 取得股票清單...")
    TICKERS = get_tickers()
    print("[2/2] 載入歷史日線(約 1-2 分鐘)...")
    load_history(TICKERS)
    threading.Thread(target=sweep_loop, daemon=True).start()
    print("全市場即時儀表板: http://127.0.0.1:8001")
    uvicorn.run(app, host="127.0.0.1", port=8001, log_level="warning")
