# 打包说明

## 前置

- Python 3.11（当前机器：`C:\Users\yang'wei\AppData\Local\Python\pythoncore-3.11-64\python.exe`）
- `py -3.11 -m pip install pyinstaller pystray`
- Inno Setup 6（可选，生成安装包）

## 构建 exe

```powershell
cd D:\Flutter\fixmath
powershell -ExecutionPolicy Bypass -File .\packaging\build.ps1
```

产物：`dist\FixMath\FixMath.exe`（onedir，整个 `dist\FixMath` 需一起分发）。

## 生成安装包

用 Inno Setup 打开 `packaging\fixmath.iss` 编译，产物在 `dist\FixMath-Setup-0.1.0.exe`。

安装包会包含：

- 程序与运行时；
- 开始菜单、桌面快捷方式（可选）；
- 卸载；
- 开机自启（可选，写 HKCU Run）。

**不包含**模型权重（`%APPDATA%\pix2text`，约 226 MB）。公开分发时建议：

1. 首次启动检测模型，缺失则提示下载；或
2. 把 `pix2text` 模型目录作为额外数据打进安装包。

## 体积

由于 `pix2text.latex_ocr` 顶层依赖 torch，onedir 约 1.5–2 GB，安装包约 0.7–1.2 GB。

要显著瘦身，需要把 OCR 加载层换成纯 onnxruntime + tokenizers（见 `P0_PLAN.md` 的体积优化项）。

## 常见构建问题

- `hook-torch` 很慢：正常，首次 10–30 分钟；
- 缺 `tensorboard` 警告：可忽略；
- 缺模型：先运行一次源码版，让 `%APPDATA%\pix2text` 下载完成；
- 缺 DLL：确认 Visual C++ 运行库已安装（Windows 10/11 通常自带）。
