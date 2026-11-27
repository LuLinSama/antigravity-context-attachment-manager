# Antigravity Context Attachment Manager

适用于 Windows 的 Antigravity 素材库。它记录会话中通过原生文件工具读取的图片、视频、音频和文档，按轮次展示，并可用 Windows 默认应用打开原件。

## 安装

需要支持插件 Hooks 和 MCP Apps 的 Antigravity 桌面客户端，以及 Python 3.10 或更新版本。在仓库目录打开 PowerShell，任选一种安装范围。

```powershell
# 全局安装
.\install.ps1 -Global

# 仅安装到指定工作区
.\install.ps1 -Workspace 'D:\your-workspace'
```

如果 `python` 不在 PATH 中，在安装命令末尾加上 `-Python 'C:\path\to\python.exe'`。安装完成后，完全退出 Antigravity，再从桌面或开始菜单重新打开。

## 使用

1. 在会话里让 Antigravity 使用 `read_file` 或 `view_file` 读取素材。
2. 点击侧栏「插件 → 上下文素材」，也可在会话侧面板添加「上下文素材」。
3. 按轮次查看素材，或按文件名搜索、按类型筛选。点击素材卡片即可打开原件。

面板默认跟随当前会话。打开多个 Antigravity 窗口时，可以通过「管理的会话」手动选择。会话日志暂不可用时，读取记录会显示在「未记录轮次」。

## 本地数据与请求

- 新增素材记录保存在 `%LOCALAPPDATA%\antigravity-context-attachment-manager\attachments.sqlite3`。插件保存原件路径和读取记录。预览依赖原文件仍在该路径。
- 素材库记录受支持的原生文件工具读取。直接拖入聊天的附件由 Antigravity 管理。
- 模型请求由 Antigravity 客户端直接发送。历史附件是否在后续轮次重新上传，由客户端决定。

## 开发

```powershell
python -m unittest discover -s tests -v
```

## 许可证

[MIT](LICENSE)
