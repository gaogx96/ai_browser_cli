<#
.SYNOPSIS
Tab 存活测试 - 验证 view/listen --extension 不误关用户标签页
使用方法: 手动跑，按提示操作
#>

$CLI = "E:\agent-browser-cli\target\debug\agent-browser-cli.exe"

function Get-Tabs {
    $raw = & $CLI list-tabs 2>&1
    $json = ($raw -split '\n' | Where-Object { $_ -notmatch '^\[' } | Select-Object -Last 1) | ConvertFrom-Json
    return $json
}

function Test-Result {
    param($N, $C)
    if ($C) { Write-Host "  [PASS] $N" -ForegroundColor Green }
    else { Write-Host "  [FAIL] $N" -ForegroundColor Red }
}

Write-Host "=== Tab 存活测试 ===" -ForegroundColor Cyan
Write-Host "`n请确保 Chrome 中至少打开了 3 个标签页"
Read-Host "准备好后按 Enter"

# 1. 记录初始 tab
Write-Host "`n[Step 1] 记录初始 tab..." -ForegroundColor Yellow
$before = Get-Tabs
$beforeCount = $before.count
$beforeUrls = @($before.tabs.url)
Write-Host "  当前 tabs: $beforeCount"
Test-Result "至少有 3 个 tab" ($beforeCount -ge 3)

# 2. view --extension
Write-Host "`n[Step 2] 运行 view --extension..." -ForegroundColor Yellow
& $CLI view --url "https://example.com" --extension 2>&1 | Out-Null
Start-Sleep 2

# 3. 检查 tab
Write-Host "`n[Step 3] 检查 tab 存活..." -ForegroundColor Yellow
$after = Get-Tabs
if (-not $after) { Write-Host "  [FAIL] list-tabs 失败"; exit 1 }
$afterCount = $after.count
$lost = $beforeUrls | Where-Object { -not ($after.tabs.url -contains $_) }
Test-Result "tab 数量未大幅减少" ($afterCount -ge ($beforeCount - 2))
if ($lost.Count -gt 0) { Write-Host "  ⚠️ 消失的 tab: $($lost -join ', ')" -ForegroundColor DarkYellow }

# 4. listen --extension (用文件管道)
Write-Host "`n[Step 4] 运行 listen --extension..." -ForegroundColor Yellow
$psi = New-Object System.Diagnostics.ProcessStartInfo
$psi.FileName = $CLI
$psi.Arguments = "listen --extension"
$psi.RedirectStandardInput = $true
$psi.UseShellExecute = $false
$p = New-Object System.Diagnostics.Process
$p.StartInfo = $psi
$p.Start() | Out-Null
Start-Sleep 3
$p.StandardInput.WriteLine('{"action":"navigate","url":"https://example.com"}')
Start-Sleep 3
$p.StandardInput.WriteLine('{"action":"tree"}')
Start-Sleep 2
$p.StandardInput.Close()
$p.WaitForExit(15000) | Out-Null
Start-Sleep 2

# 5. 最终检查
Write-Host "`n[Step 5] 最终检查 tab 存活..." -ForegroundColor Yellow
$final = Get-Tabs
if (-not $final) { Write-Host "  [FAIL] list-tabs 失败" }
$finalCount = $final.count
Test-Result "tab 数量未大幅减少" ($finalCount -ge ($beforeCount - 2))

Write-Host "`n=== 完成 ===" -ForegroundColor Cyan
Write-Host "运行前: $beforeCount tab"
Write-Host "运行后: $finalCount tab"