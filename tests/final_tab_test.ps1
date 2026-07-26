[Console]::OutputEncoding = [System.Text.Encoding]::UTF8
$CLI = "E:\agent-browser-cli\target\debug\agent-browser-cli.exe"

function Get-TabCount {
    $raw = cmd.exe /c "`"$CLI`" list-tabs 2>&1"
    foreach ($l in ($raw -split "`n")) {
        if ($l -match '^{\"') { try { return ($l | ConvertFrom-Json).count } catch {} }
    }
    return 0
}

Write-Host "=== Tab Safety Final ===" -ForegroundColor Cyan

# Before
$before = Get-TabCount
Write-Host "Before: $before tabs" -ForegroundColor Yellow

# view --extension
Write-Host "Running view --extension..." -ForegroundColor Yellow
cmd.exe /c "`"$CLI`" view --url https://example.com --extension > NUL 2>&1"
$afterView = Get-TabCount
Write-Host "After view: $afterView tabs (lost $($before - $afterView))" -ForegroundColor Yellow

# listen --extension (with piped commands)
$tmp = [System.IO.Path]::GetTempFileName()
@('{"action":"navigate","url":"https://example.com"}', '{"action":"tree"}') | Out-File $tmp -Encoding utf8 -Force
Get-Process -Name "agent-browser-cli" -ErrorAction SilentlyContinue | Stop-Process -Force -ErrorAction SilentlyContinue
Start-Sleep 1
cmd.exe /c "type `"$tmp`" | `"$CLI`" listen --extension > NUL 2>&1"
Remove-Item $tmp -ErrorAction SilentlyContinue
Start-Sleep 2

$afterListen = Get-TabCount
Write-Host "After listen: $afterListen tabs (lost $($before - $afterListen))" -ForegroundColor Yellow

# Judge
$allPass = ($afterView -ge $before - 2) -and ($afterListen -ge $before - 2)
Write-Host "Before: $before | After view: $afterView | After listen: $afterListen" -ForegroundColor Cyan
if ($allPass) { Write-Host "`n[PASS] Tab safety verified" -ForegroundColor Green; exit 0 }
else { Write-Host "`n[FAIL] Tabs were lost" -ForegroundColor Red; exit 1 }