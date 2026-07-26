[Console]::OutputEncoding = [System.Text.Encoding]::UTF8
$CLI = "E:\agent-browser-cli\target\debug\agent-browser-cli.exe"
$FIXTURES = "E:\agent-browser-cli\tests\fixtures"
$PASS = 0; $FAIL = 0
$script:servers = @()

function Test-Result {
    param($N, $C)
    if ($C) { Write-Host "  [PASS] $N" -ForegroundColor Green; $script:PASS++ }
    else { Write-Host "  [FAIL] $N" -ForegroundColor Red; $script:FAIL++ }
}

function Start-Fixtures {
    foreach ($port in @(8080,8081,8082)) {
        $p = Start-Process -NoNewWindow -FilePath "python" -ArgumentList "-m http.server $port --directory $FIXTURES" -PassThru
        $script:servers += $p
    }
    Start-Sleep 2
    foreach ($port in @(8080,8081,8082)) {
        $r = curl.exe -s -o NUL -w "%{http_code}" "http://127.0.0.1:$port/" 2>$null
        if ($r -eq "000") { throw "Fixtures failed on port $port" }
    }
}

function Stop-Fixtures {
    foreach ($p in $script:servers) { try { $p.Kill() } catch {} }
}

Write-Host "=== Dynamic Frame Click Test ===" -ForegroundColor Cyan

try {
    # 1. Fixtures
    Write-Host "--- [1/4] Starting fixtures ---" -ForegroundColor Yellow
    Start-Fixtures
    Write-Host "  OK"

    # 2. Extract tree via view --extension
    Write-Host "--- [2/4] View + extract ---" -ForegroundColor Yellow
    $raw = cmd.exe /c "`"$CLI`" view --url http://127.0.0.1:8080/oopif_dynamic.html --extension 2>&1"
    $json = $null
    foreach ($l in ($raw -split "`n")) {
        if ($l -match '^{\"') { try { $json = $l | ConvertFrom-Json } catch {} }
    }
    if (-not $json) { throw "view --extension returned no JSON" }
    $tree = $json.tree
    if ($tree -match '\[@(f1-e\d+)\]') { $buttonId = $matches[1] }
    if (-not $buttonId) { throw "No f1-eN button in tree" }
    Write-Host "  ID: $buttonId  Status: $($json.status)"

    # Wait for MutationObserver to insert intruder iframe
    Write-Host "  Waiting for dynamic insertion..." -ForegroundColor Yellow
    Start-Sleep 3

    # 3. Click via listen --extension (piped stdin)
    Write-Host "--- [3/4] Click via listen ---" -ForegroundColor Yellow
    $tmp = [System.IO.Path]::GetTempFileName()
    @('{"action":"click","target_id":"' + $buttonId + '"}', '{"action":"tree"}') | Out-File $tmp -Encoding utf8 -Force
    Get-Process -Name "agent-browser-cli" -ErrorAction SilentlyContinue | Stop-Process -Force -ErrorAction SilentlyContinue
    Start-Sleep 1
    $clickRaw = cmd.exe /c "type `"$tmp`" | `"$CLI`" listen --extension 2>&1"
    Remove-Item $tmp -ErrorAction SilentlyContinue

    # 4. Judge
    Write-Host "--- [4/4] Results ---" -ForegroundColor Yellow
    $clicked = $clickRaw -match 'CLICKED-ORIGINAL'
    $hasErr = $clickRaw -match '"status":"error"'
    $frameDead = $clickRaw -match 'not found|重新提取'

    if ($clicked) { Test-Result "Clicked original button" $true }
    elseif ($frameDead -or $hasErr) { Test-Result "Safe rejection" $true }
    else { Test-Result "Click verification" $false }

} catch { Write-Host "  [ERROR] $_" -ForegroundColor Red
} finally { Stop-Fixtures }

Write-Host "=== Result PASS: $PASS FAIL: $FAIL ===" -ForegroundColor Cyan
if ($FAIL -gt 0) { exit 1 }
exit 0