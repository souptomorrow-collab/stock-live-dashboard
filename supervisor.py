# -*- coding: utf-8 -*-
"""
監督程式(watchdog):讓公開即時網頁的發布 pipeline 穩定不中斷。

職責:
1. 啟動 app.py(本機即時分析伺服器),並等它健康(/api/watch 有資料)
2. 啟動 publish_worker.py(每 5 分鐘把分析結果推到 GitHub data 分支)
3. 持續監看;任一程序崩潰就自動重啟,並寫入 logs/

搭配 Windows 工作排程器(開機/登入自動執行)即可全自動。
手動測試: python supervisor.py    (Ctrl+C 停止,會一併關閉子程序)
"""
import os
import shutil
import subprocess
import sys
import time
from datetime import datetime

import requests

# pythonw.exe(開機自動啟動用)沒有主控台,sys.stdout/stderr 會是 None;
# 先補成可寫對象再 reconfigure,否則啟動瞬間就 AttributeError 崩潰。
if sys.stdout is None:
    sys.stdout = open(os.devnull, "w", encoding="utf-8")
if sys.stderr is None:
    sys.stderr = sys.stdout
try:
    sys.stdout.reconfigure(encoding="utf-8")
except Exception:
    pass

ROOT = os.path.dirname(os.path.abspath(__file__))
LOG_DIR = os.path.join(ROOT, "logs")
PY = sys.executable  # 用啟動本程式的同一個 Python
API = "http://127.0.0.1:8000"
HEALTH_TIMEOUT = 480   # app.py 載入台股+美股+加密貨幣歷史約 3-5 分鐘,給足 8 分鐘
RESTART_DELAY = 10     # 子程序崩潰後重啟前等待秒數
NO_WINDOW = subprocess.CREATE_NO_WINDOW if sys.platform == "win32" else 0
TUNNEL_ID = "stock-dash"   # devtunnel 持久通道:對外公開分享的固定網址

os.makedirs(LOG_DIR, exist_ok=True)


def log(msg):
    line = f"{datetime.now():%Y-%m-%d %H:%M:%S} {msg}"
    print(line, flush=True)
    with open(os.path.join(LOG_DIR, "supervisor.log"), "a", encoding="utf-8") as f:
        f.write(line + "\n")


def find_devtunnel():
    """找 devtunnel.exe(公開分享通道用),找不到回 None。"""
    p = shutil.which("devtunnel")
    if p:
        return p
    cand = os.path.join(os.environ.get("LOCALAPPDATA", ""), "Microsoft", "WinGet",
                        "Packages",
                        "Microsoft.devtunnel_Microsoft.Winget.Source_8wekyb3d8bbwe",
                        "devtunnel.exe")
    return cand if os.path.exists(cand) else None


def spawn(name, cmd):
    """啟動子程序 cmd(list),輸出導向 logs/<name>.log,且不彈出主控台視窗。"""
    out = open(os.path.join(LOG_DIR, name + ".log"),
               "a", encoding="utf-8", buffering=1)
    out.write(f"\n===== 啟動 {datetime.now():%Y-%m-%d %H:%M:%S} =====\n")
    return subprocess.Popen(cmd, cwd=ROOT, stdout=out, stderr=subprocess.STDOUT,
                            creationflags=NO_WINDOW)


def wait_healthy():
    """輪詢 app.py 直到 /api/watch 回傳有報價(代表歷史載入完、開始更新)"""
    deadline = time.time() + HEALTH_TIMEOUT
    while time.time() < deadline:
        try:
            d = requests.get(API + "/api/watch", timeout=5).json()
            if d.get("quotes"):
                return True
        except Exception:
            pass
        time.sleep(5)
    return False


def main():
    log("=== Supervisor 啟動 ===")
    procs = {}   # name -> Popen
    cmds = {     # name -> 重啟用指令
        "app": [PY, os.path.join(ROOT, "app.py")],
        "publish_worker": [PY, os.path.join(ROOT, "publish_worker.py")],
    }
    dt = find_devtunnel()
    if dt:
        cmds["devtunnel"] = [dt, "host", TUNNEL_ID]

    # 先起 app.py,等它健康後再起其餘
    procs["app"] = spawn("app", cmds["app"])
    log("已啟動 app.py,等待健康檢查(載入歷史中,約 3-5 分鐘)...")
    if wait_healthy():
        log("app.py 健康 ✅")
    else:
        log("app.py 在時限內未就緒 ⚠️(仍繼續監看)")
    procs["publish_worker"] = spawn("publish_worker", cmds["publish_worker"])
    log("已啟動 publish_worker。")
    if "devtunnel" in cmds:
        procs["devtunnel"] = spawn("devtunnel", cmds["devtunnel"])
        log("已啟動 devtunnel host(公開分享通道)。")
    else:
        log("未找到 devtunnel,略過公開分享通道。")

    # 監看迴圈:任一程序退出就重啟
    while True:
        time.sleep(15)
        for name, p in list(procs.items()):
            if p.poll() is None:
                continue  # 還活著
            log(f"⚠️ {name} 已退出(rc={p.returncode}),{RESTART_DELAY}s 後重啟")
            time.sleep(RESTART_DELAY)
            if name == "publish_worker" and procs["app"].poll() is not None:
                continue  # app 也掛了,下一輪會先處理 app
            procs[name] = spawn(name, cmds[name])
            log(f"已重啟 {name}")


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        log("收到中斷,結束。(子程序由 OS 回收)")
