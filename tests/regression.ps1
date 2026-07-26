$CLI = ".\target\debug\agent-browser-cli.exe"
$URL = "http://127.0.0.1:8080/oopif_main.html"
$PASS = 0
$FAIL = 0

function Test-Result {
    param($N, $C, $D)
    if ($C) { Write-Host "  [PASS] $N" -ForegroundColor Green; $script:PASS++ }
    else { Write-Host "  [FAIL] $N" -ForegroundColor Red; if ($D) { Write-Host "         $D" }; $script:FAIL++ }
}

Write-Host "=== Agent Browser CLI 回归测试 ===" -ForegroundColor Cyan

# 1. 环境检查
Write-Host "`n--- [1/5] 环境检查 ---" -ForegroundColor Yellow
$r = curl.exe -s -o NUL -w "%{http_code}" "http://127.0.0.1:8080/oopif_main.html"
Test-Result "Fixture 8080" ($r -eq "200") "HTTP=$r"
$r = curl.exe -s -o NUL -w "%{http_code}" "http://127.0.0.1:8081/oopif_frame.html"
Test-Result "Fixture 8081" ($r -eq "200") "HTTP=$r"

# 2. 端口模式
Write-Host "`n--- [2/5] 端口模式 --connect ---" -ForegroundColor Yellow
$o = & $CLI view --url $URL --connect "http://127.0.0.1:9222" 2>&1
Test-Result "端口: 状态=ok" ($o -match '"status":"ok"') ""
Test-Result "端口: 主 frame" ($o -match "f0-e1") ""
Test-Result "端口: 跨域 iframe" ($o -match "f1-e1") ""
Test-Result "端口: 交互元素=2" ($o -match '"interactive_count":2') ""
$global:pt = $o

# 3. 自动检测
Write-Host "`n--- [3/5] 自动检测 ---" -ForegroundColor Yellow
$o = & $CLI view --url $URL 2>&1
Test-Result "自动: 状态=ok" ($o -match '"status":"ok"') ""
Test-Result "自动: 交互元素=2" ($o -match '"interactive_count":2') ""

# 4. 扩展模式
Write-Host "`n--- [4/5] 扩展模式 --extension ---" -ForegroundColor Yellow
$o = & $CLI view --url $URL --extension 2>&1
Test-Result "扩展: 状态=ok" ($o -match '"status":"ok"') ""
Test-Result "扩展: 主 frame" ($o -match "f0-e1") ""
Test-Result "扩展: 跨域 iframe" ($o -match "f1-e1") ""
Test-Result "扩展: 交互元素=2" ($o -match '"interactive_count":2') ""
$global:et = $o

# 三模式一致
if ($global:pt -and $global:et) {
    Test-Result "三模式一致" ($global:pt -eq $global:et) ""
}

# 5. 下载
Write-Host "`n--- [5/5] 下载 ---" -ForegroundColor Yellow
$o = & $CLI download-test 2>&1
Test-Result "下载: 完成" ($o -match "Download complete") ""
Test-Result "下载: 有路径" ($o -match "filename:") ""

# 汇总
Write-Host "`n=== 完成 PASS:$PASS FAIL:$FAIL ===" -ForegroundColor Cyan
if ($FAIL -gt 0) { exit 1 }
exit 0