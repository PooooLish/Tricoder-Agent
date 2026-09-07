# TriCoder 一键测试与沙盒冒烟脚本
#
# 用法：
#   .\run_tests.ps1                一键运行全部自动化测试（unittest + compileall）
#   .\run_tests.ps1 -Tui           启动 Textual TUI，工作区固定在 test/ 沙盒
#   .\run_tests.ps1 -Chat          启动交互 chat，工作区固定在 test/ 沙盒
#   .\run_tests.ps1 -Provider openai  切换 Provider（默认 deepseek）

param(
    [switch]$Tui,
    [switch]$Chat,
    [string]$Provider = "deepseek"
)

$ErrorActionPreference = "Stop"
$projectRoot = $PSScriptRoot
Set-Location -LiteralPath $projectRoot

if (-not (Test-Path -LiteralPath ".venv")) {
    Write-Host "未找到 .venv。请先执行：" -ForegroundColor Red
    Write-Host "  python -m venv .venv" -ForegroundColor Yellow
    Write-Host "  .\.venv\Scripts\python -m pip install -e ." -ForegroundColor Yellow
    exit 1
}
$py = Join-Path $projectRoot ".venv\Scripts\python.exe"
$testWorkspace = Join-Path $projectRoot "test"

if ($Tui -or $Chat) {
    if (-not (Test-Path -LiteralPath $testWorkspace)) {
        Write-Host "测试沙盒目录不存在：$testWorkspace" -ForegroundColor Red
        exit 1
    }
    if (-not (Test-Path -LiteralPath (Join-Path $testWorkspace ".env.local"))) {
        Write-Host "警告：test\.env.local 不存在；真实 API 冒烟需要先在 test\.env.local 配置密钥。" -ForegroundColor Yellow
    }
    $mode = if ($Tui) { "tui" } else { "chat" }
    Write-Host "==> 启动 $mode，工作区固定在 $testWorkspace（Provider: $Provider）" -ForegroundColor Cyan
    & $py -m tricoder $mode --provider $Provider --workspace $testWorkspace
    exit $LASTEXITCODE
}

Write-Host "=== 单元测试（tests/）===" -ForegroundColor Cyan
& $py -m unittest discover -s tests -v
if ($LASTEXITCODE -ne 0) {
    Write-Host "单元测试失败" -ForegroundColor Red
    exit $LASTEXITCODE
}

Write-Host "=== 编译检查（src + tests）===" -ForegroundColor Cyan
& $py -m compileall -q src tests
if ($LASTEXITCODE -ne 0) {
    Write-Host "编译检查失败" -ForegroundColor Red
    exit $LASTEXITCODE
}

Write-Host ""
Write-Host "=== 全部通过 ===" -ForegroundColor Green
Write-Host "提示：运行真实 API 冒烟请使用： .\run_tests.ps1 -Tui" -ForegroundColor DarkGray
