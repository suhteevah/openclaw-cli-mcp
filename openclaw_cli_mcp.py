#!/usr/bin/env python3
"""
OpenClaw CLI Control MCP Server — Cross-Platform Edition
=========================================================
Full terminal controller for remote AI command & control.
Supports Windows (asyncio subprocess + ConPTY via winpty when available)
and Linux/macOS (native PTY via pty.openpty + os.fork).

Author: Matt Gates / Ridge Cell Repair LLC
GitHub: @suhteevah
"""

import asyncio
import json
import os
import platform
import signal
import sys
import time
import uuid
import logging
import subprocess
from collections import deque
from contextlib import asynccontextmanager
from datetime import datetime, timezone
from enum import Enum
from pathlib import Path
from typing import Any, Dict, List, Optional

from mcp.server.fastmcp import FastMCP
from pydantic import BaseModel, Field, ConfigDict, field_validator

IS_WINDOWS = platform.system() == "Windows"
if not IS_WINDOWS:
    import pty, select, struct, termios, fcntl
HAS_WINPTY = False
if IS_WINDOWS:
    try:
        import winpty
        HAS_WINPTY = True
    except ImportError:
        pass

if IS_WINDOWS:
    _default_log_dir = os.path.join(os.environ.get("LOCALAPPDATA", "C:\\openclaw"), "openclaw", "logs")
else:
    _default_log_dir = "/var/log/openclaw"
LOG_DIR = Path(os.environ.get("OPENCLAW_LOG_DIR", _default_log_dir))
try:
    LOG_DIR.mkdir(parents=True, exist_ok=True)
except PermissionError:
    LOG_DIR = Path.home() / ".openclaw" / "logs"
    LOG_DIR.mkdir(parents=True, exist_ok=True)
LOG_FILE = LOG_DIR / "cli-mcp.log"
LOG_LEVEL = os.environ.get("OPENCLAW_LOG_LEVEL", "verbose").lower()
VERBOSE_LEVEL = 15
logging.addLevelName(VERBOSE_LEVEL, "VERB")

class OpenClawFormatter(logging.Formatter):
    LEVEL_MAP = {"DEBUG":"DEBG","VERB":"VERB","INFO":"INFO","WARNING":"WARN","ERROR":"ERR!","CRITICAL":"CRIT"}
    def format(self, record):
        tag = self.LEVEL_MAP.get(record.levelname, record.levelname[:4])
        ts = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        return f"[{ts}] [{tag}]  {record.getMessage()}"

logger = logging.getLogger("openclaw_cli_mcp")
_level = {"debug":logging.DEBUG,"verbose":VERBOSE_LEVEL,"info":logging.INFO,"warn":logging.WARNING,"error":logging.ERROR}.get(LOG_LEVEL, VERBOSE_LEVEL)
logger.setLevel(_level)
try:
    _fh = logging.FileHandler(LOG_FILE); _fh.setFormatter(OpenClawFormatter()); logger.addHandler(_fh)
except PermissionError:
    pass
_sh = logging.StreamHandler(sys.stderr); _sh.setFormatter(OpenClawFormatter()); logger.addHandler(_sh)

def log(msg): logger.info(msg)
def log_verbose(msg): logger.log(VERBOSE_LEVEL, msg)
def log_debug(msg): logger.debug(msg)
def log_warn(msg): logger.warning(msg)
def log_error(msg): logger.error(msg)
def log_success(msg): logger.info(f"OK {msg}")
def log_step(msg): logger.info(f"=== {msg} ===")

MAX_SESSIONS = int(os.environ.get("OPENCLAW_MAX_SESSIONS", "20"))
OUTPUT_BUFFER_LINES = int(os.environ.get("OPENCLAW_OUTPUT_BUFFER", "5000"))
READ_TIMEOUT = float(os.environ.get("OPENCLAW_READ_TIMEOUT", "0.5"))
if IS_WINDOWS:
    DEFAULT_SHELL = os.environ.get("OPENCLAW_SHELL", "powershell.exe")
else:
    DEFAULT_SHELL = os.environ.get("OPENCLAW_SHELL", "/bin/bash")

class SessionState(str, Enum):
    RUNNING = "running"
    IDLE = "idle"
    DEAD = "dead"

class CLISession:
    def __init__(self, session_id, shell=DEFAULT_SHELL, working_dir="", env_vars=None, label="", rows=24, cols=120):
        self.session_id = session_id
        self.label = label or f"session-{session_id[:8]}"
        self.shell = shell
        self.working_dir = working_dir or str(Path.home())
        self.env_vars = env_vars or {}
        self.rows = rows; self.cols = cols
        self.created_at = datetime.now(timezone.utc)
        self.last_activity = self.created_at
        self.output_buffer = deque(maxlen=OUTPUT_BUFFER_LINES)
        self.state = SessionState.IDLE
        self.command_history = []
        self.plat = platform.system()
        self.master_fd = None; self.pid = None
        self._proc = None; self._reader_task = None; self._pending_output = asyncio.Queue()
        log_verbose(f"CLISession created: id={session_id}, label={self.label}, shell={shell}, cwd={self.working_dir}, platform={self.plat}")

    async def start(self):
        if IS_WINDOWS: return await self._start_windows()
        return self._start_unix()

    async def _start_windows(self):
        log_step(f"Starting Windows session: {self.label}")
        try:
            env = os.environ.copy(); env.update(self.env_vars); env["OPENCLAW_SESSION_ID"] = self.session_id
            sl = self.shell.lower()
            if "powershell" in sl or "pwsh" in sl:
                args = [self.shell, "-NoLogo", "-NoProfile", "-NoExit", "-Command", "-"]
            elif "cmd" in sl:
                args = [self.shell, "/Q", "/K"]
            else:
                args = [self.shell]
            log_verbose(f"Shell args: {args}")
            self._proc = await asyncio.create_subprocess_exec(*args, stdin=asyncio.subprocess.PIPE, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE, cwd=self.working_dir, env=env, creationflags=subprocess.CREATE_NEW_PROCESS_GROUP)
            self.pid = self._proc.pid; self.state = SessionState.RUNNING
            self._reader_task = asyncio.create_task(self._win_reader())
            log_success(f"Windows session started: pid={self.pid}")
            await asyncio.sleep(0.5); return True
        except Exception as e:
            log_error(f"Windows session failed: {e}"); self.state = SessionState.DEAD; return False

    async def _win_reader(self):
        log_verbose(f"Win reader started: {self.label}")
        try:
            while self._proc and self._proc.returncode is None:
                for stream, tag in [(self._proc.stdout, ""), (self._proc.stderr, "[stderr] ")]:
                    try:
                        data = await asyncio.wait_for(stream.read(65536), timeout=0.05)
                        if data:
                            decoded = data.decode("utf-8", errors="replace")
                            for line in decoded.splitlines(keepends=True): self.output_buffer.append(f"{tag}{line}")
                            await self._pending_output.put(decoded)
                            self.last_activity = datetime.now(timezone.utc)
                            log_debug(f"win read {tag}{len(data)}b")
                    except asyncio.TimeoutError: pass
                    except Exception: break
                await asyncio.sleep(0.01)
        except asyncio.CancelledError: pass
        except Exception as e: log_error(f"Win reader error: {e}")
        finally:
            if self._proc and self._proc.returncode is not None: self.state = SessionState.DEAD

    async def _win_read(self, timeout):
        chunks = []; deadline = time.time() + timeout
        while time.time() < deadline:
            try: chunks.append(self._pending_output.get_nowait())
            except asyncio.QueueEmpty: await asyncio.sleep(0.05)
        result = "".join(chunks)
        if result: log_verbose(f"Win output: {len(result)} chars")
        return result

    async def _win_write(self, data):
        if not self._proc or not self._proc.stdin: return False
        try:
            self._proc.stdin.write(data.encode("utf-8")); await self._proc.stdin.drain()
            self.last_activity = datetime.now(timezone.utc)
            log_verbose(f"Win write: {len(data)} chars: {repr(data[:100])}"); return True
        except Exception as e: log_error(f"Win write failed: {e}"); return False

    def _start_unix(self):
        log_step(f"Starting Unix PTY session: {self.label}")
        try:
            self.master_fd, slave_fd = pty.openpty()
            flags = fcntl.fcntl(self.master_fd, fcntl.F_GETFL)
            fcntl.fcntl(self.master_fd, fcntl.F_SETFL, flags | os.O_NONBLOCK)
            fcntl.ioctl(slave_fd, termios.TIOCSWINSZ, struct.pack("HHHH", self.rows, self.cols, 0, 0))
            env = os.environ.copy(); env.update(self.env_vars); env["TERM"] = "xterm-256color"; env["OPENCLAW_SESSION_ID"] = self.session_id
            pid = os.fork()
            if pid == 0:
                os.close(self.master_fd); os.setsid(); fcntl.ioctl(slave_fd, termios.TIOCSCTTY, 0)
                os.dup2(slave_fd, 0); os.dup2(slave_fd, 1); os.dup2(slave_fd, 2)
                if slave_fd > 2: os.close(slave_fd)
                os.chdir(self.working_dir); os.execvpe(self.shell, [self.shell, "--norc", "--noprofile", "-i"], env)
            else:
                os.close(slave_fd); self.pid = pid; self.state = SessionState.RUNNING
                log_success(f"Unix PTY started: pid={pid}"); return True
        except Exception as e: log_error(f"Unix PTY failed: {e}"); self.state = SessionState.DEAD; return False

    def _unix_read(self, timeout):
        if self.master_fd is None: return ""
        chunks = []; deadline = time.time() + timeout
        while time.time() < deadline:
            try:
                r, _, _ = select.select([self.master_fd], [], [], max(0.01, deadline - time.time()))
                if not r: break
                data = os.read(self.master_fd, 65536)
                if not data: break
                chunks.append(data)
            except (OSError, IOError): break
        if chunks:
            raw = b"".join(chunks); decoded = raw.decode("utf-8", errors="replace")
            for line in decoded.splitlines(keepends=True): self.output_buffer.append(line)
            self.last_activity = datetime.now(timezone.utc)
            log_verbose(f"Unix read: {len(raw)}b, buf={len(self.output_buffer)} lines"); return decoded
        return ""

    def _unix_write(self, data):
        if self.master_fd is None: return False
        try:
            os.write(self.master_fd, data.encode("utf-8")); self.last_activity = datetime.now(timezone.utc)
            log_verbose(f"Unix write: {len(data)} chars: {repr(data[:100])}"); return True
        except (OSError, IOError) as e: log_error(f"Unix write failed: {e}"); return False

    async def read_output(self, timeout=READ_TIMEOUT):
        if IS_WINDOWS: return await self._win_read(timeout)
        return self._unix_read(timeout)

    async def write_input(self, data):
        if IS_WINDOWS: return await self._win_write(data)
        return self._unix_write(data)

    def is_alive(self):
        if IS_WINDOWS:
            if not self._proc: return False
            if self._proc.returncode is not None: self.state = SessionState.DEAD; return False
            return True
        else:
            if self.pid is None: return False
            try:
                pid, _ = os.waitpid(self.pid, os.WNOHANG)
                if pid == 0: return True
                self.state = SessionState.DEAD; return False
            except ChildProcessError: self.state = SessionState.DEAD; return False

    async def send_signal_to_proc(self, sig_name):
        if IS_WINDOWS:
            if not self._proc: return False
            try:
                if sig_name in ("SIGINT","CTRL_C","CTRL_BREAK"): self._proc.send_signal(signal.CTRL_BREAK_EVENT)
                else: self._proc.terminate()
                return True
            except Exception as e: log_error(f"Win signal failed: {e}"); return False
        else:
            if self.pid is None: return False
            try:
                sig_num = getattr(signal, sig_name, None)
                if sig_num is None: return False
                os.kill(self.pid, sig_num); return True
            except ProcessLookupError: self.state = SessionState.DEAD; return False

    def resize(self, rows, cols):
        self.rows, self.cols = rows, cols
        if IS_WINDOWS: return True
        if self.master_fd is None: return False
        try:
            fcntl.ioctl(self.master_fd, termios.TIOCSWINSZ, struct.pack("HHHH", rows, cols, 0, 0))
            if self.pid: os.kill(self.pid, signal.SIGWINCH)
            return True
        except (OSError, IOError): return False

    async def kill(self):
        log_step(f"Killing: {self.label}")
        if IS_WINDOWS:
            if self._reader_task:
                self._reader_task.cancel()
                try: await self._reader_task
                except asyncio.CancelledError: pass
            if self._proc:
                try:
                    self._proc.terminate()
                    try: await asyncio.wait_for(self._proc.wait(), timeout=3)
                    except asyncio.TimeoutError: self._proc.kill()
                except Exception as e: log_error(f"Win kill error: {e}")
        else:
            if self.pid:
                try:
                    os.kill(self.pid, signal.SIGTERM); await asyncio.sleep(0.2)
                    try: os.kill(self.pid, signal.SIGKILL)
                    except ProcessLookupError: pass
                    try: os.waitpid(self.pid, os.WNOHANG)
                    except ChildProcessError: pass
                except ProcessLookupError: pass
            if self.master_fd is not None:
                try: os.close(self.master_fd)
                except OSError: pass
                self.master_fd = None
        self.state = SessionState.DEAD; log_success(f"Session {self.label} terminated"); return True

    def get_info(self):
        alive = self.is_alive()
        return {"session_id":self.session_id,"label":self.label,"pid":self.pid,
                "state":SessionState.RUNNING.value if alive else SessionState.DEAD.value,
                "shell":self.shell,"working_dir":self.working_dir,"platform":self.plat,
                "created_at":self.created_at.isoformat(),"last_activity":self.last_activity.isoformat(),
                "output_buffer_lines":len(self.output_buffer),"commands_run":len(self.command_history),
                "terminal_size":f"{self.cols}x{self.rows}"}

sessions = {}

def _get_session(session_id):
    if session_id not in sessions:
        raise ValueError(f"Session '{session_id}' not found. Available: {list(sessions.keys())}")
    return sessions[session_id]

@asynccontextmanager
async def app_lifespan():
    log_step("OpenClaw CLI MCP Server starting")
    log(f"Platform: {platform.system()} {platform.release()} ({platform.machine()})")
    log(f"Python: {platform.python_version()}")
    log(f"Shell: {DEFAULT_SHELL} | Max sessions: {MAX_SESSIONS} | Buffer: {OUTPUT_BUFFER_LINES}")
    log(f"Log: {LOG_FILE} | Level: {LOG_LEVEL}")
    if IS_WINDOWS: log(f"Mode: Windows subprocess (winpty={'yes' if HAS_WINPTY else 'no'})")
    else: log("Mode: Unix PTY (pty.openpty + fork)")
    yield {"sessions": sessions}
    log_step("Shutting down")
    for sess in list(sessions.values()): await sess.kill()
    sessions.clear(); log_success("Shutdown complete")

mcp = FastMCP("openclaw_cli_mcp", lifespan=app_lifespan)

class StartSessionInput(BaseModel):
    model_config = ConfigDict(str_strip_whitespace=True, extra="forbid")
    label: str = Field(default="", max_length=100)
    shell: str = Field(default=DEFAULT_SHELL)
    working_dir: str = Field(default="")
    env_vars: Optional[Dict[str, str]] = Field(default=None)
    rows: int = Field(default=24, ge=10, le=500)
    cols: int = Field(default=120, ge=40, le=500)

class ExecuteCommandInput(BaseModel):
    model_config = ConfigDict(str_strip_whitespace=True, extra="forbid")
    session_id: str = Field(..., min_length=1)
    command: str = Field(..., min_length=1, max_length=10000)
    timeout: float = Field(default=30.0, ge=0.1, le=600.0)
    wait_for: Optional[str] = Field(default=None, max_length=200)

class SendInputData(BaseModel):
    model_config = ConfigDict(str_strip_whitespace=False, extra="forbid")
    session_id: str = Field(..., min_length=1)
    data: str = Field(..., max_length=50000)
    read_timeout: float = Field(default=2.0, ge=0.0, le=120.0)

class ReadOutputInput(BaseModel):
    model_config = ConfigDict(str_strip_whitespace=True, extra="forbid")
    session_id: str = Field(..., min_length=1)
    lines: int = Field(default=50, ge=1, le=5000)
    timeout: float = Field(default=0.5, ge=0.0, le=30.0)
    wait_for: Optional[str] = Field(default=None, max_length=200)
    wait_timeout: float = Field(default=10.0, ge=0.1, le=300.0)

class SendSignalInput(BaseModel):
    model_config = ConfigDict(str_strip_whitespace=True, extra="forbid")
    session_id: str = Field(..., min_length=1)
    signal_name: str = Field(default="SIGINT")
    @field_validator("signal_name")
    @classmethod
    def validate_signal(cls, v):
        allowed = {"SIGINT","SIGTERM","SIGKILL","SIGTSTP","SIGCONT","SIGHUP","SIGUSR1","SIGUSR2","CTRL_C","CTRL_BREAK"}
        v = v.upper()
        if v not in allowed: raise ValueError(f"Must be one of: {sorted(allowed)}")
        return v

class ResizeInput(BaseModel):
    model_config = ConfigDict(extra="forbid")
    session_id: str = Field(..., min_length=1)
    rows: int = Field(..., ge=10, le=500)
    cols: int = Field(..., ge=40, le=500)

class SessionIdInput(BaseModel):
    model_config = ConfigDict(str_strip_whitespace=True, extra="forbid")
    session_id: str = Field(..., min_length=1)

class SendPromptInput(BaseModel):
    model_config = ConfigDict(str_strip_whitespace=False, extra="forbid")
    session_id: str = Field(..., min_length=1)
    prompt: str = Field(..., min_length=1, max_length=50000)
    wait_for_completion: bool = Field(default=True)
    completion_marker: str = Field(default=">>> ", max_length=100)
    timeout: float = Field(default=120.0, ge=1.0, le=600.0)

class OneOffCommandInput(BaseModel):
    model_config = ConfigDict(str_strip_whitespace=True, extra="forbid")
    command: str = Field(..., min_length=1, max_length=10000)
    working_dir: str = Field(default="")
    timeout: float = Field(default=60.0, ge=0.5, le=600.0)
    env_vars: Optional[Dict[str, str]] = Field(default=None)

@mcp.tool(name="openclaw_start_session", annotations={"title":"Start CLI Session","readOnlyHint":False,"destructiveHint":False,"idempotentHint":False,"openWorldHint":True})
async def start_session(params: StartSessionInput) -> str:
    """Start a new persistent terminal session. PTY on Linux, subprocess on Windows."""
    if len(sessions) >= MAX_SESSIONS: return json.dumps({"error": f"Max {MAX_SESSIONS} sessions reached."})
    sid = str(uuid.uuid4())
    s = CLISession(sid, params.shell, params.working_dir, params.env_vars or {}, params.label, params.rows, params.cols)
    if not await s.start(): return json.dumps({"error": "Failed to start. Check logs."})
    s.resize(params.rows, params.cols); await asyncio.sleep(0.5)
    init = await s.read_output(timeout=1.0); sessions[sid] = s
    log_success(f"Session ready: {sid} ({s.label})")
    return json.dumps({"status":"ok","session_id":sid,"label":s.label,"pid":s.pid,"shell":s.shell,"working_dir":s.working_dir,"platform":s.plat,"terminal_size":f"{params.cols}x{params.rows}","initial_output":init}, indent=2)

@mcp.tool(name="openclaw_execute_command", annotations={"title":"Execute Command","readOnlyHint":False,"destructiveHint":True,"idempotentHint":False,"openWorldHint":True})
async def execute_command(params: ExecuteCommandInput) -> str:
    """Execute a command in a session. Sends command+Enter, returns output."""
    try: s = _get_session(params.session_id)
    except ValueError as e: return json.dumps({"error": str(e)})
    if not s.is_alive(): return json.dumps({"error": f"Session {s.label} is dead."})
    await s.read_output(timeout=0.1)
    cmd = params.command if params.command.endswith("\n") else params.command + "\n"
    if not await s.write_input(cmd): return json.dumps({"error": "Write failed"})
    s.command_history.append({"command":params.command,"timestamp":datetime.now(timezone.utc).isoformat()})
    collected = []; t0 = time.time(); deadline = t0 + params.timeout
    while time.time() < deadline:
        chunk = await s.read_output(timeout=min(0.3, deadline-time.time()))
        if chunk:
            collected.append(chunk)
            if params.wait_for and params.wait_for in "".join(collected): break
        else:
            await asyncio.sleep(0.1)
            if not params.wait_for:
                q = await s.read_output(timeout=0.2)
                if q: collected.append(q)
                else: break
    out = "".join(collected); elapsed = round(time.time()-t0, 2)
    to = time.time()>=deadline and params.wait_for and params.wait_for not in out
    return json.dumps({"status":"timeout" if to else "ok","session_id":params.session_id,"command":params.command,"output":out,"output_length":len(out),"elapsed_seconds":elapsed,"timed_out":to,"session_alive":s.is_alive()}, indent=2)

@mcp.tool(name="openclaw_send_input", annotations={"title":"Send Raw Input","readOnlyHint":False,"destructiveHint":True,"idempotentHint":False,"openWorldHint":True})
async def send_input(params: SendInputData) -> str:
    """Send raw input to stdin (no auto-newline)."""
    try: s = _get_session(params.session_id)
    except ValueError as e: return json.dumps({"error": str(e)})
    if not s.is_alive(): return json.dumps({"error":"Dead session"})
    if not await s.write_input(params.data): return json.dumps({"error":"Write failed"})
    await asyncio.sleep(0.1); out = await s.read_output(timeout=params.read_timeout)
    return json.dumps({"status":"ok","bytes_sent":len(params.data.encode("utf-8")),"output":out,"output_length":len(out)}, indent=2)

@mcp.tool(name="openclaw_send_prompt", annotations={"title":"Send Prompt to AI","readOnlyHint":False,"destructiveHint":False,"idempotentHint":False,"openWorldHint":True})
async def send_prompt(params: SendPromptInput) -> str:
    """Send a prompt to an AI in a CLI session (Ollama, llama.cpp, etc). Waits for completion_marker."""
    try: s = _get_session(params.session_id)
    except ValueError as e: return json.dumps({"error": str(e)})
    if not s.is_alive(): return json.dumps({"error":"Dead session"})
    await s.read_output(timeout=0.2)
    p = params.prompt if params.prompt.endswith("\n") else params.prompt + "\n"
    if not await s.write_input(p): return json.dumps({"error":"Write failed"})
    chunks = []; t0 = time.time(); deadline = t0 + params.timeout; found = False
    while time.time() < deadline:
        c = await s.read_output(timeout=min(1.0, deadline-time.time()))
        if c:
            chunks.append(c)
            if params.wait_for_completion and params.completion_marker in "".join(chunks): found = True; break
        else:
            if not params.wait_for_completion:
                await asyncio.sleep(0.2); f = await s.read_output(timeout=0.3)
                if f: chunks.append(f)
                break
            await asyncio.sleep(0.2)
    full = "".join(chunks); elapsed = round(time.time()-t0, 2)
    ai = full
    if params.prompt in ai: ai = ai[ai.index(params.prompt)+len(params.prompt):]
    mk = params.completion_marker.rstrip()
    if mk and ai.rstrip().endswith(mk): ai = ai.rstrip(); ai = ai[:ai.rfind(mk)]
    return json.dumps({"status":"timeout" if (params.wait_for_completion and not found) else "ok","session_id":params.session_id,"prompt":params.prompt,"ai_response":ai.strip(),"raw_output":full,"elapsed_seconds":elapsed,"completion_marker_found":found}, indent=2)

@mcp.tool(name="openclaw_read_output", annotations={"title":"Read Output","readOnlyHint":True,"destructiveHint":False,"idempotentHint":True,"openWorldHint":False})
async def read_output(params: ReadOutputInput) -> str:
    """Read recent output from a session."""
    try: s = _get_session(params.session_id)
    except ValueError as e: return json.dumps({"error": str(e)})
    if params.wait_for:
        col = []; dl = time.time()+params.wait_timeout
        while time.time()<dl:
            c = await s.read_output(timeout=min(0.5, dl-time.time()))
            if c:
                col.append(c)
                if params.wait_for in "".join(col): break
            else: await asyncio.sleep(0.1)
    else: await s.read_output(timeout=params.timeout)
    recent = list(s.output_buffer)[-params.lines:]
    return json.dumps({"status":"ok","session_alive":s.is_alive(),"output":"".join(recent),"lines_returned":len(recent),"total_buffered":len(s.output_buffer)}, indent=2)

@mcp.tool(name="openclaw_send_signal", annotations={"title":"Send Signal","readOnlyHint":False,"destructiveHint":True,"idempotentHint":True,"openWorldHint":False})
async def send_signal(params: SendSignalInput) -> str:
    """Send signal to session process."""
    try: s = _get_session(params.session_id)
    except ValueError as e: return json.dumps({"error": str(e)})
    ok = await s.send_signal_to_proc(params.signal_name); await asyncio.sleep(0.3)
    out = await s.read_output(timeout=0.5)
    return json.dumps({"status":"ok" if ok else "failed","signal":params.signal_name,"output_after_signal":out,"session_alive":s.is_alive()}, indent=2)

@mcp.tool(name="openclaw_resize_terminal", annotations={"title":"Resize Terminal","readOnlyHint":False,"destructiveHint":False,"idempotentHint":True,"openWorldHint":False})
async def resize_terminal(params: ResizeInput) -> str:
    """Resize terminal."""
    try: s = _get_session(params.session_id)
    except ValueError as e: return json.dumps({"error": str(e)})
    s.resize(params.rows, params.cols)
    return json.dumps({"status":"ok","terminal_size":f"{params.cols}x{params.rows}"})

@mcp.tool(name="openclaw_list_sessions", annotations={"title":"List Sessions","readOnlyHint":True,"destructiveHint":False,"idempotentHint":True,"openWorldHint":False})
async def list_sessions() -> str:
    """List all sessions."""
    return json.dumps({"status":"ok","platform":platform.system(),"total":len(sessions),"max":MAX_SESSIONS,"sessions":[s.get_info() for s in sessions.values()]}, indent=2)

@mcp.tool(name="openclaw_kill_session", annotations={"title":"Kill Session","readOnlyHint":False,"destructiveHint":True,"idempotentHint":True,"openWorldHint":False})
async def kill_session(params: SessionIdInput) -> str:
    """Kill a session."""
    try: s = _get_session(params.session_id)
    except ValueError as e: return json.dumps({"error": str(e)})
    final = await s.read_output(timeout=0.2); await s.kill(); del sessions[params.session_id]
    return json.dumps({"status":"ok","label":s.label,"final_output":final,"commands_executed":len(s.command_history)}, indent=2)

@mcp.tool(name="openclaw_get_session_history", annotations={"title":"Session History","readOnlyHint":True,"destructiveHint":False,"idempotentHint":True,"openWorldHint":False})
async def get_session_history(params: SessionIdInput) -> str:
    """Get command history."""
    try: s = _get_session(params.session_id)
    except ValueError as e: return json.dumps({"error": str(e)})
    return json.dumps({"status":"ok","label":s.label,"commands":s.command_history,"total":len(s.command_history)}, indent=2)

@mcp.tool(name="openclaw_run_command", annotations={"title":"One-Off Command","readOnlyHint":False,"destructiveHint":True,"idempotentHint":False,"openWorldHint":True})
async def run_command(params: OneOffCommandInput) -> str:
    """Run a single command without persistent session."""
    log_step(f"One-off: {params.command[:100]}")
    env = os.environ.copy()
    if params.env_vars: env.update(params.env_vars)
    cwd = params.working_dir or str(Path.home()); t0 = time.time()
    try:
        if IS_WINDOWS:
            proc = await asyncio.create_subprocess_exec("powershell.exe","-NoProfile","-Command",params.command, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE, cwd=cwd, env=env)
        else:
            proc = await asyncio.create_subprocess_shell(params.command, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE, cwd=cwd, env=env)
        so, se = await asyncio.wait_for(proc.communicate(), timeout=params.timeout)
        return json.dumps({"status":"ok","command":params.command,"return_code":proc.returncode,"stdout":so.decode("utf-8",errors="replace") if so else "","stderr":se.decode("utf-8",errors="replace") if se else "","elapsed":round(time.time()-t0,2)}, indent=2)
    except asyncio.TimeoutError: return json.dumps({"status":"timeout","error":f"Timed out after {params.timeout}s"}, indent=2)
    except Exception as e: return json.dumps({"status":"error","error":str(e)}, indent=2)

if __name__ == "__main__":
    import argparse
    parser = argparse.ArgumentParser(description="OpenClaw CLI Control MCP Server")
    parser.add_argument("--transport", choices=["stdio","streamable_http"], default="stdio")
    parser.add_argument("--port", type=int, default=8462)
    parser.add_argument("--host", default="127.0.0.1")
    args = parser.parse_args()
    log_step("OpenClaw CLI MCP starting")
    log(f"Platform: {platform.system()} | Transport: {args.transport}")
    if args.transport == "streamable_http":
        log(f"HTTP: {args.host}:{args.port}"); mcp.run(transport="streamable_http", host=args.host, port=args.port)
    else: mcp.run()
