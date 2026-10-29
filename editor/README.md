# FixMath 编辑器（MVP）

左图右文：左边是截图 `clean.png`，右边是可编辑的 LaTeX。  
新截图到达时自动跳到最新一条；`review` 非空时顶部会出现橙色提示。

## 快捷键

| 键 | 功能 |
|---|---|
| `Ctrl+Shift+C` | 复制 `$…$` |
| `Ctrl+S` | 保存修改到该条目的 `edit.json` |
| `Alt+← / Alt+→` | 上一条 / 下一条 |

## 启动

```bat
run_editor.bat
```

或

```powershell
py -3.11 fixmath_editor.py
```

默认读取 `%LOCALAPPDATA%\FixMath\captures`；可用 `--captures <dir>` 指定。

## 说明

- 不修改 `meta.json`，人工修改写在同目录的 `edit.json`，重新识别不会覆盖；
- 编码是纯文本编辑器，不是所见即所得；等网络允许后再换 MathLive；
- 暂不做语法高亮，`copy` 前建议自己扫一眼括号。
