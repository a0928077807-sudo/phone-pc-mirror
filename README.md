# phone-pc-mirror

Android 手機與 Windows 電腦互相投影，外加一個「給外地的人用瀏覽器看電腦」的網頁直播。
全部開關集中在一個手機瀏覽器就能開的控制頁上。

底層都是現成的開源工具（scrcpy、Sunshine／Moonlight、ffmpeg、cloudflared），
這個專案負責把它們接起來，並處理最麻煩的那一塊：**聲音該從哪裡出來**。

## 能做什麼

| 方向 | 用什麼 | 延遲 | 開關 |
|---|---|---|---|
| 手機 → 電腦 | scrcpy（USB／同 Wi-Fi／Tailscale 自動選） | 約 35～70 ms | 聲音：電腦／手機／都出；電腦能不能操作手機；視窗：一般／無邊框／全螢幕；置頂 |
| 電腦 → 自己的手機 | Sunshine ＋ Moonlight | 低 | 聲音：手機／電腦／都出；手機能不能操作電腦 |
| 電腦 → 遠端的人 | ffmpeg ＋ HLS ＋ Cloudflare Tunnel，對方用瀏覽器看 | 約 8～15 秒 | 聲音：對方／電腦／都放；對方能不能操作；畫質：流暢 720p／高畫質穩定（1080p＋720p 自動切換）；30／60 fps；密碼 |

電腦往外送的聲音都**包含電腦麥克風**，而且麥克風**不會從電腦喇叭放出來**。

## 聲音怎麼繞

裝一張虛擬音效卡 VB-CABLE。麥克風的「聆聽此裝置」固定導進 `CABLE Input`（喇叭聽不到），
所有往外送的串流都從 CABLE 收音。控制頁每秒看一次有沒有人在看，依模式調整：

- **只給對方**：預設喇叭切到 CABLE，電腦聲音＋麥克風全部送出去，電腦不出聲
- **都出**：預設喇叭留在原本的喇叭，另外把喇叭的聲音（loopback）即時轉送一份進 CABLE
- **只在電腦**：預設喇叭留在原本的喇叭，關掉麥克風聆聽，CABLE 裡沒聲音

串流結束會自動切回原本的喇叭。

## 需求

- Windows 10／11，NVIDIA 顯卡（直播用 NVENC 編碼；其他顯卡要自己改 `live.py` 的編碼器）
- Android 11 以上（scrcpy 傳聲音需要），手機開「開發人員選項 → 無線偵錯」或插 USB
- Python 3.11 以上
- 建議裝 [Tailscale](https://tailscale.com/)：手機在外面也能連，控制頁只開給自己的裝置
- 遠端直播另外需要：ffmpeg（`winget install Gyan.FFmpeg`）、cloudflared、一個放在 Cloudflare 的網域

## 安裝

```powershell
git clone https://github.com/a0928077807-sudo/phone-pc-mirror
cd phone-pc-mirror
powershell -ExecutionPolicy Bypass -File setup.ps1   # UAC 按「是」
pip install -r requirements.txt
```

`setup.ps1` 會下載 scrcpy、SoundVolumeView、hls.js，安裝 VB-CABLE 與 Sunshine，
把原本的預設喇叭與麥克風改回來（裝 VB-CABLE 時 Windows 常常會自己換掉），
設好麥克風聆聽，產生 Sunshine 網頁帳密並寫進 `config.json`。

然後打開 `config.json` 填：

- `panel_host`：電腦的 Tailscale IP（只在電腦本機用就填 `127.0.0.1`）
- `phone_tailscale_ip`：手機的 Tailscale IP
- 要遠端直播再填 `ffmpeg`、`cloudflared`、`live_public_url`

啟動控制頁：執行 `start_panel.vbs`（沒有視窗）。要開機自動啟動，就把它的捷徑放進 `shell:startup`。
手機瀏覽器開 `http://<panel_host>:47380`。

手機投電腦也可以不開控制頁，直接 `python mirror.py`（會照控制頁存的設定開）。

## 第一次用 Moonlight

手機裝 [Moonlight](https://github.com/moonlight-stream/moonlight-android)，按「＋」輸入電腦的 Tailscale IP。
點電腦後會跳出 4 位數 PIN，**這時不要切到別的 App**：新版 Sunshine 送出 PIN 後還要跟 Moonlight 來回握手，
Moonlight 一進背景配對就會失敗。可以讓電腦自動讀 PIN 送出（手機要能用 adb 連上）：

```powershell
python tools/autopair.py
```

## 遠端直播

直播走一條**專用**的 Cloudflare Tunnel，控制頁開著就一直連著。對方可以先打開網址輸入密碼等著，
還沒開播會顯示「直播还没开始」，一開播畫面就自動出現。（只在直播時才連通道的話，對方提早打開只會看到
Cloudflare 的 1033 錯誤頁，不知道發生什麼事。）

```powershell
cloudflared tunnel login
cloudflared tunnel create live-stream
cloudflared tunnel route dns live-stream live.example.com
```

把 `cloudflared-live.example.yml` 複製成 `cloudflared-live.yml`，填入通道 ID 與憑證路徑。
控制頁上按「開始直播」→「複製給對方」，把網址和密碼傳出去就好。觀看頁是簡體中文，iPhone 與 Android 都能看。

影音同步每台電腦可能不一樣，可以量一次：

```powershell
python tools/sync_test.py              # 看「beep - flash」，負的代表聲音比畫面早
python tools/sync_test.py "adelay=100:all=1"   # 試補償值
```

量到的毫秒數填進 `config.json` 的 `live_audio_delay_ms`。

## 安全

- 控制頁沒有密碼，靠「只綁 Tailscale IP」限制只有自己的裝置連得到；POST 要帶自訂標頭，擋掉其他網站的跨站請求。**不要綁 `0.0.0.0`。**
- 直播觀看端只綁 `127.0.0.1`，經 Cloudflare 對外；密碼 8 碼，錯 5 次鎖 5 分鐘，換密碼所有人登出。
- 「對方能不能操作」打開時，拿到密碼的人可以點你的電腦、打字。預設是只看，用完記得關。
- 玩有反作弊的遊戲時，建議把遠端操作關掉（程式模擬的滑鼠鍵盤可能被判定）。

## 踩過的坑

- **微信等 VoIP 通話中，scrcpy 收不到手機聲音**：通話會把手機切進 `MODE_IN_COMMUNICATION`，scrcpy 預設的 `output` 來源整個收不到。`mirror.py` 開投影前會先看 `dumpsys audio`，通話中自動改用 `playback`。
- **Sunshine 的 `virtual_sink` 不能設成 CABLE**：設了它會掛回呼，預設喇叭一被改就切回去，「都出」模式就做不出來。改成 `audio_sink = CABLE`、`virtual_sink = none`。
- **中文 Windows 的裝置名稱**：裝置管理員顯示「喇叭 (VB-Audio Virtual Cable)」，但端點名稱仍是 `CABLE Input`，用端點名稱找。
- **主要的 cloudflared 如果是 Windows 服務**，非管理員改不了設定也重開不了；另起一個 connector 連同一條通道會分走一半流量。所以直播另開一條通道。
- **ddagrab（畫面）與 dshow（聲音）時鐘不同**，聲音大約早 100 ms，用 `adelay` 補，再用 `aresample=async` 修長時間漂移。

## 授權

MIT。`setup.ps1` 下載的第三方軟體各自有自己的授權：
[scrcpy](https://github.com/Genymobile/scrcpy)（Apache-2.0）、
[Sunshine](https://github.com/LizardByte/Sunshine)（GPL-3.0）、
[Moonlight](https://github.com/moonlight-stream/moonlight-android)（GPL-3.0）、
[hls.js](https://github.com/video-dev/hls.js)（Apache-2.0）、
[VB-CABLE](https://vb-audio.com/Cable/)（donationware）、
[SoundVolumeView](https://www.nirsoft.net/utils/sound_volume_view.html)（NirSoft freeware）。
本專案不附這些程式的檔案。
