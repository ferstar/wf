"""phase / log / agent / parallel / pipeline。"""

from __future__ import annotations

import asyncio
import json
import subprocess
import sys
import uuid
from pathlib import Path
from typing import Any, Awaitable, Callable

from wf import jsonio
from wf.engine import external_busy_count, kill_pid_tree, kill_window, pane_pid, run_in_tmux
from wf.state import PhaseRec, get_state

_LOG_LINE = 2000


class _Gate:
    """全局槽位：本进程已占用的加上 tmux 里别人的非占位窗口。上限是 state.slots。"""

    def __init__(self, state: Any):
        self.state = state
        if self.state._slot_lock is None:
            self.state._slot_lock = asyncio.Lock()

    async def acquire(self, agent_id: str) -> None:
        ticket: asyncio.Future[bool] = asyncio.get_running_loop().create_future()
        async with self.state._slot_lock:
            self.state._wait_q.append(ticket)
        emitted = False
        try:
            while True:
                async with self.state._slot_lock:
                    head = bool(self.state._wait_q) and self.state._wait_q[0] is ticket
                    external = external_busy_count(self.state.tmux_session, self.state.windows)
                    if head and external + self.state.active < self.state.slots:
                        self.state._wait_q.pop(0)
                        self.state.active += 1
                        return
                if not emitted:
                    self.state.emit(
                        "agent_queued",
                        {"agent": agent_id, "external": external, "active": self.state.active},
                    )
                    emitted = True
                await asyncio.sleep(0.15)
        except asyncio.CancelledError:
            async with self.state._slot_lock:
                if ticket in self.state._wait_q:
                    self.state._wait_q.remove(ticket)
            raise

    def release(self) -> None:
        self.state.active = max(0, self.state.active - 1)


class Turn:
    def __init__(self, agent: "Agent", prompt: str, schema: dict[str, Any]):
        self.agent = agent
        self.prompt = prompt
        self.schema = schema
        self.done = False
        self._fut: asyncio.Future[Any] | None = None
        get_state().turns.append(self)

    def __await__(self):
        if self._fut is None:
            loop = asyncio.get_running_loop()
            self._fut = loop.create_future()
            asyncio.get_running_loop().create_task(self._drive())
        return self._fut.__await__()

    async def _drive(self) -> None:
        assert self._fut is not None
        try:
            value = await self.agent._run_turn(self.prompt, self.schema)
            self.done = True
            if not self._fut.done():
                self._fut.set_result(value)
        except Exception as exc:
            self.done = True
            if not self._fut.done():
                self._fut.set_exception(exc)


class Agent:
    def __init__(
        self,
        prompt: str | None = None,
        schema: Any = None,
        role: str | None = None,
        engine: str = "pi",
        workspace: str = "inherit",
        cwd: str | None = None,
        timeout: float | None = None,
        tools: str = "none",
        keep: bool = False,
        keep_on_success: bool = False,
    ):
        if workspace not in ("inherit", "branch"):
            raise ValueError("workspace 只允许 inherit 或 branch（v1 不做 share）")
        if engine not in ("pi", "grok", "claude", "codex"):
            raise ValueError("engine 只允许 pi、grok、claude 或 codex")
        if tools not in ("none", "read", "full"):
            raise ValueError("tools 只允许 none、read 或 full")
        if tools == "full" and workspace != "branch":
            raise ValueError("tools=full 仅允许 workspace=branch")
        self.standing = prompt
        self.tools = tools
        self.keep = bool(keep)
        self.keep_on_success = bool(keep_on_success)
        self.schema = jsonio.parse_schema(schema) if schema not in (None, "") else None
        if schema == "":
            raise ValueError("schema 必填（dict 或 JSON 字符串）")
        self.role = role
        self.engine = engine
        self.workspace = workspace
        self.cwd = cwd
        self.timeout = timeout
        self.mode: str | None = None
        self.session_id = str(uuid.uuid4())
        st = get_state()
        self.agent_id = f"a{st.next_agent:03d}"
        st.next_agent += 1
        self.turns_started = 0
        self.turn_no = 0
        self._lock = asyncio.Lock()
        self._once_result: Any = None
        self._once_done = False
        self._window: str | None = None
        self._worktree: Path | None = None
        self._branch: str | None = None
        self._repo: Path | None = None
        st.agents.append(self)

    def __await__(self):
        if self.mode == "multi":
            raise TypeError("这个 agent 已经按多轮调用，不能再一次性 await")
        self.mode = "once"
        if not self.standing or not str(self.standing).strip():
            raise ValueError("一次性 agent 的 prompt 不能为空")
        if self.schema is None:
            raise ValueError("schema 必填（dict 或 JSON 字符串）")
        return self._go_once().__await__()

    def __call__(self, prompt: str, schema: Any = None):
        if self.mode == "once":
            raise TypeError("这个 agent 已经一次性 await，不能再多轮调用")
        self.mode = "multi"
        use_schema = self.schema if schema is None else jsonio.parse_schema(schema)
        if use_schema is None:
            raise ValueError("schema 必填（dict 或 JSON 字符串）")
        if prompt is None or str(prompt).strip() == "":
            raise ValueError("prompt 不能为空")
        return Turn(self, str(prompt), use_schema)

    async def _go_once(self) -> Any:
        async with self._lock:
            if self._once_done:
                return self._once_result
            self._once_result = await self._execute(str(self.standing), self.schema, standing=None)
            self._once_done = True
            return self._once_result

    async def _run_turn(self, prompt: str, schema: dict[str, Any]) -> Any:
        async with self._lock:
            self.turns_started += 1
            return await self._execute(prompt, schema, standing=self.standing)

    def _resolve_cwd(self) -> Path:
        st = get_state()
        base = Path(self.cwd) if self.cwd else st.cwd
        if not base.is_absolute():
            base = (st.cwd / base).resolve()
        return base

    def _prepare_workspace(self, agent_dir: Path) -> Path:
        cwd = self._resolve_cwd()
        if self.workspace == "inherit":
            return cwd
        repo = cwd
        probe = subprocess.run(
            ["git", "-C", str(repo), "rev-parse", "--show-toplevel"],
            capture_output=True,
            text=True,
        )
        if probe.returncode != 0:
            raise RuntimeError(f"workspace=branch 需要 git 仓库，{repo} 不是")
        root = Path(probe.stdout.strip())
        self._repo = root
        st = get_state()
        run_id = st.run_dir.name
        branch = f"wf/{run_id}/{self.agent_id}"
        dest = st.run_dir / "worktrees" / self.agent_id
        dest.parent.mkdir(parents=True, exist_ok=True)
        exists = subprocess.run(
            ["git", "-C", str(root), "show-ref", "--verify", "--quiet", f"refs/heads/{branch}"],
        )
        if exists.returncode == 0:
            raise RuntimeError(f"分支已存在，不复用: {branch}")
        add = subprocess.run(
            ["git", "-C", str(root), "worktree", "add", "-b", branch, str(dest), "HEAD"],
            capture_output=True,
            text=True,
        )
        if add.returncode != 0:
            raise RuntimeError(f"git worktree add 失败: {add.stderr.strip()}")
        self._worktree = dest
        self._branch = branch
        return dest

    def _mark_meta(self, **extra: Any) -> None:
        st = get_state()
        path = st.run_dir / "agents" / self.agent_id / "meta.json"
        data: dict[str, Any] = {}
        if path.exists():
            try:
                data = json.loads(path.read_text(encoding="utf-8"))
            except json.JSONDecodeError:
                data = {}
        data.update(extra)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(data, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")

    def _cleanup_workspace(self, ok: bool) -> None:
        if self.workspace != "branch" or self._worktree is None or self._repo is None:
            return
        st = get_state()
        permanent = self.keep or (ok and self.keep_on_success)
        retain = permanent or not ok
        self._mark_meta(keep=permanent, repo=str(self._repo), worktree=str(self._worktree), branch=self._branch)
        if retain:
            st.kept_worktrees.append(str(self._worktree))
            return
        subprocess.run(
            ["git", "-C", str(self._repo), "worktree", "remove", "--force", str(self._worktree)],
            capture_output=True,
            text=True,
        )
        if self._branch:
            subprocess.run(
                ["git", "-C", str(self._repo), "branch", "-D", self._branch],
                capture_output=True,
                text=True,
            )

    async def _execute(self, user_prompt: str, schema: dict[str, Any] | None, standing: str | None) -> Any:
        if schema is None:
            raise ValueError("schema 必填（dict 或 JSON 字符串）")
        st = get_state()
        st.agent_runs += 1
        agent_dir = st.run_dir / "agents" / self.agent_id
        # 多轮共用一个目录下的 session，但每一轮有自己的 attempt 子目录输出。
        agent_dir.mkdir(parents=True, exist_ok=True)
        session_dir = agent_dir / "session"
        session_dir.mkdir(parents=True, exist_ok=True)
        work = self._prepare_workspace(agent_dir)
        label = self.role or (user_prompt.splitlines()[0][:60] if user_prompt else self.agent_id)
        st.current_phase().agents.append(self.agent_id)
        window = f"wf-{_safe(st.run_dir.name)}-{self.agent_id}"
        # tmux 窗口名过长时截断，仍以 run 前缀开头，方便 wf kill 扫描。
        if len(window) > 80:
            window = window[:80]
        self._window = window
        meta = {
            "id": self.agent_id,
            "role": self.role,
            "engine": self.engine,
            "workspace": self.workspace,
            "cwd": str(work),
            "window": window,
            "session_id": self.session_id,
            "phase": st.phase,
            "worktree": str(self._worktree) if self._worktree else None,
            "branch": self._branch,
            "tools": self.tools,
            "keep": self.keep,
            "repo": str(self._repo) if self._repo else None,
        }
        (agent_dir / "meta.json").write_text(json.dumps(meta, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        st.emit("agent_start", {"agent": self.agent_id, "role": self.role, "engine": self.engine, "window": window})
        gate = _Gate(st)
        last_error = ""
        ok = False
        self.turn_no += 1
        try:
            for attempt in (1, 2):
                attempt_dir = agent_dir / f"turn-{self.turn_no}" / f"attempt-{attempt}"
                attempt_dir.mkdir(parents=True, exist_ok=True)
                result_path = attempt_dir / "result.json"
                prompt = jsonio.build_prompt(
                    user_prompt,
                    schema,
                    standing=standing,
                    retry_error=last_error or None,
                    tools=self.tools,
                    result_path=str(result_path),
                )
                (attempt_dir / "prompt.txt").write_text(prompt, encoding="utf-8")
                (agent_dir / "prompt.txt").write_text(prompt, encoding="utf-8")
                # 多轮从第 2 轮起 resume；同一轮校验失败的第二次尝试也 resume。
                resume = (self.mode == "multi" and self.turns_started > 1) or attempt > 1
                from wf.engine import command_for

                cmd = command_for(
                    engine=self.engine,
                    prompt=prompt,
                    session_id=self.session_id,
                    session_dir=session_dir,
                    cwd=str(work),
                    resume=resume,
                    standing=standing if self.engine in ("pi", "claude") else None,
                    override=st.engine_bin,
                    tools=self.tools,
                )
                # 把本轮命令的 prompt 也留在 attempt 目录；run.sh 在 attempt 目录里生成会覆盖窗口。
                # 窗口名固定，重试前先杀掉上一轮已经退出的窗口（同名不能再建）。
                kill_window(st.tmux_session, window)
                await gate.acquire(self.agent_id)
                held = True
                st.emit(
                    "agent_status",
                    {"agent": self.agent_id, "status": "running", "attempt": attempt, "window": window},
                )
                try:
                    output = await run_in_tmux(
                        st,
                        window=window,
                        cwd=str(work),
                        cmd=cmd,
                        agent_dir=attempt_dir,
                        timeout=self.timeout,
                    )
                except asyncio.CancelledError:
                    st.emit("agent_cancel", {"agent": self.agent_id, "window": window})
                    pid = pane_pid(st.tmux_session, window)
                    kill_window(st.tmux_session, window)
                    if pid:
                        kill_pid_tree(pid)
                    raise
                finally:
                    if held:
                        gate.release()
                        held = False
                (agent_dir / "output.txt").write_text(output, encoding="utf-8")
                try:
                    value = jsonio.coerce_result(output, schema, result_path)
                except Exception as exc:
                    last_error = f"{type(exc).__name__}: {exc}\n--- output ---\n{output[-4000:]}"
                    st.emit(
                        "schema_fail",
                        {"agent": self.agent_id, "attempt": attempt, "error": str(exc)[:500]},
                    )
                    if attempt == 2:
                        st.agent_fail += 1
                        st.emit("agent_finish", {"agent": self.agent_id, "ok": False})
                        raise RuntimeError(
                            f"agent {self.agent_id} ({label}) 校验失败，已重试一次: {exc}"
                        ) from exc
                    st.emit("agent_status", {"agent": self.agent_id, "status": "retry", "attempt": attempt})
                    continue
                (agent_dir / "result.json").write_text(
                    json.dumps(value, ensure_ascii=False, indent=2) + "\n",
                    encoding="utf-8",
                )
                (attempt_dir / "result.json").write_text(
                    json.dumps(value, ensure_ascii=False, indent=2) + "\n",
                    encoding="utf-8",
                )
                st.agent_ok += 1
                st.emit("agent_finish", {"agent": self.agent_id, "ok": True, "attempt": attempt})
                ok = True
                return value
        finally:
            self._cleanup_workspace(ok)
        raise RuntimeError(f"agent {self.agent_id} 未产生结果")


def _safe(text: str) -> str:
    out = []
    for ch in text:
        if ch.isalnum() or ch in "-_":
            out.append(ch)
        else:
            out.append("-")
    return "".join(out)


def phase(name: str) -> str:
    st = get_state()
    st.phase = str(name)
    st.phases.append(PhaseRec(name=st.phase))
    st.emit("phase", {"name": st.phase})
    return name


def log(*args: Any) -> None:
    if len(args) < 1:
        raise TypeError("log() 至少需要一个参数")
    message = " ".join(str(a) for a in args)
    st = get_state()
    if len(message) > _LOG_LINE:
        st.log_seq += 1
        folder = st.run_dir / "logs"
        folder.mkdir(parents=True, exist_ok=True)
        path = folder / f"log-{st.log_seq:04d}.txt"
        path.write_text(message, encoding="utf-8")
        message = message[:180].replace("\n", " ") + f" … len={len(message)} file={path}"
    st.current_phase().logs.append(message)
    st.emit("log", {"message": message})
    print(message, file=sys.stderr, flush=True)


def agent(
    prompt: str | None = None,
    schema: Any = None,
    role: str | None = None,
    engine: str = "pi",
    workspace: str = "inherit",
    cwd: str | None = None,
    timeout: float | None = None,
    tools: str = "none",
    keep: bool = False,
    keep_on_success: bool = False,
) -> Agent:
    return Agent(
        prompt=prompt,
        schema=schema,
        role=role,
        engine=engine,
        workspace=workspace,
        cwd=cwd,
        timeout=timeout,
        tools=tools,
        keep=keep,
        keep_on_success=keep_on_success,
    )


async def parallel(items: Any, fn: Callable[[Any], Awaitable[Any]]) -> list[Any]:
    seq = list(items)
    awaitables = []
    for item in seq:
        aw = fn(item)
        if not asyncio.iscoroutine(aw) and not hasattr(aw, "__await__"):
            raise TypeError("parallel 的 fn 必须返回 awaitable")
        awaitables.append(aw)
    tasks = [asyncio.ensure_future(aw) for aw in awaitables]

    async def _finish(exc: BaseException) -> None:
        for task in tasks:
            if not task.done():
                task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
        raise exc

    try:
        return list(await asyncio.gather(*tasks))
    except Exception as exc:
        await _finish(exc)
        raise


async def pipeline(items: Any, fn: Callable[[Any], Awaitable[Any]]) -> list[Any]:
    results = []
    for item in list(items):
        aw = fn(item)
        if not asyncio.iscoroutine(aw) and not hasattr(aw, "__await__"):
            raise TypeError("pipeline 的 fn 必须返回 awaitable")
        results.append(await aw)
    return results


def check_unused() -> None:
    st = get_state()
    for ag in st.agents:
        if ag.standing and str(ag.standing).strip() and ag.mode is None:
            raise RuntimeError(
                f"agent {ag.agent_id} 带有 prompt 但从未 await 也从未调用"
            )
    for turn in st.turns:
        if not turn.done:
            raise RuntimeError(f"agent {turn.agent.agent_id} 有一轮调用从未 await")
