import re


FETCH_TOOL = "cam_fetch"
LIST_TOOL = "cam_list"
ATTACHMENT_ID = re.compile(r"CAM-[0-9a-f]{32}")
MARKER = "AGY_CAM_PROXY_V1 "
TOOL_PREFIX = "mcp_antigravity-context-attachment-manager_media_"


def tool_matches(name, tool):
    return name in (tool, TOOL_PREFIX + tool)
