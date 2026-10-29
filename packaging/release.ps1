param(
    [string]$Version = "",
    [switch]$SkipBuild,
    [switch]$Draft
)

$ErrorActionPreference = "Stop"
$root = Split-Path $PSScriptRoot -Parent
Set-Location $root

if (-not $Version) {
    $Version = & py -3.11 -c "import version; print(version.__version__)"
}
if (-not $Version) { throw "无法读取 version.py 中的版本号" }

if (-not $SkipBuild) {
    & powershell -ExecutionPolicy Bypass -File (Join-Path $PSScriptRoot "build.ps1")
    if ($LASTEXITCODE -ne 0) { throw "构建失败" }
}

$zip = Join-Path $root "dist\FixMath-$Version.zip"
if (Test-Path $zip) { Remove-Item $zip -Force }
Compress-Archive -Path (Join-Path $root "dist\FixMath\*") -DestinationPath $zip

$gh = Get-Command gh -ErrorAction SilentlyContinue
if (-not $gh) {
    Write-Host "未找到 gh CLI。zip 已生成：$zip"
    Write-Host "请手动创建 GitHub Release，或安装 https://cli.github.com/"
    exit 0
}

$args = @("release", "create", "v$Version", $zip,
          "--title", "v$Version",
          "--notes", "FixMath v$Version")
if ($Draft) { $args += "--draft" }
& gh @args
