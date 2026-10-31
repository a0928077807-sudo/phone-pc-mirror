# phone-pc-mirror 一次性安裝：下載 scrcpy／SoundVolumeView／hls.js，安裝 VB-CABLE 與 Sunshine，
# 把麥克風「聆聽此裝置」導進 CABLE Input（喇叭聽不到），產生 Sunshine 網頁帳密寫進 config.json。
# 需要系統管理員：直接執行，UAC 按「是」。紀錄寫到 install.log。
$here = Split-Path -Parent $MyInvocation.MyCommand.Path
$id = [Security.Principal.WindowsIdentity]::GetCurrent()
if (-not ([Security.Principal.WindowsPrincipal]$id).IsInRole([Security.Principal.WindowsBuiltInRole]::Administrator)) {
    Start-Process powershell.exe -Verb RunAs -ArgumentList "-NoProfile -ExecutionPolicy Bypass -File `"$($MyInvocation.MyCommand.Path)`""
    exit
}
Start-Transcript (Join-Path $here 'install.log') -Force
$ProgressPreference = 'SilentlyContinue'
$dl  = Join-Path $here 'downloads'
$bin = Join-Path $here 'bin'
$sun = 'C:\Program Files\Sunshine'
New-Item -ItemType Directory -Force $dl, $bin, (Join-Path $here 'live') | Out-Null

function Get-LatestAsset($repo, $pattern) {
    $r = Invoke-RestMethod "https://api.github.com/repos/$repo/releases/latest"
    ($r.assets | Where-Object { $_.name -like $pattern } | Select-Object -First 1).browser_download_url
}

# 1. 下載
"下載 scrcpy…"
Invoke-WebRequest (Get-LatestAsset 'Genymobile/scrcpy' 'scrcpy-win64-*.zip') -OutFile "$dl\scrcpy.zip"
Expand-Archive "$dl\scrcpy.zip" "$bin" -Force
$scrcpy = (Get-ChildItem $bin -Recurse -Filter scrcpy.exe | Select-Object -First 1).FullName

"下載 Sunshine…"
Invoke-WebRequest (Get-LatestAsset 'LizardByte/Sunshine' 'Sunshine-Windows-AMD64-installer.msi') -OutFile "$dl\Sunshine.msi"

"下載 VB-CABLE…"
Invoke-WebRequest 'https://download.vb-audio.com/Download_CABLE/VBCABLE_Driver_Pack45.zip' -OutFile "$dl\VBCABLE.zip"
Expand-Archive "$dl\VBCABLE.zip" "$dl\VBCABLE" -Force

"下載 SoundVolumeView…"
Invoke-WebRequest 'https://www.nirsoft.net/utils/soundvolumeview-x64.zip' -OutFile "$dl\svv.zip"
Expand-Archive "$dl\svv.zip" "$dl\svv" -Force
Copy-Item "$dl\svv\SoundVolumeView.exe" $bin -Force
$svv = Join-Path $bin 'SoundVolumeView.exe'

"下載 hls.js…"
Invoke-WebRequest (Get-LatestAsset 'video-dev/hls.js' 'release.zip') -OutFile "$dl\hlsjs.zip"
Expand-Archive "$dl\hlsjs.zip" "$dl\hlsjs" -Force
Copy-Item (Get-ChildItem "$dl\hlsjs" -Recurse -Filter hls.min.js | Select-Object -First 1).FullName (Join-Path $here 'live\hls.min.js') -Force

function Get-Endpoints {
    $csv = Join-Path $env:TEMP 'svv_endpoints.csv'
    Start-Process $svv -ArgumentList "/scomma `"$csv`" /Columns `"Name,Device Name,Direction,Default,Device State,Item ID`"" -Wait
    Import-Csv $csv | Where-Object { $_.'Item ID' -like '{0.0.*' -and $_.'Item ID' -notlike '*|*' }
}

# 2. 記下原本的預設喇叭與麥克風（裝 VB-CABLE 可能會被搶走）
$before = Get-Endpoints
$defOut = ($before | Where-Object { $_.Direction -eq 'Render' -and $_.Default } | Select-Object -First 1).'Item ID'
$defIn  = ($before | Where-Object { $_.Direction -eq 'Capture' -and $_.Default } | Select-Object -First 1).'Item ID'
"原本預設喇叭：$defOut"
"原本預設麥克風：$defIn"

# 3. 安裝
"安裝 VB-CABLE…"
Start-Process "$dl\VBCABLE\VBCABLE_Setup_x64.exe" -ArgumentList '-i -h' -Wait
"安裝 Sunshine…"
$msi = Start-Process msiexec.exe -ArgumentList "/i `"$dl\Sunshine.msi`" /qn /norestart" -Wait -PassThru
"msiexec 結束碼：$($msi.ExitCode)"

# 4. 找 CABLE Input（驅動裝完要等一下才出現）
$cable = $null
for ($i = 0; $i -lt 30 -and -not $cable; $i++) {
    $cable = (Get-Endpoints | Where-Object { $_.Direction -eq 'Render' -and $_.Name -like 'CABLE Input*' } | Select-Object -First 1).'Item ID'
    if (-not $cable) { Start-Sleep 2 }
}
"CABLE Input：$cable"

# 5. 預設裝置改回原本的
if ($defOut) { Start-Process $svv -ArgumentList "/SetDefault `"$defOut`" all" -Wait }
if ($defIn)  { Start-Process $svv -ArgumentList "/SetDefault `"$defIn`" all" -Wait }

# 6. 麥克風「聆聽此裝置」只送進 CABLE Input
if ($cable -and $defIn) {
    Start-Process $svv -ArgumentList "/SetPlaybackThroughDevice `"$defIn`" `"$cable`"" -Wait
    Start-Process $svv -ArgumentList "/SetListenToThisDevice `"$defIn`" 1" -Wait
} else {
    "找不到 CABLE Input 或預設麥克風，麥克風沒設定"
}

# 7. Sunshine 網頁帳密，寫進 config.json
$pw = -join ((48..57) + (97..122) | Get-Random -Count 16 | ForEach-Object { [char]$_ })
& (Join-Path $sun 'sunshine.exe') --creds mirror $pw
$cfgPath = Join-Path $here 'config.json'
if (-not (Test-Path $cfgPath)) { Copy-Item (Join-Path $here 'config.example.json') $cfgPath }
$cfg = Get-Content $cfgPath -Raw -Encoding UTF8 | ConvertFrom-Json
$cfg.scrcpy = $scrcpy
$cfg.sunshine_user = 'mirror'
$cfg.sunshine_password = $pw
[IO.File]::WriteAllText($cfgPath, ($cfg | ConvertTo-Json), (New-Object Text.UTF8Encoding $false))

# 8. 防火牆與服務
if (-not (Get-NetFirewallRule -DisplayName 'Sunshine (phone-pc-mirror)' -ErrorAction SilentlyContinue)) {
    New-NetFirewallRule -DisplayName 'Sunshine (phone-pc-mirror)' -Direction Inbound -Program (Join-Path $sun 'sunshine.exe') -Action Allow -Profile Any | Out-Null
}
Get-Service *sunshine* | Restart-Service -Force

""
"完成。接下來："
"  1. 打開 config.json，填 panel_host（電腦的 Tailscale IP）、phone_tailscale_ip，要遠端直播再填 ffmpeg、cloudflared、live_public_url"
"  2. pip install -r requirements.txt"
"  3. 執行 start_panel.vbs（或 python panel.py），手機瀏覽器開 http://<panel_host>:47380"
Stop-Transcript
Read-Host "按 Enter 關閉"
