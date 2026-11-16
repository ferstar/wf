"""在 tmux 会话的新窗口里跑一次引擎，并把 stdout/stderr 落盘。"""

from __future__ import annotations

import asyncio
import os
import shlex
import signal
import subprocess
from pathlib import Path
from typing import Any

from wf.state import RunState


def _which_engine(name: str, override: str | None) -> str:
    if override:
        return override
    return name


PLACEHOLDER_WINDOWS = frozenset({"idle", "wf-keep"})


def list_session_windows(session: str) -> list[str]:
    """tmux 窗口名。测试可设 WF_FAKE_LIST_WINDOWS 指向一行一个名字的文件。"""
    fake = os.environ.get("WF_FAKE_LIST_WINDOWS")
    if fake:
        path = Path(fake)
        if not path.exists():
            return []
        if os.access(path, os.X_OK):
            proc = subprocess.run([str(path)], capture_output=True, text=True, check=False)
            text = proc.stdout
        else:
            text = path.read_text(encoding="utf-8")
        return [line.strip() for line in text.splitlines() if line.strip()]
    proc = subprocess.run(
        ["tmux", "list-windows", "-t", session, "-F", "#{window_name}"],
        capture_output=True,
        text=True,
    )
    if proc.returncode != 0:
        return []
    return [line.strip() for line in proc.stdout.splitlines() if line.strip()]


def external_busy_count(session: str, own_windows: list[str]) -> int:
    own = set(own_windows)
    count = 0
    for name in list_session_windows(session):
        if name in PLACEHOLDER_WINDOWS or name in own:
            continue
        count += 1
    return count


def command_for(
    *,
    engine: str,
    prompt: str,
    session_id: str,
    session_dir: Path,
    cwd: str,
    resume: bool,
    standing: str | None,
    override: str | None,
    tools: str = "none",
) -> list[str]:
    """拼 pi / grok / claude / codex / 假引擎命令。prompt 作为参数，由调用方写入 shell 时再引用。"""
    binary = _which_engine(engine, override)
    if override:
        cmd = [
            binary,
            "-p",
            prompt,
            "--engine",
            engine,
            "--session-id",
            session_id,
            "--session-dir",
            str(session_dir),
            "--cwd",
            cwd,
        ]
        if resume:
            cmd += ["--resume", session_id, "--session", session_id]
        return cmd
    if engine == "pi":
        # 真 session：固定 session-id + session-dir。续轮再加 --session 指向同一 id。
        cmd = [
            "pi",
            "-p",
            prompt,
            "--mode",
            "text",
            "--session-dir",
            str(session_dir),
            "--session-id",
            session_id,
            "--no-extensions",
            "--no-mcp",
            "--no-skills",
            "--thinking",
            "low",
        ]
        if tools == "none":
            cmd.append("--no-tools")
        elif tools == "read":
            cmd += ["--tools", "read,grep,find,ls,bash", "--exclude-tools", "edit,write"]
        elif tools == "full":
            pass
        else:
            raise ValueError(f"未知 tools: {tools}")
        if standing:
            cmd += ["--append-system-prompt", standing]
        # pi 不允许 --session-id 与 --session 同时出现。
        # 续轮只重复同一 --session-id 与 --session-dir，会话文件留在 run 目录里。
        return cmd
    if engine == "grok":
        # 第一轮 --session-id 指定新会话 UUID；之后 --resume 同一 id。
        # 不使用 --system-prompt-override，避免盖掉 grok 默认人设。常驻说明已写进 prompt。
        cmd = ["grok", "-p", prompt, "--cwd", cwd, "--output-format", "plain"]
        if tools == "none":
            cmd += [
                "--disallowed-tools",
                "read_file,grep,list_dir,run_terminal_cmd,search_replace,web_search,web_fetch,todo_write,task",
            ]
        elif tools == "read":
            # 能限制工具就不加 --always-approve。bash 仍可能写文件，限制写在 prompt 里。
            cmd += ["--tools", "read_file,grep,list_dir,run_terminal_cmd", "--no-subagents"]
        elif tools == "full":
            cmd += ["--always-approve"]
        else:
            raise ValueError(f"未知 tools: {tools}")
        if resume:
            cmd += ["--resume", session_id]
        else:
            cmd += ["--session-id", session_id]
        return cmd
    if engine == "claude":
        # 标志来自 Claude Code CLI 文档，不是本机 --help。
        # https://code.claude.com/docs/en/cli-reference
        cmd = ["claude", "-p", prompt]
        if tools == "none":
            cmd += ["--tools", "", "--permission-mode", "dontAsk"]
        elif tools == "read":
            cmd += [
                "--permission-mode",
                "plan",
                "--tools",
                "Read,Grep,Glob,LS,Bash",
                "--disallowedTools",
                "Edit,Write,MultiEdit,NotebookEdit",
            ]
        elif tools == "full":
            cmd += ["--permission-mode", "bypassPermissions"]
        else:
            raise ValueError(f"未知 tools: {tools}")
        if standing:
            cmd += ["--append-system-prompt", standing]
        if resume:
            cmd += ["--resume", session_id]
        else:
            cmd += ["--session-id", session_id]
        return cmd
    if engine == "codex":
        # 标志来自 Codex CLI 文档。没有真正的无工具模式；none 与 read 都是只读沙箱。
        # https://developers.openai.com/codex/cli/reference
        if tools == "none" or tools == "read":
            sandbox = "read-only"
        elif tools == "full":
            sandbox = "workspace-write"
        else:
            raise ValueError(f"未知 tools: {tools}")
        # 官方没有可预先指定的 session id。每轮新 exec；续轮不传 resume。
        return [
            "codex",
            "exec",
            "--sandbox",
            sandbox,
            "--ask-for-approval",
            "never",
            "--cd",
            cwd,
            prompt,
        ]
    raise ValueError(f"未知 engine: {engine}")


def ensure_tmux_session(name: str) -> None:
    probe = subprocess.run(
        ["tmux", "has-session", "-t", name],
        capture_output=True,
        text=True,
    )
    if probe.returncode == 0:
        return
    subprocess.run(
        ["tmux", "new-session", "-d", "-s", name, "-n", "wf-keep"],
        check=True,
        capture_output=True,
        text=True,
    )


def kill_window(session: str, window: str) -> None:
    subprocess.run(
        ["tmux", "kill-window", "-t", f"{session}:{window}"],
        capture_output=True,
        text=True,
    )


def _descendants(pid: int) -> list[int]:
    try:
        out = subprocess.run(
            ["ps", "-o", "pid=", "--ppid", str(pid)],
            capture_output=True,
            text=True,
            check=False,
        )
    except OSError:
        return []
    kids: list[int] = []
    for line in out.stdout.split():
        if line.isdigit():
            child = int(line)
            kids.append(child)
            kids.extend(_descendants(child))
    return kids


def kill_pid_tree(pid: int) -> None:
    if pid <= 0:
        return
    pids = _descendants(pid) + [pid]
    for sig in (signal.SIGTERM, signal.SIGKILL):
        for p in pids:
            try:
                os.kill(p, sig)
            except ProcessLookupError:
                pass
        if sig == signal.SIGTERM:
            # 给进程一点时间退出，再 SIGKILL。同步短睡，树通常很小。
            import time

            time.sleep(0.2)


def pane_pid(session: str, window: str) -> int | None:
    proc = subprocess.run(
        ["tmux", "list-panes", "-t", f"{session}:{window}", "-F", "#{pane_pid}"],
        capture_output=True,
        text=True,
    )
    if proc.returncode != 0:
        return None
    line = proc.stdout.strip().splitlines()
    if not line or not line[0].strip().isdigit():
        return None
    return int(line[0].strip())


def write_runner_script(path: Path, cmd: list[str], stdout_path: Path, stderr_path: Path, exit_path: Path) -> None:
    # 用 argv 数组执行，避免 prompt 里的引号把 shell 弄坏。
    # 非 tty 时 libc 对 stdout 全缓冲，JSON 会一直留在管道里。按行冲刷。
    quoted = "stdbuf -oL -eL " + " ".join(shlex.quote(c) for c in cmd)
    body = "\n".join(
        [
            "#!/bin/bash",
            "set +e",
            f"{quoted} > {shlex.quote(str(stdout_path))} 2> {shlex.quote(str(stderr_path))}",
            "ec=$?",
            f"echo $ec > {shlex.quote(str(exit_path))}",
            "exit $ec",
            "",
        ]
    )
    path.write_text(body, encoding="utf-8")
    path.chmod(0o755)


async def run_in_tmux(
    state: RunState,
    *,
    window: str,
    cwd: str,
    cmd: list[str],
    agent_dir: Path,
    timeout: float | None,
) -> str:
    """开窗口、等待 exitcode 文件。取消或超时会杀掉窗口和子进程。"""
    ensure_tmux_session(state.tmux_session)
    stdout_path = agent_dir / "stdout.txt"
    stderr_path = agent_dir / "stderr.txt"
    exit_path = agent_dir / "exitcode"
    output_path = agent_dir / "output.txt"
    if exit_path.exists():
        exit_path.unlink()
    script = agent_dir / "run.sh"
    write_runner_script(script, cmd, stdout_path, stderr_path, exit_path)
    if window not in state.windows:
        state.windows.append(window)
        state.write_meta()
    proc = await asyncio.create_subprocess_exec(
        "tmux",
        "new-window",
        "-d",
        "-t",
        state.tmux_session,
        "-n",
        window,
        "-c",
        cwd,
        str(script),
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    await proc.wait()
    if proc.returncode != 0:
        err = (await proc.stderr.read()).decode() if proc.stderr else ""
        raise RuntimeError(f"tmux new-window 失败: {err.strip()}")

    loop = asyncio.get_running_loop()
    start = loop.time()
    pid = pane_pid(state.tmux_session, window)
    stable = 0
    last_sig = None
    try:
        while not exit_path.exists():
            if timeout is not None and (loop.time() - start) > timeout:
                raise TimeoutError(f"agent 超过 {timeout}s")
            if pid is None:
                pid = pane_pid(state.tmux_session, window)
            out = stdout_path.read_text(encoding="utf-8", errors="replace") if stdout_path.exists() else ""
            err = stderr_path.read_text(encoding="utf-8", errors="replace") if stderr_path.exists() else ""
            sig = (len(out), len(err))
            stable = stable + 1 if sig == last_sig else 0
            last_sig = sig
            # pi -p 有时已经把 JSON 写完但不退出。输出停稳后能解析就收尾。
            if out.strip() and stable >= 8:
                from wf.jsonio import extract_json

                try:
                    extract_json(out)
                except ValueError:
                    pass
                else:
                    kill_window(state.tmux_session, window)
                    if pid:
                        kill_pid_tree(pid)
                    if not exit_path.exists():
                        exit_path.write_text("0\n", encoding="utf-8")
                    break
            # 引擎报错后挂住：当成这一次失败，让上层还有一次重试。
            if stable >= 12 and not out.strip() and (
                "stream ended" in err.lower() or "\nError:" in f"\n{err}" or err.startswith("Error:")
            ):
                kill_window(state.tmux_session, window)
                if pid:
                    kill_pid_tree(pid)
                if not exit_path.exists():
                    exit_path.write_text("1\n", encoding="utf-8")
                break
            await asyncio.sleep(0.15)
    except (asyncio.CancelledError, TimeoutError):
        kill_window(state.tmux_session, window)
        if pid:
            kill_pid_tree(pid)
        raise
    text_out = stdout_path.read_text(encoding="utf-8", errors="replace") if stdout_path.exists() else ""
    text_err = stderr_path.read_text(encoding="utf-8", errors="replace") if stderr_path.exists() else ""
    output_path.write_text(text_out + ("" if not text_err else "\n----- stderr -----\n" + text_err), encoding="utf-8")
    # 窗口留着会占并发槽。输出已经落盘，关掉窗口。
    kill_window(state.tmux_session, window)
    if pid:
        kill_pid_tree(pid)
    code = exit_path.read_text(encoding="utf-8").strip() if exit_path.exists() else ""
    if code not in ("0", ""):
        return output_path.read_text(encoding="utf-8")
    return output_path.read_text(encoding="utf-8")


def kill_recorded_windows(session: str, windows: list[str]) -> list[str]:
    killed: list[str] = []
    for window in windows:
        probe = subprocess.run(
            ["tmux", "list-windows", "-t", session, "-F", "#{window_name}"],
            capture_output=True,
            text=True,
        )
        names = set(probe.stdout.split())
        if window not in names:
            continue
        pid = pane_pid(session, window)
        kill_window(session, window)
        if pid:
            kill_pid_tree(pid)
        killed.append(window)
    return killed
