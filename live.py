"""電腦畫面網頁直播：ffmpeg 擷取桌面＋CABLE Output 的聲音，切成 HLS，給 live_public_url 的觀看頁。

延遲幾秒換流暢與影音同步（遠端網路可能慢，用 HLS 緩衝吃掉抖動）。
觀看端只綁 127.0.0.1:47381，靠 cloudflared 對外；要密碼，遠端操作另外要控制頁打開。
"""
import ctypes
import hashlib
import hmac
import json
import secrets
import shutil
import subprocess
import threading
import time
from ctypes import wintypes
from http.cookies import SimpleCookie
from http.server import BaseHTTPRequestHandler
from pathlib import Path

from conf import C

HERE = Path(__file__).resolve().parent
WEB = HERE / "live"
HLS = WEB / "hls"
FFMPEG = Path(C["ffmpeg"])
FF_LOG = HERE / "live_ffmpeg.log"
NOWIN = subprocess.CREATE_NO_WINDOW
# 直播專用的 cloudflared 通道，控制頁開著就連著；沒開播時對方看到的是密碼頁與「還沒開始」
CLOUDFLARED = Path(C["cloudflared"])
TUNNEL_CFG = HERE / C["live_tunnel_config"]
TUNNEL_LOG = HERE / "cloudflared-live.log"

QUALITY = {
    # 流暢：720p 單一畫質、2 秒一段，延遲約 6～10 秒
    "smooth": {"seg": 2, "list": 8, "ladder": [("1280:720", "1500k")]},
    # 高畫質穩定：1080p＋720p 兩層，播放器依網速自動切；4 秒一段、緩衝大，延遲約 15 秒
    "hq": {"seg": 4, "list": 10, "ladder": [("1920:1080", "5000k"), ("1280:720", "2500k")]},
}
# 實測（tools/sync_test.py）：dshow 的聲音比 ddagrab 的畫面早約 100 ms，延後補齊；
# aresample async 修長時間的時鐘漂移。每台電腦可能不同，量了填進 config 的 live_audio_delay_ms
AUDIO_FIX = f"adelay={int(C['live_audio_delay_ms'])}:all=1,aresample=async=1000"


def build_ffmpeg(q, fps=30):
    """擷取一次桌面與聲音，依 ladder 編一到多層畫質；多層時另出 index.m3u8 主清單。"""
    n = len(q["ladder"])
    gop = fps * q["seg"]   # 每段開頭都是關鍵幀，各層對齊才能無縫切換
    fc = f"[0:v]hwdownload,format=bgra,split={n}" + "".join(f"[s{i}]" for i in range(n)) + ";"
    fc += "".join(f"[s{i}]scale={size},format=nv12[v{i}];" for i, (size, _) in enumerate(q["ladder"]))
    fc += f"[1:a]{AUDIO_FIX},asplit={n}" + "".join(f"[a{i}]" for i in range(n))
    args = [str(FFMPEG), "-hide_banner", "-loglevel", "warning",
            "-f", "lavfi", "-i", f"ddagrab=output_idx=0:framerate={fps}",
            "-f", "dshow", "-audio_buffer_size", "50", "-i", f"audio={C['cable_capture_dshow']}",
            "-filter_complex", fc]
    for i in range(n):
        args += ["-map", f"[v{i}]", "-map", f"[a{i}]"]
    args += ["-c:v", "h264_nvenc", "-preset", "p5", "-profile:v", "high",
             "-g", str(gop), "-keyint_min", str(gop), "-no-scenecut", "1", "-strict_gop", "1"]
    for i, (_, br) in enumerate(q["ladder"]):
        kb = int(br.rstrip("k")) * (3 if fps >= 60 else 2) // 2   # 60 fps 位元率多 50%
        args += [f"-b:v:{i}", f"{kb}k", f"-maxrate:v:{i}", f"{kb}k", f"-bufsize:v:{i}", f"{kb * 2}k"]
    args += ["-c:a", "aac", "-b:a", "128k", "-ar", "48000", "-ac", "2",
             "-f", "hls", "-hls_time", str(q["seg"]), "-hls_list_size", str(q["list"]),
             "-hls_flags", "delete_segments+independent_segments+omit_endlist"]
    if n == 1:
        args += ["-hls_segment_filename", str(HLS / "s_%05d.ts"), str(HLS / "index.m3u8")]
    else:
        args += ["-master_pl_name", "index.m3u8",
                 "-var_stream_map", " ".join(f"v:{i},a:{i}" for i in range(n)),
                 "-hls_segment_filename", str(HLS / "v%v_%05d.ts"), str(HLS / "v%v.m3u8")]
    return args


def kill_strays():
    """控制頁重開時，上一輪留下的 ffmpeg 還在寫 HLS，先清掉。"""
    ps = (f"Get-CimInstance Win32_Process | Where-Object {{ "
          f"($_.Name -eq 'ffmpeg.exe' -and $_.CommandLine -like '*{HLS.name}*_%05d*') -or "
          f"($_.Name -eq 'cloudflared.exe' -and $_.CommandLine -like '*{TUNNEL_CFG.name}*') }} | "
          f"ForEach-Object {{ Stop-Process -Id $_.ProcessId -Force }}")
    subprocess.run(["powershell", "-NoProfile", "-Command", ps], creationflags=NOWIN, timeout=30)


PW_PERIOD = 300   # 動態密碼每 5 分鐘換一組
PW_CHARS = "abcdefghjkmnpqrstuvwxyz23456789"   # 去掉 i l o 0 1 這種容易看錯的


def _password_at(secret, window):
    """由 secret 與時間窗算出那 5 分鐘的密碼（跟 TOTP 同一個道理，不用存）。"""
    d = hmac.new(secret.encode(), f"pw:{window}".encode(), hashlib.sha256).digest()
    return "".join(PW_CHARS[b % len(PW_CHARS)] for b in d[:6])


def current_password(s, now=None):
    return _password_at(s["secret"], int((now or time.time()) // PW_PERIOD))


def password_seconds_left(now=None):
    now = now or time.time()
    return int(PW_PERIOD - now % PW_PERIOD)


def password_ok(s, pw, now=None):
    """這一組或上一組都收：密碼剛換時對方可能還在輸入舊的。"""
    w = int((now or time.time()) // PW_PERIOD)
    pw = str(pw).strip().lower()
    return any(hmac.compare_digest(pw, _password_at(s["secret"], x)) for x in (w, w - 1))


class Live:
    def __init__(self):
        self.proc = None
        self.tunnel = None
        self.wanted = False       # 使用者要它開著；ffmpeg 自己掛掉就由 keepalive() 重開
        self.quality = "smooth"
        self.fps = 30
        self.sessions = {}        # 觀看者 id -> 最後一次看到的時間（算幾個人在看）
        self.fails = {}           # IP -> [失敗時間]
        self.lock = threading.Lock()

    def running(self):
        return self.proc is not None and self.proc.poll() is None

    def tunnel_up(self):
        return self.tunnel is not None and self.tunnel.poll() is None

    def ensure_tunnel(self):
        """通道跟著控制頁一直開著：對方先打開網址也看得到密碼頁與「還沒開播」，不會撞到 Cloudflare 錯誤頁。"""
        if not self.tunnel_up():
            self.tunnel = subprocess.Popen(
                [str(CLOUDFLARED), "tunnel", "--config", str(TUNNEL_CFG), "--logfile", str(TUNNEL_LOG), "run"],
                stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, creationflags=NOWIN)

    def start(self, quality, fps=None):
        self._kill()
        self.ensure_tunnel()
        self.wanted, self.quality = True, quality
        if fps:
            self.fps = fps
        shutil.rmtree(HLS, ignore_errors=True)
        HLS.mkdir(parents=True, exist_ok=True)
        args = build_ffmpeg(QUALITY.get(quality, QUALITY["smooth"]), self.fps)
        self.proc = subprocess.Popen(args, stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                                     stderr=open(FF_LOG, "w", encoding="utf-8"), creationflags=NOWIN)

    def stop(self):
        self.wanted = False
        self._kill()

    def keepalive(self):
        if self.wanted and not self.running():
            self.start(self.quality)
            return True
        return False

    def _kill(self):
        if self.running():
            self.proc.terminate()
            try:
                self.proc.wait(5)
            except subprocess.TimeoutExpired:
                self.proc.kill()
        self.proc = None

    def viewers(self):
        now = time.time()
        with self.lock:
            return sum(1 for t in self.sessions.values() if now - t < 15)


# ---------- 遠端操作（SendInput） ----------

user32 = ctypes.WinDLL("user32", use_last_error=True)
ULONG_PTR = ctypes.c_size_t


class MOUSEINPUT(ctypes.Structure):
    _fields_ = [("dx", wintypes.LONG), ("dy", wintypes.LONG), ("mouseData", wintypes.DWORD),
                ("dwFlags", wintypes.DWORD), ("time", wintypes.DWORD), ("dwExtraInfo", ULONG_PTR)]


class KEYBDINPUT(ctypes.Structure):
    _fields_ = [("wVk", wintypes.WORD), ("wScan", wintypes.WORD), ("dwFlags", wintypes.DWORD),
                ("time", wintypes.DWORD), ("dwExtraInfo", ULONG_PTR)]


class _U(ctypes.Union):
    _fields_ = [("mi", MOUSEINPUT), ("ki", KEYBDINPUT), ("pad", ctypes.c_byte * 32)]


class INPUT(ctypes.Structure):
    _fields_ = [("type", wintypes.DWORD), ("u", _U)]


MOVE, ABS = 0x0001, 0x8000
LDOWN, LUP, RDOWN, RUP, WHEEL = 0x0002, 0x0004, 0x0008, 0x0010, 0x0800
KEYUP, UNICODE = 0x0002, 0x0004
VK = {"enter": 0x0D, "backspace": 0x08, "esc": 0x1B, "tab": 0x09,
      "left": 0x25, "up": 0x26, "right": 0x27, "down": 0x28}


def _send(*inputs):
    arr = (INPUT * len(inputs))(*inputs)
    user32.SendInput(len(inputs), arr, ctypes.sizeof(INPUT))


def _mouse(flags, dx=0, dy=0, data=0):
    i = INPUT(type=0)
    i.u.mi = MOUSEINPUT(dx, dy, ctypes.c_ulong(data & 0xFFFFFFFF).value, flags, 0, 0)
    return i


def _key(vk=0, scan=0, flags=0):
    i = INPUT(type=1)
    i.u.ki = KEYBDINPUT(vk, scan, flags, 0, 0)
    return i


def do_input(ev):
    """ev 的 x、y 是 0～1，相對於主螢幕（ddagrab output 0）。"""
    t = ev.get("type")
    if t in ("click", "right", "move"):
        x = min(max(float(ev["x"]), 0), 1)
        y = min(max(float(ev["y"]), 0), 1)
        _send(_mouse(MOVE | ABS, int(x * 65535), int(y * 65535)))
        if t == "click":
            _send(_mouse(LDOWN), _mouse(LUP))
        elif t == "right":
            _send(_mouse(RDOWN), _mouse(RUP))
    elif t == "scroll":
        _send(_mouse(WHEEL, data=int(ev.get("dy", 0)) * 120))
    elif t == "text":
        seq = []
        data = str(ev.get("text", ""))[:500].encode("utf-16-le")
        for i in range(0, len(data), 2):
            code = int.from_bytes(data[i:i + 2], "little")
            seq += [_key(scan=code, flags=UNICODE), _key(scan=code, flags=UNICODE | KEYUP)]
        if seq:
            _send(*seq)
    elif t == "key" and ev.get("key") in VK:
        vk = VK[ev["key"]]
        _send(_key(vk), _key(vk, flags=KEYUP))


# ---------- 觀看端 HTTP ----------

def _sign(s, vid):
    """cookie＝觀看者 id.簽章。簽章只綁 secret 不綁密碼：密碼每 5 分鐘換，已經進來的人不會被踢；
    要踢掉所有人就換 secret（控制頁「踢掉所有人」）。服務重開不必重登。"""
    return hmac.new(s["secret"].encode(), f"session:{vid}".encode(), hashlib.sha256).hexdigest()[:32]


def make_handler(live, get_settings):
    """get_settings() 回傳 settings["live"]（含 secret、control）。"""

    class ViewerHandler(BaseHTTPRequestHandler):
        def log_message(self, *a):
            pass

        def _send(self, code, body, ctype="application/json; charset=utf-8", headers=None):
            data = body if isinstance(body, bytes) else json.dumps(body, ensure_ascii=False).encode()
            self.send_response(code)
            self.send_header("Content-Type", ctype)
            self.send_header("Cache-Control", "no-store")
            for k, v in (headers or {}).items():
                self.send_header(k, v)
            self.end_headers()
            self.wfile.write(data)

        def _token(self):
            c = SimpleCookie(self.headers.get("Cookie") or "")
            vid, _, sig = (c["lv"].value if "lv" in c else "").partition(".")
            if not vid or not hmac.compare_digest(sig, _sign(get_settings(), vid)):
                return None
            with live.lock:
                live.sessions[vid] = time.time()
            return vid

        def _ip(self):
            return self.headers.get("CF-Connecting-IP") or self.client_address[0]

        def do_GET(self):
            path = self.path.split("?")[0]
            authed = self._token() is not None
            if path == "/":
                page = "watch.html" if authed else "login.html"
                return self._send(200, (WEB / page).read_bytes(), "text/html; charset=utf-8")
            if path == "/hls.min.js":
                return self._send(200, (WEB / "hls.min.js").read_bytes(), "application/javascript")
            if not authed:
                return self._send(401, {"error": "login"})
            if path == "/api/info":
                s = get_settings()
                return self._send(200, {"live": live.running(), "control": bool(s["control"])})
            if path.startswith("/hls/"):
                f = HLS / Path(path).name
                if f.suffix in (".m3u8", ".ts") and f.exists():
                    ctype = "application/vnd.apple.mpegurl" if f.suffix == ".m3u8" else "video/mp2t"
                    return self._send(200, f.read_bytes(), ctype)
            self._send(404, {"error": "not found"})

        def do_POST(self):
            n = int(self.headers.get("Content-Length") or 0)
            try:
                body = json.loads(self.rfile.read(min(n, 10000)) or b"{}")
            except ValueError:
                return self._send(400, {"error": "bad json"})
            if self.path == "/login":
                ip, now = self._ip(), time.time()
                with live.lock:
                    recent = [t for t in live.fails.get(ip, []) if now - t < 300]
                    live.fails[ip] = recent
                if len(recent) >= 5:
                    return self._send(429, {"error": "錯太多次，五分鐘後再試"})
                if password_ok(get_settings(), body.get("password", "")):
                    vid = secrets.token_hex(8)
                    tok = f"{vid}.{_sign(get_settings(), vid)}"
                    with live.lock:
                        live.sessions[vid] = now
                    return self._send(200, {"ok": True}, headers={
                        "Set-Cookie": f"lv={tok}; Path=/; Max-Age=2592000; HttpOnly; Secure; SameSite=Lax"})
                with live.lock:
                    live.fails[ip].append(now)
                return self._send(403, {"error": "密碼不對"})
            if self._token() is None:
                return self._send(401, {"error": "login"})
            if self.path == "/api/input":
                if not (get_settings()["control"] and live.running()):
                    return self._send(403, {"error": "現在不能操作"})
                do_input(body)
                return self._send(200, {"ok": True})
            self._send(404, {"error": "not found"})

    return ViewerHandler
