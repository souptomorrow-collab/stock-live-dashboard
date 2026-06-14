# -*- coding: utf-8 -*-
"""
股票分析整合網站(正式入口)
- /          首頁(導覽)
- /watchlist 自選股即時分析(台股秒級 + 美股/加密貨幣)
- /market    全市場即時分析(1900 檔輪掃)
- /scan      全市場掃描報告(靜態,scan_all_stocks.py 產生)
- /report    自選股深度報告(靜態,stock_analyzer.py 產生)
用法: python app.py  →  http://127.0.0.1:8000
"""
import io
import os
import sys
import threading

from fastapi import FastAPI
from fastapi.responses import HTMLResponse, JSONResponse, FileResponse

sys.stdout.reconfigure(encoding="utf-8")
OUT_DIR = os.path.dirname(os.path.abspath(__file__))

import live_dashboard as wl       # 自選股即時模組
import live_all_dashboard as mk   # 全市場即時模組

app = FastAPI(title="股票分析儀表板")

NAV = """<div style="display:flex;gap:6px;flex-wrap:wrap;margin:-4px 0 18px;
padding:10px;background:#1b1e27;border:1px solid #2a2e3a;border-radius:12px">
<a href="/" style="color:#e8eaf0;text-decoration:none;padding:6px 14px;border-radius:8px;font-size:14px">🏠 首頁</a>
<a href="/watchlist" style="color:#e8eaf0;text-decoration:none;padding:6px 14px;border-radius:8px;font-size:14px;background:#222633">📡 自選股即時</a>
<a href="/market" style="color:#e8eaf0;text-decoration:none;padding:6px 14px;border-radius:8px;font-size:14px;background:#222633">🔍 全市場即時</a>
<a href="/scan" style="color:#e8eaf0;text-decoration:none;padding:6px 14px;border-radius:8px;font-size:14px;background:#222633">📋 全市場掃描報告</a>
<a href="/report" style="color:#e8eaf0;text-decoration:none;padding:6px 14px;border-radius:8px;font-size:14px;background:#222633">📈 自選股深度報告</a>
</div>"""

HOME = f"""<!DOCTYPE html>
<html lang="zh-Hant"><head>
<meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>股票分析儀表板</title>
<style>
:root{{color-scheme:dark}}
body{{font-family:'Microsoft JhengHei',system-ui,sans-serif;background:#12141a;color:#e8eaf0;margin:0;padding:24px;max-width:980px;margin:auto}}
h1{{margin:16px 0 6px}} .sub{{color:#8a90a0;margin-bottom:24px}}
.grid{{display:grid;grid-template-columns:repeat(auto-fill,minmax(280px,1fr));gap:16px}}
a.card{{display:block;background:#1b1e27;border:1px solid #2a2e3a;border-radius:14px;padding:20px;
text-decoration:none;color:#e8eaf0;transition:.15s}}
a.card:hover{{border-color:#4f9cff;transform:translateY(-2px)}}
.t{{font-size:18px;font-weight:700;margin-bottom:6px}}
.d{{color:#8a90a0;font-size:13.5px;line-height:1.6}}
.note{{color:#666c7e;font-size:12px;margin-top:28px}}
</style></head><body>
<h1>📊 股票分析儀表板</h1>
<div class="sub">台股・美股・加密貨幣 | 即時報價 × 技術指標自動分析</div>
<div class="grid">
<a class="card" href="/watchlist"><div class="t">📡 自選股即時</div>
<div class="d">台積電、聯發科、NVIDIA、BTC 等 12 檔。台股走證交所即時源,每 10 秒更新訊號。</div></a>
<a class="card" href="/market"><div class="t">🔍 全市場即時</div>
<div class="d">台股全市場 ~1900 檔 + 美股 S&P 500 + 加密貨幣前 100,即時輪掃,可搜尋、篩選市場/訊號、排序。</div></a>
<a class="card" href="/scan"><div class="t">📋 全市場掃描報告</div>
<div class="d">每日收盤後的全市場技術面掃描快照,含評分理由。</div></a>
<a class="card" href="/report"><div class="t">📈 自選股深度報告</div>
<div class="d">自選股 K 線走勢圖 + 完整指標解讀(MA/RSI/MACD/布林/量能)。</div></a>
</div>
<div class="note">⚠️ 所有訊號由技術指標自動產生,僅供參考,不構成投資建議。</div>
</body></html>"""


@app.get("/", response_class=HTMLResponse)
def home():
    return HOME


@app.get("/watchlist", response_class=HTMLResponse)
def watchlist_page():
    page = wl.PAGE.replace("/api/quotes", "/api/watch")
    return page.replace("<body>", "<body>" + NAV, 1)


@app.get("/market", response_class=HTMLResponse)
def market_page():
    page = mk.PAGE.replace("/api/quotes", "/api/market")
    return page.replace("<body>", "<body>" + NAV, 1)


@app.get("/api/watch")
def api_watch():
    return JSONResponse(wl.STATE)


@app.get("/api/market")
def api_market():
    return JSONResponse(mk.STATE)


@app.get("/scan")
def scan_report():
    p = os.path.join(OUT_DIR, "report_all.html")
    if os.path.exists(p):
        return FileResponse(p)
    return HTMLResponse("尚未產生,請先執行 python scan_all_stocks.py")


@app.get("/report")
def deep_report():
    p = os.path.join(OUT_DIR, "report.html")
    if os.path.exists(p):
        return FileResponse(p)
    return HTMLResponse("尚未產生,請先執行 python stock_analyzer.py")


def boot():
    print("[1/3] 載入自選股歷史資料...")
    wl.load_history()
    threading.Thread(target=wl.updater, daemon=True).start()
    print("[2/3] 取得全市場清單(台股+美股+加密貨幣)與歷史資料(約 3-5 分鐘)...")
    tickers = mk.get_all_tickers()
    mk.load_history(tickers)
    threading.Thread(target=mk.sweep_loop, daemon=True).start()       # 台股 MIS 輪掃
    threading.Thread(target=mk.yf_sweep_loop, daemon=True).start()    # 美股+加密貨幣 Yahoo 輪掃
    print("[3/3] 啟動完成")


if __name__ == "__main__":
    import uvicorn
    boot()
    print("整合儀表板: http://127.0.0.1:8000  (0.0.0.0 對外)")
    uvicorn.run(app, host="0.0.0.0", port=8000, log_level="warning")
