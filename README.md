# Antigravity Context Attachment Manager

**CAM 0.5.0 · Windows 桌面插件 · MIT**

需要分析本地图片、视频、音频或 PDF 时，把**路径**发给主会话，由一次性的 `cam-media` 子 Agent 读取原件，只把文字分析带回主会话。后续追问如果凭已有分析就能回答，主 Agent 不必再次读取文件。

CAM 使用 Antigravity 的原生子 Agent、文件读取工具和请求流程。**不反代、不接管模型端点、不解密或改写模型请求。** 素材库只记录原件引用；可选的上行统计只读本机已有的 V2Ray/Xray 计数器。

## 快速开始

需要 Windows、Python 3.10+，以及支持插件、Hooks、自定义子 Agent 和 MCP Apps 的 Antigravity 桌面客户端。

1. 克隆或下载本仓库，在仓库目录里打开 PowerShell。
2. 选择一种安装范围：

   ```powershell
   # 当前用户全局安装
   .\install.ps1 -Global

   # 或者：仅安装到一个已存在的工作区
   .\install.ps1 -Workspace 'D:\your-workspace'
   ```

   如果 `python` 不在 PATH，在同一条安装命令后加 `-Python 'C:\path\to\python.exe'`。同一个工作区建议只用一种安装范围，避免同时加载不同版本。
3. 完全退出并重新打开 Antigravity。向主会话发送**纯文本绝对路径**，例如：

   > 请分析 D:/media/sample.mp4，按时间顺序总结三处关键画面。

   不要拖入文件，也不要使用聊天框的附件或文件引用功能。这些入口不受 CAM 的读取 Hook 控制。
4. 打开「插件 → 上下文素材」。面板默认跟随 Antigravity 当前会话；也可以从下拉框选择历史会话。后续可以直接基于子 Agent 返回的文字追问，不需要再发路径。

## 素材如何与主会话隔离

```text
主 Agent 收到本地文件分析任务
├─ 源码、Markdown、CSV 等普通文本 → 原生读取，正常放行
└─ 图片、视频、音频、PDF、二进制 Office 文档
   ├─ 正常路径：主 Agent 直接调用原生 invoke_subagent（cam-media）
   └─ 兜底路径：主 Agent 直接调用 read_file / view_file
                 → PreToolUse 拦截，并提示改由 cam-media 读取
                             ↓
              独立子 Agent 上下文读取一个原件，只返回文字
                             ↓
              主 Agent 使用该结果继续任务，不把原件带回主上下文
```

- **一次一份素材。** `cam-media` 只允许一次媒体读取，不能继续委派。多份素材按顺序分别读取；身份无法验证或读取失败时报告限制，不循环创建子 Agent，也不换工具绕开拦截。CAM 按本地路径和扩展名识别媒体，实际可读格式取决于客户端的原生读取工具和所选模型。
- **用完不复用。** 子 Agent 完成且结果送达后，如原生 `manage_subagents` 可用，主 Agent 按精确 ID 终止对应子 Agent，不影响其他任务。已确认结束的子 Agent 如果被再次唤醒，Hook 会在下一次模型调用前终止它。确实需要重新检查原件时，才启动新的 `cam-media`。
- **不是安全沙箱。** Hook 只覆盖已配置的原生工具；不控制手动附件，也不删除 Antigravity 自己的会话记录、日志或缓存。子 Agent 读取素材时，Antigravity 仍可按原生流程把它发给模型服务。

## 看懂「上下文素材」面板

| 界面 | 展示内容 |
| --- | --- |
| 上方流量栏 | 最新记录轮次，以及该轮「主 Agent」「子 Agent」各自的上行估算。没有素材的纯文字轮次也可显示流量。 |
| 每轮素材标题 | 该轮轮次；有采样记录时，同时显示主、子 Agent 的上行。 |
| 素材卡片 | 经验证的子 Agent 成功读取的原件，归到发起委派的主会话轮次。图片按需从原件读取缩略图；点击卡片使用 Windows 默认应用打开原件。 |

素材编号只是本地索引，不会自动把文件附加到对话。CAM 不备份原件、不建预览缓存；原件移动或删除后，旧路径无法预览或打开。素材卡片说明原件**被读取**，不是已上传该原件的证明。

## 可选：查看 V2Ray / Xray 上行

不需要准备计数器就能使用素材隔离和素材库。需要看流量时，使用现有 V2Ray/Xray 核心已经开放在 IPv4 回环地址上的 Metrics 接口。CAM 默认读取 `127.0.0.1:10812` 的 `/debug/vars`，取 JSON 字段 `stats.outbound.proxy.uplink`。如果端口或出站标签不同，在 `%LOCALAPPDATA%\antigravity-context-attachment-manager\traffic.json` 使用实际值：

```json
{
  "enabled": true,
  "port": 10812,
  "outboundTag": "proxy"
}
```

`outboundTag` 是计数器中的出站标签，不是域名过滤器。CAM 不启动或配置代理，不变更系统代理或路由，也不自行过滤 Google 流量。设 `"enabled": false` 可关闭采样，不影响素材读取和归档。

**这些数字是模型调用窗口内的出站计数增量，不是各 Agent 的精确请求字节数。** 共享出站标签可能计入其他会话或进程的流量，窗口外的传输也不在统计里。发生并发、计数回退或重置、采样边界缺失、接口不可用时，面板标出原因，不用 `0` 冒充未知；若有能独立采样的窗口，会单独显示那部分估算。因此不要用面板数字推算精确节省比例，也不要用一次纯文字轮次的计数来断定原件有没有被原生客户端重传。

## 从 0.4.x 升级

更新仓库后，按原安装范围重跑 `install.ps1`，再完全重启 Antigravity。新版用原生子 Agent 隔离素材上下文；不再提供旧版的「改写前」「本次节省」或「累计节省」指标。

CAM 的本地数据放在 `%LOCALAPPDATA%\antigravity-context-attachment-manager`。`attachments.sqlite3` 保存素材引用，`usage.sqlite3` 保存调用窗口与计数记录；旧数据不会被升级脚本删除，旧反代统计和未经新版子 Agent 验证的素材不会混入新版面板。

## 排查与验证

| 现象 | 先检查什么 |
| --- | --- |
| 主 Agent 没有调用 `cam-media` | 安装后是否完全重启、是否打开了对应工作区；确认发送的是纯文本绝对路径，而不是聊天附件。 |
| 面板没有新增素材 | 读取是否成功，是否由 `cam-media` 执行，面板是否选中了对应的主会话。 |
| 卡片能看见但打不开 | 原件是否还在原路径，以及 Windows 是否关联了该格式的默认应用。 |
| 流量显示「计数不可用」 | 确认现有核心的 `/debug/vars` 在本机可读、出站计数已启用，且端口和标签与 `traffic.json` 一致。 |

0.5.0 桌面回归覆盖了图片直接委派、主 Agent 直接读图被拦截、普通文本放行和 PDF 子 Agent 读取。一次合成视频回归中，第 9 轮只有子 Agent 读取并归档了 **52.1 MiB** 的视频，随后该子 Agent 被终止；第 11 轮的纯文字追问没有调用工具或新建子 Agent，主 Agent 窗口的出站估算约为 **4.93 MiB**。这验证了追问时没有再次调用读取工具，**不是**对原生客户端网络载荷的验证。

运行项目测试：

```powershell
python -m unittest discover -s tests -v
```

参考文档：[Antigravity Hooks](https://antigravity.google/docs/hooks/) · [Antigravity 自定义子 Agent](https://antigravity.google/docs/subagents/) · [Xray Metrics](https://xtls.github.io/en/config/metrics.html)

许可证：[MIT](LICENSE)
