# -*- coding: utf-8 -*-
"""
全市場掃描:爬取證交所全部上市+上櫃股票清單,批次抓資料、算指標、評分,
輸出可排序/搜尋的 HTML 報告。
用法: python scan_all_stocks.py
輸出: report_all.html + data/scan_results.csv
"""
import io
import json
import os
import sys
import time
from datetime import datetime

import pandas as pd
import requests
import yfinance as yf

sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8")

OUT_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DATA_DIR = os.path.join(OUT_DIR, "data")
os.makedirs(DATA_DIR, exist_ok=True)

PERIOD = "6mo"        # 6個月日線,足夠算 MA60
CHUNK = 150           # yfinance 每批下載檔數
MIN_TURNOVER = 5e6    # 流動性門檻:20日均成交金額 < 500萬台幣的略過(訊號不可靠)


# ===== 1. 爬取全部股票清單(證交所 ISIN 公告頁) =====
def fetch_ticker_list() -> pd.DataFrame:
    """strMode=2 上市, strMode=4 上櫃。頁面為 MS950 編碼的 HTML 表格。"""
    frames = []
    for mode, market, suffix in [(2, "上市", ".TW"), (4, "上櫃", ".TWO")]:
        url = f"https://isin.twse.com.tw/isin/C_public.jsp?strMode={mode}"
        r = requests.get(url, timeout=30, headers={"User-Agent": "Mozilla/5.0"})
        r.encoding = "ms950"
        tables = pd.read_html(io.StringIO(r.text))
        df = tables[0]
        df.columns = df.iloc[0]
        df = df.iloc[1:]
        # 只取「股票」區段(在 "股票" 列與下一個分類列之間),用 CFICode=ESVUFR 過濾普通股
        df = df[df["CFICode"] == "ESVUFR"].copy()
        parts = df["有價證券代號及名稱"].str.split("　", n=1, expand=True)
        df["code"] = parts[0].str.strip()
        df["name"] = parts[1].str.strip()
        df = df[df["code"].str.fullmatch(r"\d{4}")]  # 4碼普通股
        df["market"] = market
        df["yf"] = df["code"] + suffix
        df["industry"] = df.get("產業別", "")
        frames.append(df[["code", "name", "market", "industry", "yf"]])
        print(f"  {market}: {len(frames[-1])} 檔")
    return pd.concat(frames, ignore_index=True)


# ===== 2. 指標 + 評分(與 stock_analyzer.py 同一套邏輯) =====
def analyze_one(df: pd.DataFrame) -> dict | None:
    df = df.dropna(subset=["Close"])
    if len(df) < 70:
        return None
    close = df["Close"]
    ma5 = close.rolling(5).mean()
    ma20 = close.rolling(20).mean()
    ma60 = close.rolling(60).mean()
    delta = close.diff()
    gain = delta.clip(lower=0).ewm(alpha=1 / 14, adjust=False).mean()
    loss = (-delta.clip(upper=0)).ewm(alpha=1 / 14, adjust=False).mean()
    rsi = 100 - 100 / (1 + gain / loss)
    ema12 = close.ewm(span=12, adjust=False).mean()
    ema26 = close.ewm(span=26, adjust=False).mean()
    macd = ema12 - ema26
    macd_sig = macd.ewm(span=9, adjust=False).mean()
    hist = macd - macd_sig
    std20 = close.rolling(20).std()
    bb_up, bb_lo = ma20 + 2 * std20, ma20 - 2 * std20
    vol_ma20 = df["Volume"].rolling(20).mean()
    turnover = (close * df["Volume"]).rolling(20).mean()

    i, p = -1, -2
    if pd.isna(ma60.iloc[i]) or pd.isna(rsi.iloc[i]):
        return None
    if pd.notna(turnover.iloc[i]) and turnover.iloc[i] < MIN_TURNOVER:
        return None  # 流動性不足

    score = 0
    reasons = []
    c, cp = close.iloc[i], close.iloc[p]
    if ma5.iloc[i] > ma20.iloc[i] > ma60.iloc[i]:
        score += 2; reasons.append("均線多頭排列")
    elif ma5.iloc[i] < ma20.iloc[i] < ma60.iloc[i]:
        score -= 2; reasons.append("均線空頭排列")
    if c > ma20.iloc[i]:
        score += 1; reasons.append("站上月線")
    else:
        score -= 1; reasons.append("跌破月線")
    if rsi.iloc[i] < 30:
        score += 2; reasons.append(f"RSI {rsi.iloc[i]:.0f} 超賣")
    elif rsi.iloc[i] > 70:
        score -= 2; reasons.append(f"RSI {rsi.iloc[i]:.0f} 超買")
    if hist.iloc[p] <= 0 < hist.iloc[i]:
        score += 2; reasons.append("MACD 黃金交叉")
    elif hist.iloc[p] >= 0 > hist.iloc[i]:
        score -= 2; reasons.append("MACD 死亡交叉")
    elif hist.iloc[i] > 0:
        score += 1; reasons.append("MACD 多方動能")
    else:
        score -= 1; reasons.append("MACD 空方動能")
    if c < bb_lo.iloc[i]:
        score += 1; reasons.append("破布林下軌")
    elif c > bb_up.iloc[i]:
        score -= 1; reasons.append("破布林上軌")
    if pd.notna(vol_ma20.iloc[i]) and vol_ma20.iloc[i] > 0:
        vr = df["Volume"].iloc[i] / vol_ma20.iloc[i]
        if vr > 1.5:
            if c > cp:
                score += 1; reasons.append(f"放量上漲 {vr:.1f}x")
            else:
                score -= 1; reasons.append(f"放量下跌 {vr:.1f}x")

    n = len(close)
    return {
        "close": round(float(c), 2),
        "chg_1d": round((c / cp - 1) * 100, 2),
        "chg_20d": round((c / close.iloc[-21] - 1) * 100, 2) if n >= 21 else None,
        "rsi": round(float(rsi.iloc[i]), 0),
        "turnover_m": round(float(turnover.iloc[i]) / 1e6, 1),
        "score": score,
        "reasons": ";".join(reasons),
    }


def signal_of(score: int) -> tuple[str, str]:
    if score >= 4: return "強力買進", "strong-buy"
    if score >= 2: return "買進", "buy"
    if score <= -4: return "強力賣出", "strong-sell"
    if score <= -2: return "賣出", "sell"
    return "觀望", "hold"


# ===== 3. 批次下載 + 掃描 =====
def scan(tickers: pd.DataFrame) -> pd.DataFrame:
    results = []
    yf_list = tickers["yf"].tolist()
    total = len(yf_list)
    for start in range(0, total, CHUNK):
        batch = yf_list[start:start + CHUNK]
        print(f"  下載 {start + 1}-{min(start + CHUNK, total)} / {total} ...", flush=True)
        try:
            data = yf.download(batch, period=PERIOD, auto_adjust=True,
                               group_by="ticker", threads=True, progress=False)
        except Exception as e:
            print(f"    批次失敗: {e}")
            continue
        for t in batch:
            try:
                df = data[t] if len(batch) > 1 else data
                r = analyze_one(df)
                if r:
                    row = tickers[tickers["yf"] == t].iloc[0]
                    r.update(code=row["code"], name=row["name"],
                             market=row["market"], industry=row["industry"])
                    results.append(r)
            except (KeyError, Exception):
                continue
        time.sleep(1)
    return pd.DataFrame(results)


# ===== 4. HTML 報告 =====
def build_html(df: pd.DataFrame, ts: str, n_total: int) -> str:
    df = df.sort_values("score", ascending=False).reset_index(drop=True)
    for col, default in [("industry", "")]:
        df[col] = df[col].fillna(default)
    sig = df["score"].apply(signal_of)
    df["signal"] = sig.apply(lambda x: x[0])
    df["css"] = sig.apply(lambda x: x[1])

    counts = df["signal"].value_counts()
    summary = " | ".join(f"{k} {counts.get(k, 0)} 檔" for k in
                         ["強力買進", "買進", "觀望", "賣出", "強力賣出"])

    rows = []
    for _, s in df.iterrows():
        chg_cls = "up" if s["chg_1d"] >= 0 else "down"
        chg20 = f"{s['chg_20d']:+.1f}%" if pd.notna(s["chg_20d"]) else "-"
        rows.append(
            f"<tr data-sig=\"{s['signal']}\"><td>{s['code']}</td><td>{s['name']}</td>"
            f"<td>{s['market']}</td><td>{s['industry']}</td>"
            f"<td class=\"num\">{s['close']:,.2f}</td>"
            f"<td class=\"num {chg_cls}\">{s['chg_1d']:+.2f}%</td>"
            f"<td class=\"num\">{chg20}</td><td class=\"num\">{s['rsi']:.0f}</td>"
            f"<td class=\"num\">{s['turnover_m']:,.0f}</td>"
            f"<td><span class=\"badge {s['css']}\">{s['signal']}</span></td>"
            f"<td class=\"num\">{s['score']:+d}</td>"
            f"<td class=\"rsn\">{s['reasons']}</td></tr>")

    return f"""<!DOCTYPE html>
<html lang="zh-Hant"><head>
<meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>全市場掃描報告</title>
<style>
:root{{color-scheme:dark}}
body{{font-family:'Microsoft JhengHei',system-ui,sans-serif;background:#12141a;color:#e8eaf0;margin:0;padding:24px}}
h1{{margin:0 0 4px}} .sub{{color:#8a90a0;margin-bottom:18px}}
.controls{{display:flex;gap:10px;margin-bottom:14px;flex-wrap:wrap}}
input,select{{background:#1b1e27;border:1px solid #2a2e3a;color:#e8eaf0;border-radius:8px;padding:8px 12px;font-size:14px}}
input{{width:260px}}
table{{width:100%;border-collapse:collapse;background:#1b1e27;border-radius:12px;overflow:hidden}}
th,td{{padding:7px 10px;text-align:left;border-bottom:1px solid #262a36;font-size:13px;white-space:nowrap}}
th{{background:#222633;color:#9aa2b8;cursor:pointer;user-select:none;position:sticky;top:0}}
th:hover{{color:#fff}}
.num{{text-align:right;font-variant-numeric:tabular-nums}}
.up{{color:#ff5d6c}} .down{{color:#3ddc84}}
.badge{{padding:2px 8px;border-radius:99px;font-size:11.5px;font-weight:700}}
.strong-buy{{background:#7a1f2b;color:#ffb3bc}} .buy{{background:#5c2a33;color:#ff9aa6}}
.hold{{background:#3a3f4d;color:#cfd5e4}}
.sell{{background:#1f4d35;color:#9be8c0}} .strong-sell{{background:#155c38;color:#7df0b4}}
.rsn{{color:#8a90a0;font-size:12px;max-width:420px;overflow:hidden;text-overflow:ellipsis}}
.note{{color:#666c7e;font-size:12px;margin-top:20px}}
</style></head><body>
<h1>🔍 全市場掃描報告</h1>
<div class="sub">更新:{ts} | 上市+上櫃普通股 {n_total} 檔,通過流動性門檻納入分析 {len(df)} 檔 | {summary} | 紅漲綠跌</div>
<div class="controls">
  <input id="q" placeholder="搜尋代碼 / 名稱 / 產業..." oninput="filt()">
  <select id="sig" onchange="filt()">
    <option value="">全部訊號</option><option>強力買進</option><option>買進</option>
    <option>觀望</option><option>賣出</option><option>強力賣出</option>
  </select>
</div>
<table id="tbl"><thead><tr>
<th onclick="srt(0)">代碼</th><th onclick="srt(1)">名稱</th><th onclick="srt(2)">市場</th>
<th onclick="srt(3)">產業</th><th onclick="srt(4,1)">收盤</th><th onclick="srt(5,1)">日漲跌</th>
<th onclick="srt(6,1)">20日漲跌</th><th onclick="srt(7,1)">RSI</th><th onclick="srt(8,1)">20日均額(百萬)</th>
<th onclick="srt(9)">訊號</th><th onclick="srt(10,1)">評分</th><th>理由</th>
</tr></thead><tbody>{''.join(rows)}</tbody></table>
<div class="note">⚠️ 技術指標自動產生,僅供參考,不構成投資建議。已排除 20 日均成交金額低於 500 萬的低流動性個股。點欄位標題可排序。</div>
<script>
const tb=document.querySelector('#tbl tbody');
function filt(){{
  const q=document.getElementById('q').value.toLowerCase(),s=document.getElementById('sig').value;
  for(const r of tb.rows){{
    const hit=(!q||r.textContent.toLowerCase().includes(q))&&(!s||r.dataset.sig===s);
    r.style.display=hit?'':'none';
  }}
}}
let dir={{}};
function srt(c,numeric){{
  dir[c]=-(dir[c]||1);
  const rows=[...tb.rows];
  rows.sort((a,b)=>{{
    let x=a.cells[c].textContent.trim(),y=b.cells[c].textContent.trim();
    if(numeric){{x=parseFloat(x.replace(/[,%+]/g,''))||0;y=parseFloat(y.replace(/[,%+]/g,''))||0;return (x-y)*dir[c];}}
    return x.localeCompare(y,'zh-Hant')*dir[c];
  }});
  rows.forEach(r=>tb.appendChild(r));
}}
</script></body></html>"""


def main():
    t0 = time.time()
    print(f"=== 全市場掃描 {datetime.now():%Y-%m-%d %H:%M} ===")
    print("[1/3] 爬取證交所股票清單...")
    tickers = fetch_ticker_list()
    print(f"  合計 {len(tickers)} 檔")

    print("[2/3] 批次下載歷史資料並分析...")
    df = scan(tickers)
    if df.empty:
        print("掃描失敗,沒有任何結果。")
        sys.exit(1)
    df.to_csv(os.path.join(DATA_DIR, "scan_results.csv"),
              index=False, encoding="utf-8-sig")

    print("[3/3] 產生報告...")
    ts = datetime.now().strftime("%Y-%m-%d %H:%M")
    out = os.path.join(OUT_DIR, "report_all.html")
    with open(out, "w", encoding="utf-8") as f:
        f.write(build_html(df, ts, len(tickers)))
    print(f"\n✅ 完成,共分析 {len(df)} 檔,耗時 {time.time() - t0:.0f} 秒")
    print(f"   報告: {out}")


if __name__ == "__main__":
    main()
