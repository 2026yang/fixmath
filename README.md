# FixMath

离线公式截图工具：在任意程序中选中公式，按 `Ctrl+Alt+7`，得到可复制的 LaTeX，或粘贴为 MathType 公式。识别全程在本机完成，不上传图片。

> 当前只提供**源码运行**方式，暂未发布预编译 exe。源码 ZIP 需要 Python 3.11；双击 `安装依赖.bat` 会自动准备环境。

## 功能

- 任意程序里左键选中公式，按 `Ctrl+Alt+7` 截图识别；
- 浏览器模式：自动处理网页选区；手动框选可缩放/平移；
- 清洗图 / 框选图双视图，点击任一图重新识别；
- LaTeX 实时预览（复杂结构自动跳过，不卡界面）；
- 一键复制 `$…$`、纯 LaTeX、透明公式图；
- 复制为 MathType 公式（需要本机安装 MathType）；
- 历史记录、搜索、保留条数、清空记录；
- 系统托盘、开机自启、诊断日志；
- 完全离线：本地 Pix2Text ONNX 模型。

## 唯一快捷键

```
Ctrl+Alt+7   截图识别
```

其他操作都是编辑器里的按钮，不注册系统级快捷键，避免和 WPS/浏览器冲突。

## 环境要求

| 条件 | 说明 |
|---|---|
| Windows 10/11 64 位 | 使用了 Win32 钩子与 DPI 接口 |
| Python 3.11 | 源码运行必需 |
| 磁盘约 8 GB | Python 依赖 + torch + 模型 |
| 网络 | 仅首次安装依赖/下载模型时需要 |

## 快速开始

### 1. 下载源码

```
https://github.com/2026yang/fixmath/archive/refs/heads/main.zip
```

或在 GitHub 页面点 `Code → Download ZIP`。

解压后目录里应有：

```
安装依赖.bat
run_fixmath.bat
fixmath_app.py
requirements.txt
capture\
editor\
ocr\
packaging\
vendor\
```

### 2. 双击安装依赖

双击：

```
安装依赖.bat
```

脚本会自动：

1. 检查 Python 3.11，**缺失时自动下载并静默安装**（当前用户，无需管理员）；
2. 检查依赖，缺失时执行 `pip install -r requirements.txt`；
3. 检查 OCR 模型：**源码已内置**，直接使用；只有模型缺失时才下载；
4. **已安装的部分直接提示“已安装”**，不会重复安装。

### 3. 启动

双击：

```
run_fixmath.bat
```

首次启动会加载 OCR 模型，约 5–10 秒。

### 4. 使用

1. 在 WPS / 浏览器 / PDF 里选中公式；
2. 按 `Ctrl+Alt+7`；
3. 编辑器里复制 LaTeX，或点“复制为 MathType 公式”。

## 手动安装（可选）

如果不想用 `安装依赖.bat`：

```powershell
py -3.11 -m pip install -r requirements.txt
py -3.11 fixmath_app.py
```

模型优先从源码目录读取：

```
vendor\pix2text\1.1\mfr-1.5-onnx
```

源码 ZIP 已包含该模型，正常不需要下载。只有手动删除了模型时，才会回退到：

```
%APPDATA%\pix2text\1.1\mfr-1.5-onnx
```

仍缺失时首次识别会自动下载。

## 数据目录

| 内容 | 路径 |
|---|---|
| 配置 | `%LOCALAPPDATA%\FixMath\config.json` |
| 截图历史 | `%LOCALAPPDATA%\FixMath\captures` |
| 日志 | `%LOCALAPPDATA%\FixMath\logs` |
| OCR 模型 | 源码 `vendor\pix2text`；回退 `%APPDATA%\pix2text` |

`设置 → 清空所有记录` 可删除全部历史。

## 常见问题

### 双击 `安装依赖.bat` 提示找不到 Python

正常情况下脚本会自动下载并安装 Python 3.11。

如果自动安装失败，再手动安装：

```
https://www.python.org/downloads/release/python-3119/
```

安装时勾选 **Add python.exe to PATH**，然后重新双击。

### 模型下载失败

通常是网络无法访问 HuggingFace。可以：

- 换网络后重试；
- 或从另一台已安装的电脑复制模型目录：
  ```
  %APPDATA%\pix2text\1.1\mfr-1.5-onnx
  ```
  到新电脑同一位置。

### `Ctrl+Alt+7` 没反应

- 确认 `run_fixmath.bat` 仍在运行；
- 如果 WPS/浏览器以管理员身份运行，FixMath 也需要以管理员身份运行；
- 输入法可能占用 `Ctrl+Alt+数字`，可在编辑器“快捷键”里换组合。

### MathType 复制失败

“复制为 MathType 公式”需要：

- Windows 桌面版 MathType 6.9 / 7.x；
- WPS 已安装 MathType 加载项。

普通 LaTeX 复制不需要 MathType。

## 开发与构建

```powershell
pip install pyinstaller pystray
powershell -ExecutionPolicy Bypass -File packaging\build.ps1
```

产物在 `dist\FixMath`。当前仓库不发布预编译 exe，构建仅供本地验证。

## 目录结构

```
fixmath/
├── 安装依赖.bat        源码环境安装
├── run_fixmath.bat     启动入口
├── fixmath_app.py      启动器
├── mathtype_bridge.py  MathType 转换
├── tray_win32.py       原生托盘
├── version.py          版本号
├── update_checker.py   更新检查（预留）
├── capture/            截图助手
├── editor/             Tk 编辑器
├── ocr/                本地 OCR 服务
├── packaging/          构建脚本
├── vendor/             latex2mathml、模型说明
└── .github/workflows/  CI
```

## 许可证

MIT，见 [LICENSE](LICENSE)。
