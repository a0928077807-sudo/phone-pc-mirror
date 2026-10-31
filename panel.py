"""手機／電腦互投的控制頁。綁 config.json 的 panel_host（建議填 Tailscale IP），手機瀏覽器開 http://<panel_host>:<panel_port>

手機投電腦（p2c）：scrcpy，開關＝聲音從哪出、電腦能不能操作手機，改了就重開 scrcpy。
電腦投手機（c2p）：Sunshine 一律從 CABLE Input 收音（audio_sink），串流開始時它會把
預設喇叭切到 CABLE；這裡每秒看一次，依模式決定要不要把預設切回喇叭、要不要把喇叭的
聲音轉送進 CABLE、要不要關掉麥克風聆聽。操作權要改 Sunshine 設定並重啟它。
"""
import base64
import json
import logging
import re
import ssl
import subprocess
import threading
import time
import urllib.request
import warnings
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import comtypes
from pycaw.pycaw import AudioUtilities

import live as livemod
import mirror
from conf import C

warnings.filterwarnings("ignore", module="pycaw")   # 有些端點讀不到屬性，pycaw 會一直警告

HERE = Path(__file__).resolve().parent
HOST, PORT = C["panel_host"], C["panel_port"]
LIVE_PORT = C["live_port"]   # 網頁直播觀看端，只綁本機，靠 cloudflared 對外
SVV = HERE / "bin" / "SoundVolumeView.exe"   # NirSoft，setup.ps1 會下載
SUN_LOG = Path(C["sunshine_log"])
SUN_URL = C["sunshine_url"]
NOWIN = subprocess.CREATE_NO_WINDOW


def find_render(prefix):
    """依名稱找播放端點 ID（例如 CABLE Input）。"""
    for d in AudioUtilities.GetAllDevices():
        if d.id.startswith("{0.0.0.") and (d.FriendlyName or "").startswith(prefix) and "Active" in str(d.state):
            return d.id
    raise SystemExit(f"找不到播放裝置「{prefix}」，VB-CABLE 裝了嗎？")


def default_mic():
    m = AudioUtilities.GetMicrophone()
    return m.GetId() if hasattr(m, "GetId") else m.id


CABLE = find_render(C["cable_render_name"])
MIC = C["mic_id"] or default_mic()

logging.basicConfig(filename=HERE / "panel.log", level=logging.INFO, encoding="utf-8",
                    format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger("panel")
lock = threading.RLock()
settings = mirror.load_settings()
if not settings.get("pc_speaker"):   # 平常用的喇叭；之後沒在串流時換了喇叭會自動記住
    _cur = AudioUtilities.GetSpeakers().id
    settings["pc_speaker"] = _cur if _cur != CABLE else ""
_lv = settings.setdefault("live", {})
for _k, _v in {"audio": "remote", "control": False, "quality": "smooth", "fps": 30}.items():
    _lv.setdefault(_k, _v)
if _lv["quality"] not in livemod.QUALITY:   # 舊版的 "sharp" 已改名
    _lv["quality"] = "smooth"
if not _lv.get("password"):
    _lv["password"] = livemod.new_password()
_lv.setdefault("secret", livemod.secrets.token_hex(16))
mirror.save_settings(settings)
live = livemod.Live()


def svv(*args):
    subprocess.run([str(SVV), *args], creationflags=NOWIN, timeout=15)


# ---------- Sunshine ----------

_AUTH = "Basic " + base64.b64encode(f"{C['sunshine_user']}:{C['sunshine_password']}".encode()).decode()
_CTX = ssl._create_unverified_context()


def sun_api(method, path, body=None):
    req = urllib.request.Request(SUN_URL + path, method=method,
                                 data=json.dumps(body).encode() if body is not None else None,
                                 headers={"Authorization": _AUTH, "Content-Type": "application/json",
                                          "Origin": SUN_URL})
    with urllib.request.urlopen(req, context=_CTX, timeout=15) as r:
        return json.loads(r.read() or b"{}")


def sun_configure(updates):
    """改 Sunshine 設定，有變才寫並重啟（重啟會斷掉正在串流的連線）。"""
    cfg = {k: v for k, v in sun_api("GET", "/api/config").items()
           if k not in ("platform", "status", "version")}
    if all(str(cfg.get(k)) == v for k, v in updates.items()):
        return False
    cfg.update(updates)
    sun_api("POST", "/api/config", cfg)
    try:
        sun_api("POST", "/api/restart", {})
    except Exception:
        pass   # 重啟時連線會直接斷，正常
    log.info("sunshine config %s, restarted", updates)
    return True


def sun_input_updates(allow):
    v = "enabled" if allow else "disabled"
    return {"mouse": v, "keyboard": v, "controller": v, "native_pen_touch": v}


def sun_streaming():
    """看 Sunshine 日誌最後一筆是連上還是斷線。"""
    try:
        with open(SUN_LOG, "rb") as f:
            f.seek(0, 2)
            f.seek(max(0, f.tell() - 200_000))
            tail = f.read().decode("utf-8", "replace")
    except OSError:
        return False
    a, b = tail.rfind("CLIENT CONNECTED"), tail.rfind("CLIENT DISCONNECTED")
    return a > b


# ---------- 電腦投手機的聲音 ----------

class Forwarder:
    """把喇叭的聲音（loopback）轉送進 CABLE Input，讓「都出」時手機也收得到電腦聲音。"""

    def __init__(self):
        self.thread = None
        self.stop_flag = threading.Event()

    def running(self):
        return self.thread is not None and self.thread.is_alive()

    def start(self, speaker_id):
        if self.running():
            return
        self.stop_flag.clear()
        self.thread = threading.Thread(target=self._run, args=(speaker_id,), daemon=True)
        self.thread.start()
        log.info("forwarder start")

    def stop(self):
        if self.running():
            self.stop_flag.set()
            self.thread.join(timeout=3)
            log.info("forwarder stop")

    def _run(self, speaker_id):
        import soundcard as sc
        try:
            src = sc.get_microphone(speaker_id, include_loopback=True)
            dst = sc.get_speaker(C["cable_render_name"])
            with src.recorder(samplerate=48000, channels=2, blocksize=480) as rec, \
                    dst.player(samplerate=48000, channels=2, blocksize=480) as play:
                while not self.stop_flag.is_set():
                    data = rec.record(numframes=480)
                    if self.stop_flag.is_set():   # 沒聲音時 record 可能卡很久，醒來先看是不是該停了
                        break
                    play.play(data)
        except Exception:
            log.exception("forwarder failed")


fwd = Forwarder()
_listen_state = None


def set_listen(on):
    global _listen_state
    if _listen_state != on:
        svv("/SetListenToThisDevice", MIC, "1" if on else "0")
        _listen_state = on
        log.info("mic listen %s", on)


def default_speaker():
    return AudioUtilities.GetSpeakers().id


def audio_tick():
    """Moonlight 串流或網頁直播進行中，就照該模式擺聲音；直播優先（兩個同時開很少見）。"""
    sun, broadcasting = sun_streaming(), live.running()
    cur = default_speaker()
    with lock:
        if broadcasting:
            mode = {"remote": "phone", "pc": "pc", "both": "both"}[settings["live"]["audio"]]
        else:
            mode = settings["c2p"]["audio"]
        pc = settings["pc_speaker"] or cur
        if not (sun or broadcasting):
            fwd.stop()
            set_listen(True)
            if cur != CABLE and cur != pc:      # 使用者換了平常用的喇叭就記起來
                settings["pc_speaker"] = cur
                mirror.save_settings(settings)
            elif cur == CABLE:                  # 串流斷了卻卡在 CABLE（Sunshine 當掉之類）
                svv("/SetDefault", pc, "all")
            return
    if mode == "phone":          # 只給對方：電腦聲音全進 CABLE，喇叭不出聲
        fwd.stop()
        set_listen(True)
        if cur != CABLE:          # Moonlight 時 Sunshine 已經切了；直播要自己切
            svv("/SetDefault", CABLE, "all")
    elif mode == "both":
        set_listen(True)
        if cur != pc:
            svv("/SetDefault", pc, "all")
        fwd.start(pc)
    else:  # pc
        fwd.stop()
        set_listen(False)
        if cur != pc:
            svv("/SetDefault", pc, "all")


def watcher():
    comtypes.CoInitialize()
    last_restart = last_tunnel = 0
    while True:
        try:
            if not live.tunnel_up() and time.time() - last_tunnel > 10:
                live.ensure_tunnel()
                last_tunnel = time.time()
            if live.wanted and not live.running() and time.time() - last_restart > 5:
                log.warning("live ffmpeg exited, restarting: %s",
                            livemod.FF_LOG.read_text(encoding="utf-8", errors="replace")[-300:])
                live.keepalive()
                last_restart = time.time()
            audio_tick()
        except Exception:
            log.exception("audio tick")
        time.sleep(1)


# ---------- 手機投電腦 ----------

p2c = {"proc": None, "busy": False, "msg": "", "via": ""}


def scrcpy_running():
    out = subprocess.run(["tasklist", "/FI", "IMAGENAME eq scrcpy.exe", "/NH"],
                         capture_output=True, text=True, creationflags=NOWIN).stdout
    return "scrcpy.exe" in out


def p2c_stop():
    subprocess.run(["taskkill", "/IM", "scrcpy.exe", "/F"], capture_output=True, creationflags=NOWIN)
    p2c["proc"] = None


def p2c_start():
    if p2c["busy"]:
        return
    p2c.update(busy=True, msg="找手機中…")

    def run():
        try:
            rect = mirror.window_rect()   # 先記位置，重開放回去
            p2c_stop()
            mirror.adb("start-server")
            kind, serial = mirror.pick("auto")
            if not serial:
                p2c["msg"] = "找不到手機：請開「無線偵錯」或插 USB"
                return
            with lock:
                opts = dict(settings["p2c"])
            proc = mirror.launch(mirror.build_args(kind, serial, opts, rect))
            time.sleep(4)
            if proc.poll() is not None:
                p2c["msg"] = "投影沒開起來：" + mirror.LOG.read_text(encoding="utf-8")[-300:]
                return
            p2c.update(proc=proc, via=mirror.NAMES[kind], msg="")
            log.info("p2c started via %s %s", kind, opts)
        except Exception as e:
            log.exception("p2c start")
            p2c["msg"] = f"出錯：{e}"
        finally:
            p2c["busy"] = False

    threading.Thread(target=run, daemon=True).start()


# ---------- HTTP ----------

def state():
    with lock:
        s = json.loads(json.dumps(settings))
    return {
        "p2c": {**s["p2c"], "running": scrcpy_running(), "busy": p2c["busy"],
                "msg": p2c["msg"], "via": p2c["via"]},
        "c2p": {**s["c2p"], "streaming": sun_streaming(), "busy": c2p_busy[0]},
        "live": {k: s["live"][k] for k in ("audio", "control", "quality", "fps", "password")}
                | {"running": live.running(), "viewers": live.viewers(), "url": C["live_public_url"]},
    }


c2p_busy = [False]


def apply_c2p_control(allow):
    c2p_busy[0] = True
    try:
        sun_configure(sun_input_updates(allow))
    except Exception:
        log.exception("sunshine configure")
    finally:
        c2p_busy[0] = False


class Handler(BaseHTTPRequestHandler):
    def log_message(self, *a):
        pass

    def _send(self, code, body, ctype="application/json; charset=utf-8"):
        data = body if isinstance(body, bytes) else json.dumps(body, ensure_ascii=False).encode()
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(data)

    def do_GET(self):
        if self.path in ("/", "/index.html"):
            self._send(200, (HERE / "index.html").read_bytes(), "text/html; charset=utf-8")
        elif self.path == "/api/state":
            self._send(200, state())
        else:
            self._send(404, {"error": "not found"})

    def do_POST(self):
        # 自訂標頭擋掉其他網站從瀏覽器偷送表單（跨站送不出這個標頭）
        if self.headers.get("X-Panel") != "1":
            return self._send(403, {"error": "forbidden"})
        n = int(self.headers.get("Content-Length") or 0)
        try:
            body = json.loads(self.rfile.read(n) or b"{}")
        except ValueError:
            return self._send(400, {"error": "bad json"})

        if self.path == "/api/p2c/start":
            p2c_start()
        elif self.path == "/api/p2c/stop":
            p2c_stop()
        elif self.path == "/api/p2c":
            with lock:
                if body.get("audio") in ("pc", "phone", "both"):
                    settings["p2c"]["audio"] = body["audio"]
                if isinstance(body.get("control"), bool):
                    settings["p2c"]["control"] = body["control"]
                if body.get("window") in ("normal", "borderless", "fullscreen"):
                    settings["p2c"]["window"] = body["window"]
                if isinstance(body.get("top"), bool):
                    settings["p2c"]["top"] = body["top"]
                mirror.save_settings(settings)
            if scrcpy_running():
                p2c_start()      # 開著就用新設定重開
        elif self.path == "/api/c2p":
            with lock:
                if body.get("audio") in ("pc", "phone", "both"):
                    settings["c2p"]["audio"] = body["audio"]
                ctl = body.get("control")
                if isinstance(ctl, bool):
                    settings["c2p"]["control"] = ctl
                mirror.save_settings(settings)
            if isinstance(ctl, bool):
                threading.Thread(target=apply_c2p_control, args=(ctl,), daemon=True).start()
        elif self.path == "/api/live/start":
            with lock:
                q = settings["live"]["quality"]
                fps = settings["live"]["fps"]
            live.start(q, fps)
            log.info("live start %s %sfps", q, fps)
        elif self.path == "/api/live/stop":
            live.stop()
            log.info("live stop")
        elif self.path == "/api/live/newpass":
            with lock:
                settings["live"]["password"] = livemod.new_password()
                mirror.save_settings(settings)
        elif self.path == "/api/live":
            with lock:
                lv = settings["live"]
                if body.get("audio") in ("remote", "pc", "both"):
                    lv["audio"] = body["audio"]
                if isinstance(body.get("control"), bool):
                    lv["control"] = body["control"]
                qchanged = body.get("quality") in livemod.QUALITY and body["quality"] != lv["quality"]
                if qchanged:
                    lv["quality"] = body["quality"]
                if str(body.get("fps")) in ("30", "60") and int(body["fps"]) != lv["fps"]:
                    lv["fps"] = int(body["fps"])
                    qchanged = True
                mirror.save_settings(settings)
            if qchanged and live.wanted:
                live.start(lv["quality"], lv["fps"])
        else:
            return self._send(404, {"error": "not found"})
        self._send(200, state())


def ensure_sunshine():
    """Sunshine 一律從 CABLE 收音、不用它自己的虛擬喇叭（不然串流中切不回喇叭）。"""
    for _ in range(30):
        try:
            with lock:
                allow = settings["c2p"]["control"]
            if not sun_streaming():
                sun_configure({"audio_sink": CABLE, "virtual_sink": "none",
                               **sun_input_updates(allow)})
            return
        except Exception:
            time.sleep(5)
    log.error("sunshine not reachable")


def main():
    log.info("panel start")
    livemod.kill_strays()
    threading.Thread(target=ensure_sunshine, daemon=True).start()
    threading.Thread(target=watcher, daemon=True).start()

    def live_settings():
        with lock:
            return dict(settings["live"])

    vsrv = ThreadingHTTPServer(("127.0.0.1", LIVE_PORT), livemod.make_handler(live, live_settings))
    threading.Thread(target=vsrv.serve_forever, daemon=True).start()
    while True:   # 開機時 Tailscale 可能還沒好，綁不上就等
        try:
            srv = ThreadingHTTPServer((HOST, PORT), Handler)
            break
        except OSError:
            time.sleep(5)
    log.info("listening %s:%s", HOST, PORT)
    srv.serve_forever()


if __name__ == "__main__":
    main()
