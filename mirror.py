"""手機投影到電腦：自動選 USB → 同 Wi-Fi → Tailscale，用 scrcpy 投畫面＋聲音。

用法：python mirror.py [auto|usb|wifi|ts]
"""
import asyncio
import json
import re
import subprocess
import sys
import time
from pathlib import Path

from conf import C

HERE = Path(__file__).resolve().parent
SCRCPY = Path(C["scrcpy"] or "scrcpy")   # 沒填就從 PATH 找
ADB = SCRCPY.with_name("adb.exe") if C["scrcpy"] else Path("adb")   # 用 scrcpy 附的 adb，版本才不會打架
TS_IP = C["phone_tailscale_ip"]     # 手機的 Tailscale IP，留空就不走這條
STATE = HERE / "last_port.json"     # 記住上次的無線偵錯埠，下次先試
LOG = HERE / "scrcpy.log"
SETTINGS = HERE / "settings.json"   # 控制頁存的開關，捷徑與控制頁共用
DEFAULTS = {
    # 手機投電腦：聲音 pc/phone/both、電腦能不能操作手機、視窗 normal/borderless/fullscreen、置頂
    "p2c": {"audio": "pc", "control": True, "window": "normal", "top": False},
    "c2p": {"audio": "phone", "control": True},  # 電腦投手機：聲音 phone/pc/both、手機能不能操作電腦
}

COMMON = ["--video-codec=h265", "--audio-codec=opus", "--audio-bit-rate=192K",
          f"--window-title={C['window_title']}"]
PROFILES = {
    # USB 頻寬夠，開滿：原生解析度、120fps、高位元率、最小緩衝
    "usb": ["--video-bit-rate=24M", "--max-fps=120", "--audio-buffer=40"],
    # 同一個 Wi-Fi：位元率降一點，聲音多一點緩衝防爆音
    "wifi": ["--video-bit-rate=16M", "--max-fps=120", "--audio-buffer=60"],
    # Tailscale 可能走中繼，降解析度、加緩衝換穩定
    "ts": ["--video-bit-rate=6M", "--max-size=1600", "--max-fps=60",
           "--video-buffer=80", "--audio-buffer=150"],
}
NAMES = {"usb": "USB", "wifi": "Wi-Fi", "ts": "Tailscale"}


def adb(*args, timeout=10):
    # adb 的錯誤訊息可能帶 UTF-8 中文；不指定編碼時 Windows 用 cp950 解碼失敗，stdout 會變 None
    r = subprocess.run([str(ADB), *args], capture_output=True, text=True, encoding="utf-8", errors="replace",
                       timeout=timeout, creationflags=subprocess.CREATE_NO_WINDOW)
    return (r.stdout or "") + (r.stderr or "")


def devices():
    """回傳 [(serial, state)]。"""
    out = adb("devices")
    return [tuple(l.split("\t")[:2]) for l in out.splitlines()[1:] if "\t" in l]


def ready(serial):
    return any(s == serial and st == "device" for s, st in devices())


def kind_of(serial):
    if TS_IP and serial.startswith(TS_IP):
        return "ts"
    if ":" in serial or "._adb-tls-connect" in serial:
        return "wifi"
    return "usb"


def find_usb():
    return next((s for s, st in devices() if st == "device" and kind_of(s) == "usb"), None)


def find_wifi():
    # 已經連著的區網裝置
    for s, st in devices():
        if st == "device" and kind_of(s) == "wifi":
            return s
    # 用 mDNS 找同網段開著無線偵錯的手機
    for _ in range(3):
        out = adb("mdns", "services")
        for m in re.finditer(r"_adb-tls-connect\._tcp\.?\s+(\d+\.\d+\.\d+\.\d+:\d+)", out):
            addr = m.group(1)
            adb("connect", addr)
            if ready(addr):
                return addr
        time.sleep(1.5)
    return None


async def _scan(ip, lo, hi):
    sem = asyncio.Semaphore(800)
    found = []

    async def probe(port):
        async with sem:
            try:
                _, w = await asyncio.wait_for(asyncio.open_connection(ip, port), 2)
                found.append(port)
                w.close()
            except Exception:
                pass

    await asyncio.gather(*(probe(p) for p in range(lo, hi)))
    return sorted(found)


def find_ts():
    if not TS_IP:
        return None
    for s, st in devices():
        if st == "device" and kind_of(s) == "ts":
            return s

    def try_port(p):
        addr = f"{TS_IP}:{p}"
        adb("connect", addr, timeout=15)
        if ready(addr):
            STATE.write_text(json.dumps({"port": p}))
            return addr
        return None

    last = json.loads(STATE.read_text())["port"] if STATE.exists() else None
    if last and (addr := try_port(last)):
        return addr
    print("  掃描無線偵錯埠（約半分鐘）…")
    for p in asyncio.run(_scan(TS_IP, 32768, 61000)):
        if p != last and (addr := try_port(p)):
            return addr
    return None


def ts_direct():
    """Tailscale 是直連（不是走 DERP 中繼）就能用 Wi-Fi 等級的畫質。"""
    try:
        r = subprocess.run(["tailscale", "ping", "-c", "1", TS_IP], capture_output=True,
                           text=True, timeout=10, creationflags=subprocess.CREATE_NO_WINDOW)
    except Exception:
        return False
    return "pong" in r.stdout and "DERP" not in r.stdout


def audio_source(serial):
    """通話中（微信等 VoIP 會把手機切進 MODE_IN_COMMUNICATION）output 收不到任何聲音，改用 playback。"""
    out = adb("-s", serial, "shell", "dumpsys audio | grep -m1 'Actual mode'")
    return "playback" if re.search(r"MODE_IN_(CALL|COMMUNICATION)", out) else "output"


def pick(mode):
    order = ["usb", "wifi", "ts"] if mode == "auto" else [mode]
    finders = {"usb": find_usb, "wifi": find_wifi, "ts": find_ts}
    for k in order:
        print(f"找手機：{NAMES[k]}…")
        s = finders[k]()
        if s:
            return k, s
    return None, None


def load_settings():
    s = json.loads(json.dumps(DEFAULTS))
    try:
        saved = json.loads(SETTINGS.read_text(encoding="utf-8"))
        for k, v in saved.items():
            if isinstance(s.get(k), dict) and isinstance(v, dict):
                s[k].update(v)
            else:
                s[k] = v
    except (OSError, ValueError):
        pass
    return s


def save_settings(s):
    tmp = SETTINGS.with_suffix(".tmp")
    tmp.write_text(json.dumps(s, ensure_ascii=False, indent=2), encoding="utf-8")
    tmp.replace(SETTINGS)


def audio_args(serial, audio):
    if audio == "phone":    # 只在手機出聲：不傳聲音
        return ["--no-audio"]
    if audio == "both":     # 兩邊都出：只有 playback 能 dup
        return ["--audio-source=playback", "--audio-dup"]
    return [f"--audio-source={audio_source(serial)}"]


def window_rect():
    """目前 scrcpy 視窗內容區的位置與大小（換視窗樣式重開時放回原處）；沒開就回 None。"""
    import ctypes
    from ctypes import wintypes
    u = ctypes.windll.user32
    h = u.FindWindowW(None, C["window_title"])
    if not h:
        return None
    r, pt = wintypes.RECT(), wintypes.POINT(0, 0)
    u.GetClientRect(h, ctypes.byref(r))
    u.ClientToScreen(h, ctypes.byref(pt))
    if r.right < 50 or r.bottom < 50:
        return None
    return pt.x, pt.y, r.right, r.bottom


def build_args(kind, serial, p2c, rect=None):
    profile = kind
    if kind == "ts" and ts_direct():
        profile = "wifi"
    args = [str(SCRCPY), f"--serial={serial}", *COMMON, *PROFILES[profile],
            *audio_args(serial, p2c["audio"])]
    if not p2c["control"]:
        args.append("--no-control")
    win = p2c.get("window", "normal")
    if win == "borderless":
        args.append("--window-borderless")
    elif win == "fullscreen":
        args.append("--fullscreen")
    if p2c.get("top"):
        args.append("--always-on-top")
    if rect and win != "fullscreen":
        x, y, w, h = rect
        args += [f"--window-x={x}", f"--window-y={y}", f"--window-width={w}", f"--window-height={h}"]
    return args


def launch(args):
    log = open(LOG, "w", encoding="utf-8")
    return subprocess.Popen(args, stdout=log, stderr=subprocess.STDOUT,
                            creationflags=subprocess.CREATE_NO_WINDOW)


def main():
    mode = sys.argv[1] if len(sys.argv) > 1 else "auto"
    adb("start-server")
    kind, serial = pick(mode)
    if not serial:
        print("\n找不到手機。USB 有插好並允許偵錯？或手機有開「無線偵錯」？")
        input("按 Enter 關閉")
        return
    args = build_args(kind, serial, load_settings()["p2c"])
    print(f"用 {NAMES[kind]} 連線：{serial}，開投影…")
    p = launch(args)
    # 等幾秒，啟動失敗就把錯誤秀出來
    for _ in range(8):
        time.sleep(0.5)
        if p.poll() is not None:
            print("\n投影沒開起來：\n" + LOG.read_text(encoding="utf-8")[-1500:])
            input("按 Enter 關閉")
            return


if __name__ == "__main__":
    main()
