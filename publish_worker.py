# -*- coding: utf-8 -*-
"""
數據發布器:定期從本機 app.py 的 API 抓最新分析結果,
推到 GitHub repo 的 data 分支(單一 commit,amend+force push,不累積歷史),
GitHub Pages 網頁從 raw.githubusercontent.com 讀取。

用法: 先跑 python app.py,再跑 python publish_worker.py
"""
import json
import os
import subprocess
import sys
import time
from datetime import datetime

import requests

sys.stdout.reconfigure(encoding="utf-8", line_buffering=True)  # 被導向到 log 檔時仍即時逐行寫入

REPO_DIR = os.path.dirname(os.path.abspath(__file__))
CLONE_DIR = os.path.join(REPO_DIR, ".dataclone")
REMOTE = "https://github.com/souptomorrow-collab/stock-live-dashboard.git"
API = "http://127.0.0.1:8000"
WATCH_INTERVAL = 20   # 自選股發布間隔(秒)— 近即時
MARKET_EVERY = 15     # 每 N 輪才發布一次全市場(15 × 20s = 5 分鐘)
# 在 pythonw(無主控台)下執行 git 時,避免每個 git.exe 彈出一個 cmd 黑窗
NO_WINDOW = subprocess.CREATE_NO_WINDOW if sys.platform == "win32" else 0


def git(*args, cwd=CLONE_DIR):
    r = subprocess.run(["git", *args], cwd=cwd, capture_output=True, text=True,
                       creationflags=NO_WINDOW)
    if r.returncode != 0:
        raise RuntimeError(f"git {' '.join(args)}: {r.stderr.strip()}")
    return r.stdout.strip()


def setup():
    if os.path.exists(os.path.join(CLONE_DIR, ".git")):
        return
    print("初始化 data 分支 clone...")
    git("clone", "--depth", "1", REMOTE, CLONE_DIR, cwd=REPO_DIR)
    try:
        git("checkout", "data")
    except RuntimeError:
        git("checkout", "--orphan", "data")
        git("rm", "-rf", "--quiet", ".")


def publish(include_market=True):
    now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    targets = [("watch.json", "/api/watch")]
    if include_market:
        targets.append(("market.json", "/api/market"))
    ok = False
    for name, path in targets:
        try:
            data = requests.get(API + path, timeout=15).json()
            if not data.get("quotes"):
                continue
            data["published_at"] = now
            with open(os.path.join(CLONE_DIR, name), "w", encoding="utf-8") as f:
                json.dump(data, f, ensure_ascii=False, separators=(",", ":"))
            ok = True
        except Exception as e:
            print(f"  [{name}] 抓取失敗: {e}")
    if not ok:
        print(f"{now} 本機伺服器無資料(app.py 沒在跑?),略過")
        return
    git("add", "-A")
    has_head = subprocess.run(["git", "rev-parse", "HEAD"], cwd=CLONE_DIR,
                              capture_output=True, creationflags=NO_WINDOW).returncode == 0
    if has_head:
        git("commit", "--amend", "-m", f"data snapshot {now}", "--allow-empty")
    else:
        git("commit", "-m", f"data snapshot {now}")
    git("push", "-f", "-u", "origin", "data")
    print(f"{now} ✅ 已發布 ({'自選股+全市場' if include_market else '自選股'})", flush=True)


if __name__ == "__main__":
    setup()
    print(f"自選股每 {WATCH_INTERVAL}s、全市場每 {WATCH_INTERVAL * MARKET_EVERY // 60} 分鐘發布,Ctrl+C 停止")
    tick = 0
    while True:
        try:
            publish(include_market=(tick % MARKET_EVERY == 0))
        except Exception as e:
            print(f"[錯誤] {e}")
        tick += 1
        time.sleep(WATCH_INTERVAL)
