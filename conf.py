"""讀 config.json（照 config.example.json 複製一份改）。所有跟這台電腦、這支手機有關的值都在這裡。"""
import json
from pathlib import Path

HERE = Path(__file__).resolve().parent
_FILE = HERE / "config.json"

DEFAULTS = {
    # 控制頁綁的位址：建議填電腦的 Tailscale IP，只讓自己的裝置連得到；只在本機用就填 127.0.0.1
    "panel_host": "127.0.0.1",
    "panel_port": 47380,
    # 手機的 Tailscale IP（不用 Tailscale 連手機就留空）
    "phone_tailscale_ip": "",
    # scrcpy 視窗標題，也拿來找視窗記位置
    "window_title": "Phone",
    "scrcpy": "",            # scrcpy.exe 完整路徑
    "ffmpeg": "",            # ffmpeg.exe 完整路徑（遠端直播用）
    "cloudflared": "",       # cloudflared.exe 完整路徑（遠端直播用）
    "live_port": 47381,      # 直播觀看端，只綁 127.0.0.1，靠 cloudflared 對外
    "live_public_url": "",   # 例如 https://live.example.com
    "live_tunnel_config": "cloudflared-live.yml",
    "sunshine_url": "https://localhost:47990",
    "sunshine_user": "",
    "sunshine_password": "",
    "sunshine_log": r"C:\Program Files\Sunshine\config\sunshine.log",
    # VB-CABLE 的播放端與錄音端名稱（英文版與中文版 Windows 都是這兩個）
    "cable_render_name": "CABLE Input",
    "cable_capture_dshow": "CABLE Output (VB-Audio Virtual Cable)",
    # 要送給對方的麥克風端點 ID；留空＝啟動時的預設錄音裝置
    "mic_id": "",
    # 直播的聲音延遲補償（毫秒），用 tools/sync_test.py 量自己的電腦
    "live_audio_delay_ms": 100,
}


def load():
    cfg = dict(DEFAULTS)
    if _FILE.exists():
        cfg.update(json.loads(_FILE.read_text(encoding="utf-8")))
    return cfg


C = load()
