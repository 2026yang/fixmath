# FixMath

离线公式截图工具：在任意程序中选中公式，按 `Ctrl+Alt+7`，得到可复制的 LaTeX，或直接粘贴为 MathType 公式。全程不联网。

## 功能

- 任意程序里左键选中公式，按 `Ctrl+Alt+7` 截图识别；
- 浏览器模式：自动处理网页选区，手动框选可缩放/平移；
- 清洗图 / 框选图双视图，点击任一图重新识别；
- LaTeX 实时预览（复杂结构自动跳过，避免卡顿）；
- 一键复制 `$…$`、纯 LaTeX、透明公式图；
- 复制为 MathType 公式（需要本机安装 MathType）；
- 历史记录、搜索、保留条数、清空记录；
- 系统托盘、开机自启、诊断日志；
- 完全离线：本地 Pix2Text ONNX 模型。

## 快捷键

默认且**唯一**的全局快捷键：

```
Ctrl+Alt+7   截图识别
```

其他操作都是编辑器里的按钮，不注册系统级快捷键，避免和 WPS/浏览器冲突。

## 安装

### 发布版

从 [Releases](../../releases) 下载 `FixMath-Setup-x.y.z.exe`（或 `FixMath-x.y.z.zip`），解压后运行 `FixMath.exe`。

首次启动会加载本地 OCR 模型，约 5–10 秒。

### 从源码运行

```powershell
git clone https://github.com/2026yang/fixmath.git
cd fixmath
py -3.11 -m venv .venv
.venv\Scripts\activate
pip install -r requirements.txt
python fixmath_app.py
```

或双击 `run_fixmath.bat`。

## 数据目录

| 内容 | 路径 |
|---|---|
| 配置 | `%LOCALAPPDATA%\FixMath\config.json` |
| 截图历史 | `%LOCALAPPDATA%\FixMath\captures` |
| 日志 | `%LOCALAPPDATA%\FixMath\logs` |
| OCR 模型 | `%APPDATA%\pix2text` |

`设置 → 清空所有记录` 可删除全部历史。

## 构建 exe

```powershell
pip install pyinstaller pystray
powershell -ExecutionPolicy Bypass -File packaging\build.ps1
```

产物：`dist\FixMath\FixMath.exe`（整个 `dist\FixMath` 需一起分发）。

模型权重不随 exe 打包，首次运行从 `%APPDATA%\pix2text` 读取或下载。

## 发布新版本（更新机制）

更新检查使用 GitHub Releases：

1. 修改 `version.py` 里的 `__version__` 和 `GITHUB_REPO`；
2. 构建 exe；
3. 打 tag 并发布 Release：

```powershell
git tag v0.1.1
git push origin v0.1.1
gh release create v0.1.1 dist\FixMath-Setup-0.1.1.exe --title "v0.1.1" --notes "fix: ..."
```

客户端在 `设置 → 检查更新` 中查询 `releases/latest`，比较版本后打开下载页。

发布包建议包含：

- `FixMath-Setup-x.y.z.exe`（Inno Setup，见 `packaging/fixmath.iss`）；
- 或 `FixMath-x.y.z.zip`（整个 onedir）。

## 目录结构

```
capture/      截图助手（Win32 钩子 + 区域截图）
editor/       Tk 编辑器
ocr/          本地 OCR HTTP 服务
packaging/    PyInstaller / Inno Setup 脚本
vendor/       latex2mathml 与公式检测模型
version.py    版本号 + GitHub 仓库
update_checker.py
```

## 已知限制

- 免费离线模型对复杂表格、矩阵、多行对齐仍可能漏项；
- 浏览器白字蓝底选区需要先取消选中；
- MathType 公式依赖本机安装的 MathType；
- 长文本混排（文字 + 公式）尚未实现。

## 许可证

MIT，见 [LICENSE](LICENSE)。
