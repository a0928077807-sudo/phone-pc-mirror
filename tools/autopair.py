"""Moonlight 配對小幫手：等手機發配對請求，從手機畫面文字讀 PIN，自動送進 Sunshine。

為什麼需要：新版 Sunshine 送出 PIN 後要等整個握手完成，手機上的 Moonlight 一被切到背景
（例如切去聊天軟體回報 PIN）配對就失敗。用這支，手機全程停在 Moonlight 的 PIN 畫面就好。

用法：python tools/autopair.py [adb 序號]   （手機要能用 adb 連上；不給序號就用第一台）
讀畫面用 uiautomator dump，只讀文字、不截圖。
"""
import base64
import json
import re
import ssl
import subprocess
import sys
import time
import urllib.request
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from conf import C  # noqa: E402

ADB = str(Path(C["scrcpy"]).with_name("adb.exe"))
AUTH = "Basic " + base64.b64encode(f"{C['sunshine_user']}:{C['sunshine_password']}".encode()).decode()
CTX = ssl._create_unverified_context()


def serial():
    if len(sys.argv) > 1:
        return sys.argv[1]
    out = subprocess.run([ADB, "devices"], capture_output=True, text=True).stdout
    devs = [l.split("\t")[0] for l in out.splitlines()[1:] if l.endswith("\tdevice")]
    if not devs:
        raise SystemExit("adb 沒有連著的手機")
    return devs[0]


def api(method, path, body=None):
    req = urllib.request.Request(C["sunshine_url"] + path, method=method,
                                 data=json.dumps(body).encode() if body else None,
                                 headers={"Authorization": AUTH, "Content-Type": "application/json",
                                          "Origin": C["sunshine_url"]})
    with urllib.request.urlopen(req, context=CTX, timeout=60) as r:
        return json.loads(r.read())


def read_pin(dev):
    subprocess.run([ADB, "-s", dev, "shell", "uiautomator", "dump", "/sdcard/ui.xml"],
                   capture_output=True, timeout=20)
    xml = subprocess.run([ADB, "-s", dev, "shell", "cat", "/sdcard/ui.xml"],
                         capture_output=True, text=True, encoding="utf-8", timeout=20).stdout
    subprocess.run([ADB, "-s", dev, "shell", "rm", "/sdcard/ui.xml"], capture_output=True)
    m = re.search(r"PIN[^\"]*?(\d{4})", xml)
    return m.group(1) if m else None


def main():
    dev = serial()
    print("等 Moonlight 發配對請求（在手機上點電腦）…")
    deadline = time.time() + 600
    while time.time() < deadline:
        pend = api("GET", "/api/pin").get("pairings", [])
        if pend:
            pid = pend[-1]["id"]
            pin = None
            for _ in range(5):
                pin = read_pin(dev)
                if pin:
                    break
                time.sleep(1)
            print(f"配對請求 {pid}，PIN {pin}")
            if pin:
                print("結果", api("POST", "/api/pin", {"pairing_id": pid, "pin": pin, "name": "phone"}))
                return
        time.sleep(1.5)
    print("十分鐘內沒有配對請求")


if __name__ == "__main__":
    main()
