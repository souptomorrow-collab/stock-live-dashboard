# -*- coding: utf-8 -*-
"""
股票自動分析:抓取台股/美股/加密貨幣資料,計算技術指標,產生 HTML 儀表板。
用法: python stock_analyzer.py
輸出: report.html (瀏覽器開啟即可) + data/*.csv (原始資料備份)
"""
import json
import os
import sys
import io
from datetime import datetime

import pandas as pd
import yfinance as yf

sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8")

# ===== 自選股清單(可自行增減) =====
WATCHLIST = {
    "台股": {
        "2330.TW": "台積電",
        "2317.TW": "鴻海",
        "2454.TW": "聯發科",
        "2308.TW": "台達電",
        "0050.TW": "元大台灣50",
    },
    "美股": {
        "AAPL": "Apple",
        "NVDA": "NVIDIA",
        "MSFT": "Microsoft",
        "GOOGL": "Alphabet",
        "TSLA": "Tesla",
    },
    "加密貨幣": {
        "BTC-USD": "Bitcoin",
        "ETH-USD": "Ethereum",
    },
}

PERIOD = "1y"  # 抓一年日線
OUT_DIR = os.path.dirname(os.path.abspath(__file__))
DATA_DIR = os.path.join(OUT_DIR, "data")
os.makedirs(DATA_DIR, exist_ok=True)


# ===== 技術指標 =====
def compute_indicators(df: pd.DataFrame) -> pd.DataFrame:
    close = df["Close"]
    df["MA5"] = close.rolling(5).mean()
    df["MA20"] = close.rolling(20).mean()
    df["MA60"] = close.rolling(60).mean()

    # RSI(14)
    delta = close.diff()
    gain = delta.clip(lower=0).ewm(alpha=1 / 14, adjust=False).mean()
    loss = (-delta.clip(upper=0)).ewm(alpha=1 / 14, adjust=False).mean()
    rs = gain / loss
    df["RSI"] = 100 - 100 / (1 + rs)

    # MACD(12,26,9)
    ema12 = close.ewm(span=12, adjust=False).mean()
    ema26 = close.ewm(span=26, adjust=False).mean()
    df["MACD"] = ema12 - ema26
    df["MACD_signal"] = df["MACD"].ewm(span=9, adjust=False).mean()
    df["MACD_hist"] = df["MACD"] - df["MACD_signal"]

    # 布林通道(20, 2)
    std20 = close.rolling(20).std()
    df["BB_upper"] = df["MA20"] + 2 * std20
    df["BB_lower"] = df["MA20"] - 2 * std20

    # 量能
    df["VOL_MA20"] = df["Volume"].rolling(20).mean()
    return df


# ===== 訊號評分 =====
def score_stock(df: pd.DataFrame) -> dict:
    last = df.iloc[-1]
    prev = df.iloc[-2]
    score = 0
    reasons = []

    # 1. 均線多頭排列
    if last["MA5"] > last["MA20"] > last["MA60"]:
        score += 2
        reasons.append("均線多頭排列 (MA5>MA20>MA60)")
    elif last["MA5"] < last["MA20"] < last["MA60"]:
        score -= 2
        reasons.append("均線空頭排列 (MA5<MA20<MA60)")

    # 2. 價格 vs MA20
    if last["Close"] > last["MA20"]:
        score += 1
        reasons.append("收盤價站上月線 MA20")
    else:
        score -= 1
        reasons.append("收盤價跌破月線 MA20")

    # 3. RSI
    if last["RSI"] < 30:
        score += 2
        reasons.append(f"RSI={last['RSI']:.0f} 超賣,可能反彈")
    elif last["RSI"] > 70:
        score -= 2
        reasons.append(f"RSI={last['RSI']:.0f} 超買,注意回檔")
    else:
        reasons.append(f"RSI={last['RSI']:.0f} 中性區間")

    # 4. MACD 黃金/死亡交叉(最近一日剛交叉)
    if prev["MACD"] <= prev["MACD_signal"] and last["MACD"] > last["MACD_signal"]:
        score += 2
        reasons.append("MACD 黃金交叉")
    elif prev["MACD"] >= prev["MACD_signal"] and last["MACD"] < last["MACD_signal"]:
        score -= 2
        reasons.append("MACD 死亡交叉")
    elif last["MACD_hist"] > 0:
        score += 1
        reasons.append("MACD 柱狀體為正(多方動能)")
    else:
        score -= 1
        reasons.append("MACD 柱狀體為負(空方動能)")

    # 5. 布林通道位置
    if last["Close"] < last["BB_lower"]:
        score += 1
        reasons.append("跌破布林下軌,超跌")
    elif last["Close"] > last["BB_upper"]:
        score -= 1
        reasons.append("突破布林上軌,過熱")

    # 6. 量能
    if pd.notna(last["VOL_MA20"]) and last["VOL_MA20"] > 0:
        vol_ratio = last["Volume"] / last["VOL_MA20"]
        if vol_ratio > 1.5 and last["Close"] > prev["Close"]:
            score += 1
            reasons.append(f"放量上漲 (量比 {vol_ratio:.1f}x)")
        elif vol_ratio > 1.5 and last["Close"] < prev["Close"]:
            score -= 1
            reasons.append(f"放量下跌 (量比 {vol_ratio:.1f}x)")

    if score >= 4:
        signal, css = "強力買進", "strong-buy"
    elif score >= 2:
        signal, css = "買進", "buy"
    elif score <= -4:
        signal, css = "強力賣出", "strong-sell"
    elif score <= -2:
        signal, css = "賣出", "sell"
    else:
        signal, css = "觀望", "hold"

    return {"score": score, "signal": signal, "css": css, "reasons": reasons}


def pct(a, b):
    return (a / b - 1) * 100 if b else float("nan")


def analyze_ticker(ticker: str, name: str) -> dict | None:
    try:
        df = yf.Ticker(ticker).history(period=PERIOD, auto_adjust=True)
        if df.empty or len(df) < 70:
            print(f"  [跳過] {ticker} 資料不足")
            return None
        df = compute_indicators(df)
        df.to_csv(os.path.join(DATA_DIR, f"{ticker.replace('.', '_')}.csv"))

        last = df.iloc[-1]
        close = float(last["Close"])
        result = score_stock(df)

        tail = df.tail(120)
        chart = {
            "dates": [d.strftime("%m/%d") for d in tail.index],
            "close": [round(float(x), 2) for x in tail["Close"]],
            "ma20": [round(float(x), 2) if pd.notna(x) else None for x in tail["MA20"]],
            "ma60": [round(float(x), 2) if pd.notna(x) else None for x in tail["MA60"]],
        }

        return {
            "ticker": ticker,
            "name": name,
            "close": close,
            "chg_1d": pct(close, float(df["Close"].iloc[-2])),
            "chg_5d": pct(close, float(df["Close"].iloc[-6])),
            "chg_20d": pct(close, float(df["Close"].iloc[-21])),
            "rsi": float(last["RSI"]),
            "ma20": float(last["MA20"]),
            "ma60": float(last["MA60"]),
            "high_52w": float(df["Close"].max()),
            "low_52w": float(df["Close"].min()),
            "chart": chart,
            **result,
        }
    except Exception as e:
        print(f"  [錯誤] {ticker}: {e}")
        return None


# ===== HTML 報告 =====
def build_html(groups: dict, ts: str) -> str:
    cards = []
    chart_js = []
    for group, items in groups.items():
        cards.append(f'<h2 class="group-title">{group}</h2><div class="grid">')
        for s in items:
            cid = s["ticker"].replace(".", "_").replace("-", "_")
            reasons = "".join(f"<li>{r}</li>" for r in s["reasons"])
            arrow = "▲" if s["chg_1d"] >= 0 else "▼"
            chg_cls = "up" if s["chg_1d"] >= 0 else "down"
            pos52 = (s["close"] - s["low_52w"]) / (s["high_52w"] - s["low_52w"]) * 100
            cards.append(f"""
<div class="card">
  <div class="card-head">
    <div><span class="name">{s['name']}</span> <span class="ticker">{s['ticker']}</span></div>
    <span class="badge {s['css']}">{s['signal']} ({s['score']:+d})</span>
  </div>
  <div class="price-row">
    <span class="price">{s['close']:,.2f}</span>
    <span class="chg {chg_cls}">{arrow} {s['chg_1d']:+.2f}%</span>
  </div>
  <div class="stats">
    <span>5日 {s['chg_5d']:+.1f}%</span><span>20日 {s['chg_20d']:+.1f}%</span>
    <span>RSI {s['rsi']:.0f}</span><span>52週位置 {pos52:.0f}%</span>
  </div>
  <canvas id="c_{cid}" height="120"></canvas>
  <ul class="reasons">{reasons}</ul>
</div>""")
            chart_js.append(f"""
new Chart(document.getElementById('c_{cid}'), {{
  type:'line',
  data:{{labels:{json.dumps(s['chart']['dates'])},datasets:[
    {{label:'收盤',data:{json.dumps(s['chart']['close'])},borderColor:'#4f9cff',borderWidth:1.6,pointRadius:0,tension:.2}},
    {{label:'MA20',data:{json.dumps(s['chart']['ma20'])},borderColor:'#ffb84f',borderWidth:1,pointRadius:0,tension:.2}},
    {{label:'MA60',data:{json.dumps(s['chart']['ma60'])},borderColor:'#b86bff',borderWidth:1,pointRadius:0,tension:.2}}
  ]}},
  options:{{plugins:{{legend:{{display:false}}}},scales:{{x:{{ticks:{{maxTicksLimit:6,color:'#888'}},grid:{{display:false}}}},y:{{ticks:{{color:'#888'}},grid:{{color:'rgba(255,255,255,.06)'}}}}}},animation:false}}
}});""")
        cards.append("</div>")

    # 總覽排序表
    all_items = [s for items in groups.values() for s in items]
    all_items.sort(key=lambda x: -x["score"])
    rows = "".join(
        f"<tr><td>{s['name']}</td><td>{s['ticker']}</td><td>{s['close']:,.2f}</td>"
        f"<td class=\"{'up' if s['chg_1d']>=0 else 'down'}\">{s['chg_1d']:+.2f}%</td>"
        f"<td>{s['rsi']:.0f}</td><td><span class=\"badge {s['css']}\">{s['signal']}</span></td>"
        f"<td>{s['score']:+d}</td></tr>"
        for s in all_items
    )

    return f"""<!DOCTYPE html>
<html lang="zh-Hant"><head>
<meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>股票自動分析儀表板</title>
<script src="https://cdn.jsdelivr.net/npm/chart.js@4"></script>
<style>
:root{{color-scheme:dark}}
body{{font-family:'Microsoft JhengHei',system-ui,sans-serif;background:#12141a;color:#e8eaf0;margin:0;padding:24px}}
h1{{margin:0 0 4px}}
.sub{{color:#8a90a0;margin-bottom:24px}}
.group-title{{border-left:4px solid #4f9cff;padding-left:10px;margin:32px 0 14px}}
.grid{{display:grid;grid-template-columns:repeat(auto-fill,minmax(340px,1fr));gap:16px}}
.card{{background:#1b1e27;border:1px solid #2a2e3a;border-radius:12px;padding:16px}}
.card-head{{display:flex;justify-content:space-between;align-items:center}}
.name{{font-size:17px;font-weight:700}}
.ticker{{color:#8a90a0;font-size:12px}}
.price-row{{display:flex;align-items:baseline;gap:10px;margin:8px 0}}
.price{{font-size:26px;font-weight:700}}
.chg{{font-size:14px;font-weight:600}}
.up{{color:#ff5d6c}} .down{{color:#3ddc84}}
.stats{{display:flex;gap:14px;flex-wrap:wrap;color:#aab;font-size:12px;margin-bottom:8px}}
.badge{{padding:3px 10px;border-radius:99px;font-size:12px;font-weight:700;white-space:nowrap}}
.strong-buy{{background:#7a1f2b;color:#ffb3bc}} .buy{{background:#5c2a33;color:#ff9aa6}}
.hold{{background:#3a3f4d;color:#cfd5e4}}
.sell{{background:#1f4d35;color:#9be8c0}} .strong-sell{{background:#155c38;color:#7df0b4}}
.reasons{{margin:10px 0 0;padding-left:18px;color:#aab;font-size:12.5px;line-height:1.7}}
table{{width:100%;border-collapse:collapse;background:#1b1e27;border-radius:12px;overflow:hidden}}
th,td{{padding:10px 14px;text-align:left;border-bottom:1px solid #2a2e3a;font-size:14px}}
th{{background:#222633;color:#9aa2b8}}
.note{{color:#666c7e;font-size:12px;margin-top:30px}}
</style></head><body>
<h1>📈 股票自動分析儀表板</h1>
<div class="sub">更新時間:{ts} | 資料來源:Yahoo Finance | 紅漲綠跌(台股習慣)</div>
<h2 class="group-title">總覽(依評分排序)</h2>
<table><tr><th>名稱</th><th>代碼</th><th>收盤</th><th>日漲跌</th><th>RSI</th><th>訊號</th><th>評分</th></tr>{rows}</table>
{''.join(cards)}
<div class="note">⚠️ 本報告由技術指標自動產生,僅供參考,不構成投資建議。評分規則:均線排列、MA20、RSI、MACD、布林通道、量能,共 -9 ~ +9 分。</div>
<script>{''.join(chart_js)}</script>
</body></html>"""


def main():
    print(f"=== 股票自動分析 {datetime.now():%Y-%m-%d %H:%M} ===")
    groups = {}
    for group, tickers in WATCHLIST.items():
        print(f"\n[{group}]")
        items = []
        for ticker, name in tickers.items():
            print(f"  抓取 {name} ({ticker}) ...", end=" ")
            r = analyze_ticker(ticker, name)
            if r:
                print(f"{r['close']:,.2f} ({r['chg_1d']:+.2f}%) → {r['signal']} ({r['score']:+d})")
                items.append(r)
        if items:
            groups[group] = items

    if not groups:
        print("沒有抓到任何資料,請檢查網路連線。")
        sys.exit(1)

    ts = datetime.now().strftime("%Y-%m-%d %H:%M")
    out = os.path.join(OUT_DIR, "report.html")
    with open(out, "w", encoding="utf-8") as f:
        f.write(build_html(groups, ts))
    print(f"\n✅ 報告已產生: {out}")


if __name__ == "__main__":
    main()
