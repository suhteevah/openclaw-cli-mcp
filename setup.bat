@echo off
REM OpenClaw CLI MCP Server - Install dependencies and verify
echo === OpenClaw CLI MCP - Quick Setup ===

echo [1/3] Installing Python dependencies...
pip install "mcp[cli]" pydantic httpx --quiet
if %ERRORLEVEL% NEQ 0 (
    echo ERROR: pip install failed. Make sure Python is on PATH.
    pause
    exit /b 1
)

echo [2/3] Verifying installation...
python -c "import mcp; import pydantic; print(f'MCP: OK, Pydantic: OK')"
if %ERRORLEVEL% NEQ 0 (
    echo ERROR: Import check failed.
    pause
    exit /b 1
)

echo [3/3] Compile check...
python -m py_compile "%~dp0openclaw_cli_mcp.py"
if %ERRORLEVEL% NEQ 0 (
    echo ERROR: Server has syntax errors.
    pause
    exit /b 1
)

echo.
echo === Installation Complete ===
echo Server: %~dp0openclaw_cli_mcp.py
echo.
echo To run in stdio mode (Claude Desktop):
echo   python "%~dp0openclaw_cli_mcp.py" --transport stdio
echo.
echo To run in HTTP mode (remote):
echo   python "%~dp0openclaw_cli_mcp.py" --transport streamable_http --host 0.0.0.0 --port 8462
echo.
pause
