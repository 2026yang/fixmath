param(
    [switch]$CheckOnly
)

$ErrorActionPreference = "Continue"
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

function Find-Python311 {
    try {
        & py -3.11 -c "import sys; print(sys.version)" *> $null
        if ($LASTEXITCODE -eq 0) {
            return @{ Exe = "py"; Args = @("-3.11") }
        }
    } catch { }

    $candidates = @(
        (Join-Path $env:LOCALAPPDATA "Programs\Python\Python311\python.exe"),
        (Join-Path $env:ProgramFiles "Python311\python.exe"),
        (Join-Path ${env:ProgramFiles(x86)} "Python311\python.exe")
    )
    foreach ($exe in $candidates) {
        if (Test-Path $exe) {
            return @{ Exe = $exe; Args = @() }
        }
    }

    try {
        $ver = & python -c "import sys; print(f'{sys.version_info.major}.{sys.version_info.minor}')" 2>$null
        if ($ver -eq "3.11") {
            return @{ Exe = "python"; Args = @() }
        }
    } catch { }
    return $null
}

$found = Find-Python311

if (-not $found) {
    if ($CheckOnly) {
        Write-Host "未找到 Python 3.11（CheckOnly 模式不安装）。" -ForegroundColor Red
        exit 1
    }

    Write-Host "未找到 Python 3.11，开始自动下载安装……" -ForegroundColor Yellow
    $url = "https://www.python.org/ftp/python/3.11.9/python-3.11.9-amd64.exe"
    $installer = Join-Path $env:TEMP "python-3.11.9-amd64.exe"
    try {
        [Net.ServicePointManager]::SecurityProtocol = [Net.SecurityProtocolType]::Tls12
        Write-Host "下载：$url"
        Invoke-WebRequest -Uri $url -OutFile $installer -UseBasicParsing
    } catch {
        Write-Host "直接下载失败，尝试 curl……" -ForegroundColor Yellow
        & curl.exe -L --fail --output $installer $url
    }

    if (-not (Test-Path $installer)) {
        Write-Host "Python 安装包下载失败。" -ForegroundColor Red
        Write-Host "请手动下载安装：$url"
        Write-Host "安装时勾选 Add python.exe to PATH，然后重新双击本文件。"
        exit 1
    }

    Write-Host "正在安装 Python 3.11（当前用户，无需管理员）……"
    $proc = Start-Process -FilePath $installer -Wait -PassThru -ArgumentList @(
        "/quiet", "InstallAllUsers=0", "PrependPath=1",
        "Include_launcher=1", "Include_test=0", "Include_pip=1"
    )
    Write-Host "安装程序退出码：$($proc.ExitCode)"

    # Refresh PATH for this process from the registry, then look again.
    $env:Path = [Environment]::GetEnvironmentVariable("Path", "User") + ";" +
                [Environment]::GetEnvironmentVariable("Path", "Machine")
    $found = Find-Python311
}

if (-not $found) {
    Write-Host "Python 3.11 安装后仍未找到。" -ForegroundColor Red
    Write-Host "请手动下载安装：https://www.python.org/ftp/python/3.11.9/python-3.11.9-amd64.exe"
    Write-Host "安装时勾选 Add python.exe to PATH，然后重新双击本文件。"
    exit 1
}

$py = $found.Exe
$pyArgs = $found.Args

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

# ---------------------------------------------------------------- torch
Write-Step "检查 torch / torchvision 兼容性"

& $py @pyArgs -c "import torch, torchvision; print('torch', torch.__version__, 'torchvision', torchvision.__version__)" 2>$null
if ($LASTEXITCODE -ne 0) {
    if ($CheckOnly) {
        Write-Host "torch/torchvision 不兼容（CheckOnly 模式不修复）。" -ForegroundColor Yellow
    } else {
        Write-Host "torch/torchvision 版本不匹配，安装兼容版本……" -ForegroundColor Yellow
        & $py @pyArgs -m pip install --force-reinstall "torch==2.5.1" "torchvision==0.20.1"
        if ($LASTEXITCODE -ne 0) {
            Write-Host "torch/torchvision 修复失败，请检查网络。" -ForegroundColor Red
        }
    }
} else {
    Write-Host "torch/torchvision 版本匹配。" -ForegroundColor Green
}

# ---------------------------------------------------------------- OCR deps
Write-Step "检查 OCR 依赖兼容性"

& $py @pyArgs -c "from rapidocr.utils.model_resolver import resolve_model_key; print('rapidocr ok')" 2>$null | Out-Null
if ($LASTEXITCODE -ne 0) {
    if ($CheckOnly) {
        Write-Host "rapidocr/cnstd 版本不兼容（CheckOnly 模式不修复）。" -ForegroundColor Yellow
    } else {
        Write-Host "rapidocr 版本不兼容，安装固定版本……" -ForegroundColor Yellow
        & $py @pyArgs -m pip install --force-reinstall `
            "rapidocr==3.9.2" "cnstd==1.2.8" "cnocr==2.3.3" "pix2text==1.1.7"
        & $py @pyArgs -c "from rapidocr.utils.model_resolver import resolve_model_key; print('rapidocr ok')" 2>$null | Out-Null
        if ($LASTEXITCODE -ne 0) {
            Write-Host "OCR 依赖修复失败，请检查网络。" -ForegroundColor Red
        } else {
            Write-Host "OCR 依赖已修复。" -ForegroundColor Green
        }
    }
} else {
    Write-Host "OCR 依赖版本兼容。" -ForegroundColor Green
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
    & $py @pyArgs (Join-Path $root "packaging\download_model.py")
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
