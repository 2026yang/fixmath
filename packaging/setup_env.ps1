param(
    [switch]$CheckOnly
)

$ErrorActionPreference = "Stop"
$root = Split-Path $PSScriptRoot -Parent
Set-Location $root

function Write-Step([string]$text) {
    Write-Host ""
    Write-Host "== $text ==" -ForegroundColor Cyan
}

Write-Host "FixMath 源码环境安装" -ForegroundColor Green
Write-Host "目录：$root"

# ---------------------------------------------------------------- Python
Write-Step "检查 Python 3.11"

$py = $null
$pyArgs = @()
try {
    & py -3.11 -c "import sys; print(sys.version)" *> $null
    if ($LASTEXITCODE -eq 0) {
        $py = "py"
        $pyArgs = @("-3.11")
    }
} catch { }

if (-not $py) {
    try {
        $ver = & python -c "import sys; print(f'{sys.version_info.major}.{sys.version_info.minor}')" 2>$null
        if ($ver -eq "3.11") {
            $py = "python"
        }
    } catch { }
}

if (-not $py) {
    Write-Host "未找到 Python 3.11。" -ForegroundColor Red
    Write-Host "请先安装：https://www.python.org/downloads/release/python-3119/"
    Write-Host "安装时勾选 Add python.exe to PATH，然后重新双击本文件。"
    exit 1
}

$version = & $py @pyArgs -c "import sys; print(sys.version.split()[0])"
Write-Host "Python $version 已找到。"

# ---------------------------------------------------------------- packages
Write-Step "检查依赖"

$missing = & $py @pyArgs -c @"
import importlib.util as u
mods = ('pix2text', 'torch', 'onnxruntime', 'PIL', 'numpy', 'matplotlib', 'pystray')
print(','.join(m for m in mods if u.find_spec(m) is None))
"@

if ([string]::IsNullOrWhiteSpace($missing)) {
    Write-Host "依赖已安装。" -ForegroundColor Green
} elseif ($CheckOnly) {
    Write-Host "缺少依赖：$missing（CheckOnly 模式不安装）" -ForegroundColor Yellow
} else {
    Write-Host "缺少：$missing"
    Write-Host "开始安装依赖，首次可能需要 10–30 分钟……" -ForegroundColor Yellow
    & $py @pyArgs -m pip install --upgrade pip
    & $py @pyArgs -m pip install -r (Join-Path $root "requirements.txt")
    if ($LASTEXITCODE -ne 0) {
        Write-Host "依赖安装失败，请检查网络后重试。" -ForegroundColor Red
        exit 1
    }
    Write-Host "依赖安装完成。" -ForegroundColor Green
}

# ---------------------------------------------------------------- OCR model
Write-Step "检查 OCR 模型"

$modelDir = Join-Path $env:APPDATA "pix2text\1.1\mfr-1.5-onnx"
$modelOk = (Test-Path (Join-Path $modelDir "encoder_model.onnx")) -and
           (Test-Path (Join-Path $modelDir "decoder_model.onnx"))

if ($modelOk) {
    Write-Host "OCR 模型已安装：$modelDir" -ForegroundColor Green
} elseif ($CheckOnly) {
    Write-Host "OCR 模型未安装：$modelDir（CheckOnly 模式不下载）" -ForegroundColor Yellow
} else {
    Write-Host "OCR 模型未找到，开始下载（约 100 MB）……" -ForegroundColor Yellow
    & $py @pyArgs -c "from pix2text.latex_ocr import LatexOCR; LatexOCR(model_backend='onnx', device='cpu', more_model_configs={'use_merged': False, 'use_cache': False})"
    if ($LASTEXITCODE -ne 0 -or -not (Test-Path (Join-Path $modelDir "encoder_model.onnx"))) {
        Write-Host "模型下载失败，请检查网络后重试。" -ForegroundColor Red
        exit 1
    }
    Write-Host "OCR 模型已就绪。" -ForegroundColor Green
}

Write-Host ""
if ($CheckOnly) {
    Write-Host "检查完成（未修改环境）。" -ForegroundColor Green
} else {
    Write-Host "全部就绪。双击 run_fixmath.bat 启动 FixMath。" -ForegroundColor Green
}
