# 台股即時分析儀表板 (Stock Live Dashboard)

台股(證交所 MIS 盤中即時)+ 美股 + 加密貨幣的自動技術分析,FastAPI 一站式整合。

## 功能

| 入口 | 說明 |
|---|---|
| `python app.py` → http://127.0.0.1:8000 | **整合網站**(下列全部功能 + 導覽) |
| `/watchlist` | 自選股即時:台股走證交所 MIS(盤中秒級)、美股/加密走 Yahoo,每 10 秒重算訊號 |
| `/market` | 全市場即時:上市+上櫃 ~1400 檔輪掃(每批 50 檔、間隔 1.5s,一輪約 1-2 分鐘),可搜尋/篩選/排序 |
| `/scan` | 全市場掃描報告(靜態快照,先跑 `python scan_all_stocks.py`) |
| `/report` | 自選股深度報告含走勢圖(先跑 `python stock_analyzer.py`) |

## 分析邏輯

技術指標評分制(-9 ~ +9 → 強力買進/買進/觀望/賣出/強力賣出):

- 均線多空排列 (MA5/MA20/MA60) ±2
- 價格 vs 月線 MA20 ±1
- RSI(14) 超買超賣 ±2
- MACD(12,26,9) 黃金/死亡交叉 ±2、柱狀體方向 ±1
- 布林通道 (20,2σ) 突破上下軌 ±1
- 量能:量比 >1.5x 配合漲跌 ±1

全市場掃描排除 20 日均成交金額 < 500 萬的低流動性個股。

## 安裝與執行

```bash
pip install fastapi uvicorn yfinance pandas requests
python app.py            # 整合網站(啟動時載入歷史資料約 1-2 分鐘)
python scan_all_stocks.py  # 產生全市場掃描報告 report_all.html
python stock_analyzer.py   # 產生自選股深度報告 report.html
```

對外公開(臨時網址,免帳號):

```bash
cloudflared tunnel --url http://127.0.0.1:8000
```

## 資料來源與限制

- 台股清單:證交所 ISIN 公告頁(快取一天於 `data/tickers.csv`)
- 台股即時報價:`mis.twse.com.tw`(官方即時源;**查詢過快會被封鎖**,已內建批次 50 檔 + 1.5s 間隔)
- 歷史日線 / 美股 / 加密:Yahoo Finance (yfinance)
- MIS 即時源對台灣本地 IP 最穩定;部署到國外雲端(如 Render)不保證可連

> ⚠️ 所有訊號由技術指標自動產生,僅供參考,不構成投資建議。
