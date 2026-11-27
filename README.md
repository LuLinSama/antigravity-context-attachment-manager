# Antigravity Context Attachment Manager

**当前版本 0.3.5 · Windows**

在 Antigravity 里照常上传图片、视频、音频和文档，或让工具读取文件。素材首次读入时随模型请求正常发送。后续请求里的历史素材由本地代理归档，并替换成带文件名、MIME 类型和 `CAM-…` 编号的引用。继续做文字工作时，模型可以沿用已有上下文。需要重新核对画面、声音或文档时，模型可按编号取回对应素材。

## 安装与启动

需要 Python 3.10+。素材面板适用于 Antigravity App 2.34+，已在 Windows 上的 Antigravity 2.35.2 验证。以下命令均在项目根目录的 PowerShell 中执行。

### 全局安装

适用于已有 `Antigravity.lnk` 桌面快捷方式，且快捷方式通过 `cmd.exe` 的 `start` 命令启动 Antigravity 的环境。

```powershell
.\install.ps1 -Global
.\configure-shortcut.ps1
```

完全退出 Antigravity，再从同一桌面快捷方式打开。配置脚本会备份原快捷方式并保留原启动命令，仅在应用启动时设置代理所需的环境变量。Antigravity 的 Sidecar 会启动本地代理。

快捷方式名称或位置不同时，可向 `configure-shortcut.ps1` 传入 `-Shortcut 'D:\path\to\Antigravity.lnk'`。如果快捷方式直接指向 `Antigravity.exe`，可用 `launch.ps1` 启动。如果 `python` 不在 `PATH` 中，可向全局安装脚本传入 `-Python` 和 `python.exe` 的完整路径。

### 工作区安装

```powershell
.\install.ps1 -Workspace 'D:\path\to\your-workspace'
.\launch.ps1
```

把示例路径替换为已有的工作区目录。运行 `launch.ps1` 前先退出 Antigravity，使用期间保持启动它的 PowerShell 窗口开启。可用 `-AntigravityPath` 指定应用路径，用 `-DataDirectory` 指定归档目录。工作区的 `media` MCP 服务使用 Antigravity 继承的 `PATH` 查找 `python`。`launch.ps1 -Python` 单独选择代理的解释器。

## 使用素材库

- **会话侧栏**。在当前会话右上角打开 **⋯ → 插件（Plugins）→ 上下文素材**。侧栏默认跟随 Antigravity 当前会话。切换官方会话时，素材、发送设置和流量统计会一起切换。当前会话尚无归档时显示空状态。
- **全页素材库**。从应用左侧打开 **插件（Plugins）→ 上下文素材**。全页初次打开显示最近归档。两个入口都可通过「管理的会话」手动查看其他归档并设置别名。在侧栏手动查看归档后，下一次切换官方会话时会恢复跟随。
- **按轮次浏览**。素材按轮次从新到旧排列，标明上传、工具读取、生成和按 ID 取回等来源。同一素材在同一轮合并显示，跨轮重新读取会出现在对应轮次。纯文字轮照常计入轮次，素材库只为有素材记录的轮次建组。支持按文件名或 `CAM-…` 编号搜索，也可按类型筛选。
- **打开原件**。常见小图显示缩略图，视频、音频和文档显示类型图标。点击有本机原件的卡片，会通过 Windows 默认应用打开归档副本。只有服务端引用的素材可尝试在会话中按 ID 取回。
- **控制历史素材发送**。默认「按需取回」。选择「暂停省流」可按 Antigravity 原有方式发送该会话的历史素材。在 `/compact` 后找不到编号时，可让模型用 `cam_list` 按文件名查找，再通过 `cam_fetch` 取回所需素材。

## 查看每轮流量

流量卡顶部显示当前轮次，手动查看归档时显示该会话最近轮次。**最近请求**显示最近一次已记录请求的改写后大小，**改写前**显示本地代理收到的请求体大小，**本次节省**是两者之差。卡片底部还显示该次请求中省略、恢复的素材数和会话累计节省。

素材库在「第 N 轮」标题旁显示该轮的 **改写前** 和 **改写后** 请求体合计。同一轮产生多次模型调用时，成功转发的生成请求会累加。统计范围仅限模型生成请求体，排除 `countTokens`、HTTP 开销和其他应用流量。缺少统计的旧轮次显示「流量未记录」。

### 50 MB 视频实测

在 Windows Antigravity 2.35.2 的同一会话中，第 5 轮用工具读取一段 **50.39 MB** 的合成 MP4。第 6 至第 8 轮只发送文字，均未再次读取或取回视频。

| 轮次 | 操作 | 改写前 | 改写后 |
| --- | --- | ---: | ---: |
| 第 5 轮 | 首次读取视频，两次模型调用 | 64.3 MiB | 64.2 MiB |
| 第 6 轮 | 纯文字 | 64.2 MiB | 84.0 KiB |
| 第 7 轮 | 纯文字 | 64.2 MiB | 84.8 KiB |
| 第 8 轮 | 纯文字 | 64.2 MiB | 85.6 KiB |

后三轮中，Antigravity 仍把历史视频交给本机代理。代理在转发前用引用替换了视频原件，三轮的恢复数均为 0。

## 兼容性与排查

- 代理处理 Gemini 请求中的 `inlineData` 和带 MIME 类型的 `fileData`，覆盖图片、视频、音频、PDF、Office 和 OpenDocument。`fileData` 归档的是服务 URI，复用依赖原 URI 的有效期和权限。取回后的媒体能否被解析，取决于所选模型和 Cloud Code 对相应类型的支持。
- 归档按会话隔离，默认保存在 `%LOCALAPPDATA%\antigravity-context-attachment-manager`。打开原件时生成的副本位于归档目录下的 `preview-cache`。缺少可靠轮次的旧素材会归入「未记录轮次」。
- 首次启动后可在插件面板确认 `media` MCP 已连接。若素材库入口尚未出现，先打开装有插件的项目会话，等 MCP 初始化后再打开菜单。恢复的旧标签页若提示 MCP 尚未加载，关闭后从菜单重新打开。若本机桌面路由不可用，或打开多个窗口后无法识别当前会话，可在「管理的会话」手动选择归档。
- 未识别的请求格式、缺少会话 ID 或取回工具未就绪时，代理会原样转发。`CLOUD_CODE_URL` 属于 Antigravity 内部入口，客户端升级后可通过新会话或 `launch.ps1 -Observe` 检查代理接入。观察模式记录请求大小和协议识别情况，保持请求体原样转发。
- 代理监听 `127.0.0.1`，模型请求上游限定为 Google Cloud Code HTTPS 主机。全局模式下可运行 `.\configure-shortcut.ps1 -Disable`，再退出应用并从原快捷方式启动，以恢复原路由。手动启动模式下，退出应用后从常规入口启动即可恢复原路由。

## 开发验证

```powershell
python -m unittest discover -s tests -v
```

桌面回归可以用一个新会话完成。先上传测试图片，再让 Antigravity 用 `read_file` 读取测试 MP4。发送纯文字追问，检查「本次节省」和「恢复 0 项」。然后要求按编号取回 MP4，确认恢复数增加。执行 `/compact` 后，可以再按文件名查找并取回图片。

## 项目贡献者

- [LuLinSama](https://github.com/LuLinSama)

## 参考资料

实现参考了原项目的历史占位符、本地归档和按 ID 取回设计。[Context Attachment Manager](https://github.com/chipfighter/context-attachment-manager)

- [Antigravity 插件文档](https://antigravity.google/docs/build-plugins/)
- [Gemini 多媒体函数响应](https://ai.google.dev/gemini-api/docs/function-calling#multimodal)
