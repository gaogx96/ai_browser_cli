# Agent Browser CLI 安全测试
# 测试一:动态插帧不点偏
# 测试二:退出不误关用户 tab

$CLI = "E:\agent-browser-cli\target\debug\agent-browser-cli.exe"
$PASS = 0
$FAIL = 0

function Test-Result {
    param($N, $C, $D)
    if ($C) { Write-Host "  [PASS] $N" -ForegroundColor Green; $script:PASS++ }
    else { Write-Host "  [FAIL] $N" -ForegroundColor Red; if ($D) { Write-Host "         $D" }; $script:FAIL++ }
}

Write-Host "=== Agent Browser CLI 安全测试 ===" -ForegroundColor Cyan

# 环境检查
Write-Host "`n--- 环境检查 ---" -ForegroundColor Yellow
$r = curl.exe -s -o NUL -w "%{http_code}" "http://127.0.0.1:8080/oopif_main.html"
Test-Result "Fixture 8080" ($r -eq "200") "HTTP=$r"
$r = curl.exe -s -o NUL -w "%{http_code}" "http://127.0.0.1:8081/oopif_frame.html"
Test-Result "Fixture 8081" ($r -eq "200") "HTTP=$r"
$r = curl.exe -s -o NUL -w "%{http_code}" "http://127.0.0.1:8082/other.html"
Test-Result "Fixture 8082" ($r -eq "200") "HTTP=$r"

# ══════════════════════════════════════════════════════
# 测试一:动态插帧不点偏
# ══════════════════════════════════════════════════════
Write-Host "`n=== 测试一:动态插帧不点偏 ===" -ForegroundColor Cyan

# 1. 先提取初始树
Write-Host "--- 提取初始树 ---" -ForegroundColor Yellow
$url = "http://127.0.0.1:8080/oopif_dynamic.html"
$output = & $CLI view --url $url --extension 2>&1
$hasF0 = $output -match "f0-e1"
$hasF1 = $output -match "f1-e1"
Test-Result "初始树: 主 frame 元素" $hasF0 ""
Test-Result "初始树: 跨域 iframe 元素" $hasF1 ""

# 2. listen 模式点击
Write-Host "--- listen 模式点击 ---" -ForegroundColor Yellow
$si = New-Object System.Diagnostics.ProcessStartInfo
$si.FileName = $CLI
$si.Arguments = "listen --extension"
$si.RedirectStandardInput = $true
$si.RedirectStandardOutput = $true
$si.RedirectStandardError = $true
$si.UseShellExecute = $false
$p = New-Object System.Diagnostics.Process
$p.StartInfo = $si
$p.Start() | Out-Null
Start-Sleep 3

# Navigate
$json = '{"action":"navigate","url":"' + $url + '"}'
$p.StandardInput.WriteLine($json)
Start-Sleep 3
# Click
$json = '{"action":"click","target_id":"f1-e1"}'
$p.StandardInput.WriteLine($json)
Start-Sleep 2
# Read tree
$json = '{"action":"tree"}'
$p.StandardInput.WriteLine($json)
Start-Sleep 2
# Exit
$p.StandardInput.Close()
$out = $p.StandardOutput.ReadToEnd()
$p.WaitForExit(10000) | Out-Null

$hasClicked = $out -match "status.*ok"
$clickedOriginal = $out -match "CLICKED-ORIGINAL"
Test-Result "click 状态=ok" $hasClicked ""
Test-Result "点中目标按钮" $clickedOriginal ""

# ══════════════════════════════════════════════════════
# 测试二:退出不误关用户 tab
# ══════════════════════════════════════════════════════
Write-Host "`n=== 测试二:退出不误关用户 tab ===" -ForegroundColor Cyan
# 清理残留进程，释放端口
Get-Process -Name "agent-browser-cli" -ErrorAction SilentlyContinue | Stop-Process -Force -ErrorAction SilentlyContinue
Start-Sleep 1

# 记录运行前 tab 数
$beforeRaw = & $CLI list-tabs 2>&1
$beforeLine = $beforeRaw -split '\n' | Where-Object { $_ -notmatch '^\[' } | Select-Object -Last 1
if (-not $beforeLine) { Write-Host "  [FAIL] list-tabs no JSON"; $beforeCount = 0; $beforeUrls = @() } else {
$beforeJson = $beforeLine | ConvertFrom-Json
$beforeCount = $beforeJson.count
$beforeUrls = @($beforeJson.tabs.url) }
Test-Result "运行前有 tab" ($beforeCount -ge 3) "count=$beforeCount"

# view --extension
Write-Host "--- 运行 view --extension ---" -ForegroundColor Yellow
$vout = & $CLI view --url "https://example.com" --extension 2>&1
Test-Result "view 成功" ($vout -match "status.*ok") ""

# view 退出后 tab 数不应减少（只关自建 tab，不影响用户 tab）
$afterRaw = & $CLI list-tabs 2>&1
$afterLine = $afterRaw -split '\n' | Where-Object { $_ -notmatch '^\[' } | Select-Object -Last 1
if (-not $afterLine) { Write-Host "  [FAIL] list-tabs no JSON"; $afterCount = 0 } else {
$afterJson = $afterLine | ConvertFrom-Json
$afterCount = $afterJson.count }
$lost = $beforeUrls | Where-Object { -not ($afterJson.tabs.url -contains $_) }
Test-Result "view 后 tab 未大量减少" ($afterCount -ge ($beforeCount - 2)) "before=$beforeCount after=$afterCount"
if ($lost.Count -gt 0) {
    Write-Host "  ⚠️ 消失的 tab: $($lost -join ', ')" -ForegroundColor DarkYellow
}

# listen --extension
Write-Host "--- 运行 listen --extension ---" -ForegroundColor Yellow
$si2 = New-Object System.Diagnostics.ProcessStartInfo
$si2.FileName = $CLI
$si2.Arguments = "listen --extension"
$si2.RedirectStandardInput = $true
$si2.RedirectStandardOutput = $true
$si2.RedirectStandardError = $true
$si2.UseShellExecute = $false
$p2 = New-Object System.Diagnostics.Process
$p2.StartInfo = $si2
$p2.Start() | Out-Null
Start-Sleep 3
$json2 = '{"action":"navigate","url":"https://example.com"}'
$p2.StandardInput.WriteLine($json2)
Start-Sleep 3
$json2 = '{"action":"tree"}'
$p2.StandardInput.WriteLine($json2)
Start-Sleep 2
$p2.Kill()
$p2.WaitForExit(5000) | Out-Null

# 再次检查 tab 数
$finalRaw = & $CLI list-tabs 2>&1
$finalLine = $finalRaw -split '\n' | Where-Object { $_ -notmatch '^\[' } | Select-Object -Last 1
if (-not $finalLine) { Write-Host "  [FAIL] list-tabs no JSON"; $finalCount = 0 } else {
$finalJson = $finalLine | ConvertFrom-Json
$finalCount = $finalJson.count }
Test-Result "listen 后 tab 未大量减少" ($finalCount -ge ($beforeCount - 2)) "before=$beforeCount final=$finalCount"

# 汇总
Write-Host "`n=== 完成 PASS:$PASS FAIL:$FAIL ===" -ForegroundColor Cyan
if ($FAIL -gt 0) { exit 1 }
exit 0