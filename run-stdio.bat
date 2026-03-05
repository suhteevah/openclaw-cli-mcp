@echo off
REM OpenClaw CLI MCP — stdio mode (for Claude Desktop)
set OPENCLAW_LOG_LEVEL=verbose
set OPENCLAW_LOG_DIR=J:\openclaw-cli-mcp\logs
set OPENCLAW_MAX_SESSIONS=20
set OPENCLAW_SHELL=powershell.exe
python "J:\openclaw-cli-mcp\openclaw_cli_mcp.py" --transport stdio %*
