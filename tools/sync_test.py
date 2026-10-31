"""量直播的影音偏移：螢幕左上角方塊每 2 秒閃白 0.1 秒，同時往 CABLE Input 放 1 kHz 嗶聲；
另一邊用直播同一套 ffmpeg 擷取錄成檔，比對閃光與嗶聲出現的時間。

用法：python tools/sync_test.py [ffmpeg 音訊濾鏡，例如 "adelay=100:all=1"]
結果「beep - flash」是負的＝聲音比畫面早，把那個毫秒數填進 config 的 live_audio_delay_ms。
測試時螢幕左上角會閃約 10 秒；嗶聲只送進 CABLE Input，喇叭聽不到。
"""
import re
import subprocess
import sys
import threading
import time
import tkinter as tk
from pathlib import Path

import tempfile

import numpy as np
import soundcard as sc

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from conf import C  # noqa: E402

FF = C["ffmpeg"]
OUT = Path(tempfile.gettempdir()) / "sync_test.ts"
AF = sys.argv[1] if len(sys.argv) > 1 else "anull"
DUR = 12

cmd = [FF, "-y", "-hide_banner", "-loglevel", "error",
       "-f", "lavfi", "-i", "ddagrab=output_idx=0:framerate=30,hwdownload,format=bgra,scale=1280:720,format=nv12",
       "-f", "dshow", "-audio_buffer_size", "50", "-i", f"audio={C['cable_capture_dshow']}",
       "-af", AF, "-t", str(DUR), "-c:v", "h264_nvenc", "-b:v", "3000k", "-g", "60",
       "-c:a", "aac", "-b:a", "128k", "-ar", "48000", str(OUT)]

root = tk.Tk()
root.overrideredirect(True)
root.attributes("-topmost", True)
root.geometry("300x300+0+0")
root.configure(bg="black")
cable = sc.get_speaker(C["cable_render_name"])
beep = (0.8 * np.sin(2 * np.pi * 1000 * np.arange(4800) / 48000)).astype(np.float32)
beep = np.stack([beep, beep], axis=1)


def flasher():
    time.sleep(2.0)
    with cable.player(samplerate=48000, channels=2, blocksize=256) as p:
        for _ in range(4):
            root.after(0, lambda: root.configure(bg="white"))
            p.play(beep)                      # 0.1 秒
            root.after(0, lambda: root.configure(bg="black"))
            time.sleep(1.9)
    time.sleep(1.5)
    root.after(0, root.destroy)


proc = subprocess.Popen(cmd)
time.sleep(0.8)
threading.Thread(target=flasher, daemon=True).start()
root.mainloop()
proc.wait()

# 影像：左上角 150x150 的平均亮度，找由暗轉亮的時間
v = subprocess.run([FF, "-hide_banner", "-i", str(OUT), "-vf",
                    "crop=150:150:20:20,signalstats,metadata=print:key=lavfi.signalstats.YAVG",
                    "-f", "null", "-"], capture_output=True, text=True).stderr
ts = [float(x) for x in re.findall(r"pts_time:([\d.]+)", v)]
ys = [float(x) for x in re.findall(r"YAVG=([\d.]+)", v)]
flashes = [ts[i] for i in range(1, len(ys)) if ys[i] > 128 and ys[i - 1] <= 128]

# 聲音：解成 48k 單聲道，找能量由靜轉響的時間
raw = subprocess.run([FF, "-hide_banner", "-loglevel", "error", "-i", str(OUT), "-vn", "-ac", "1",
                      "-ar", "48000", "-f", "f32le", "-"], capture_output=True).stdout
a = np.frombuffer(raw, dtype=np.float32)
win = 240   # 5 ms
env = np.abs(a[: len(a) // win * win]).reshape(-1, win).max(axis=1)
on = [i * win / 48000 for i in range(1, len(env)) if env[i] > 0.2 and env[i - 1] <= 0.2]
def first_pts(sel):
    out = subprocess.run([FF, "-hide_banner", "-loglevel", "error", "-i", str(OUT), "-map", sel,
                          "-c", "copy", "-f", "framecrc", "-"], capture_output=True, text=True).stdout
    num, den = map(int, re.search(r"#tb \d+: (\d+)/(\d+)", out).group(1, 2))
    first = next(l for l in out.splitlines() if l and not l.startswith("#"))
    return int(first.split(",")[2]) * num / den


# 影片 pts 是絕對時間；解出來的聲音從第一個音訊樣本算起，加回音訊起點才在同一條時間軸
a0 = first_pts("0:a")
print("video start", round(first_pts("0:v"), 3), "audio start", round(a0, 3))
on = [b + a0 for b in on]
print("flash  :", [round(x, 3) for x in flashes])
print("beep   :", [round(x, 3) for x in on])
pairs = [(f, min(on, key=lambda b: abs(b - f))) for f in flashes] if on else []
print("beep - flash (ms):", [round((b - f) * 1000) for f, b in pairs])
