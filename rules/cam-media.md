---
trigger: always_on
---

For normal source code and plain text (including Markdown, CSV and JSON), use the normal native reader. For a task that needs inspection of a local image, video, audio file, PDF or binary office document, the main agent should go directly to native `invoke_subagent` with `TypeName: cam-media` and `Workspace: inherit`. Do not try a direct read first. Supply only one user-authorized absolute path and the specific question per fresh child. When there are multiple files, inspect them one at a time; wait for the native result instead of polling.

Use only the child's plain-text findings. Do not send the original to the main context as an attachment, image link, encoded data, pasted document or tool output. Do not use shell, MCP, another reader, or a different child type to circumvent a CAM refusal. If the required media path is unknown, ask the user. Follow-ups answerable from findings require no new inspection; if needed, start a fresh CAM Media child, never message or reuse the old one. Once its text result has arrived and the child has completed, if the native `manage_subagents` tool is available, terminate only that completed child by its exact ID (`kill`, never `kill_all`). Do not delete native logs or data. If CAM Media or the native reader is unavailable, report the limitation.

If you are CAM Media, follow your own one-shot file-reading instructions. Never delegate and never try to work around a rejected read.
