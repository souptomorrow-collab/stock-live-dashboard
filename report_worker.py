# -*- coding: utf-8 -*-
"""報告產生器(背景常駐):定時重產兩份靜態深度報告,避免它們停在舊資料。
- report.html      自選股深度報告(stock_analyzer.py,已改讀 data/watchlist.json)
- report_all.html  全市場掃描報告(scan_all_stocks.py)
由 supervisor.py 管理、開機自動啟動。手動測試: python report_worker.py
"""
import os
import subprocess
import sys
import time
from datetime import datetime

# pythonw(開機自動啟動)無主控台 → sys.stdout 會是 None,先補起來
if sys.stdout is None:
    sys.stdout = open(os.devnull, "w", encoding="utf-8")
if sys.stderr is None:
    sys.stderr = sys.stdout
try:
    sys.stdout.reconfigure(encoding="utf-8")
except Exception:
    pass

ROOT = os.path.dirname(os.path.abspath(__file__))
PY = sys.executable
NO_WINDOW = subprocess.CREATE_NO_WINDOW if sys.platform == "win32" else 0
START_DELAY = 360       # 開機後先等 6 分鐘,讓 app.py 的全市場歷史載入先跑完(避免搶 Yahoo 額度)
INTERVAL = 12 * 3600    # 之後每 12 小時重產一次
REPORTS = ["report.html", "report_all.html"]


def fresh(name, max_age):
    """報告檔存在且比 max_age 秒新 → True。"""
    try:
        return (time.time() - os.path.getmtime(os.path.join(ROOT, name))) < max_age
    except OSError:
        return False


def run(script):
    t0 = time.time()
    print(f"{datetime.now():%H:%M:%S} 產生 {script} ...", flush=True)
    p = subprocess.run([PY, os.path.join(ROOT, script)], cwd=ROOT,
                       capture_output=True, text=True, encoding="utf-8",
                       errors="replace", creationflags=NO_WINDOW)
    ok = p.returncode == 0
    print(f"{datetime.now():%H:%M:%S} {script} {'完成' if ok else '失敗 rc=' + str(p.returncode)} "
          f"({round(time.time() - t0)}s)", flush=True)
    if not ok:
        print("STDOUT:", (p.stdout or "")[-600:], flush=True)
        print("STDERR:", (p.stderr or "")[-600:], flush=True)
    return ok


print(f"=== report_worker 啟動,{START_DELAY}s 後開始檢查 ===", flush=True)
time.sleep(START_DELAY)
while True:
    if all(fresh(r, INTERVAL) for r in REPORTS):
        print(f"{datetime.now():%H:%M:%S} 兩份報告仍新,略過本輪", flush=True)
    else:
        run("stock_analyzer.py")     # 自選股深度報告(輕,約數十秒)
        run("scan_all_stocks.py")    # 全市場掃描報告(重,約數分鐘)
        print(f"{datetime.now():%H:%M:%S} 本輪完成,{INTERVAL // 3600}h 後再檢查", flush=True)
    time.sleep(INTERVAL)
