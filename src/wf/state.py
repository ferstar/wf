"""当前一次 wf run 的可变状态。原语通过 contextvar 找到它。"""

from __future__ import annotations

import json
import threading
from contextvars import ContextVar
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any


@dataclass
class PhaseRec:
    name: str | None
    logs: list[str] = field(default_factory=list)
    agents: list[str] = field(default_factory=list)


@dataclass
class RunState:
    run_dir: Path
    cwd: Path
    args: dict[str, str]
    engine_bin: str | None
    tmux_session: str = "pi"
    phase: str | None = None
    phases: list[PhaseRec] = field(default_factory=list)
    agents: list[Any] = field(default_factory=list)
    turns: list[Any] = field(default_factory=list)
    next_agent: int = 1
    # 同时运行的引擎进程上限。waiters 按 FIFO 授予名额。默认 3。
    slots: int = 3
    active: int = 0
    waiters: list[Any] = field(default_factory=list)
    windows: list[str] = field(default_factory=list)
    kept_worktrees: list[str] = field(default_factory=list)
    agent_runs: int = 0
    agent_ok: int = 0
    agent_fail: int = 0
    log_seq: int = 0
    _lock: threading.Lock = field(default_factory=threading.Lock)
    _slot_lock: Any = None
    _wait_q: list[Any] = field(default_factory=list)

    def emit(self, kind: str, payload: dict[str, Any]) -> None:
        rec = {"type": kind, "phase": self.phase, **payload}
        line = json.dumps(rec, ensure_ascii=False)
        with self._lock:
            path = self.run_dir / "events.jsonl"
            with path.open("a", encoding="utf-8") as fh:
                fh.write(line + "\n")
            if kind == "log":
                with (self.run_dir / "logs.txt").open("a", encoding="utf-8") as fh:
                    fh.write(payload.get("message", "") + "\n")

    def current_phase(self) -> PhaseRec:
        if not self.phases:
            self.phases.append(PhaseRec(name=None))
        return self.phases[-1]

    def meta_path(self) -> Path:
        return self.run_dir / "meta.json"

    def write_meta(self, **extra: Any) -> None:
        path = self.meta_path()
        data: dict[str, Any] = {}
        if path.exists():
            try:
                data = json.loads(path.read_text(encoding="utf-8"))
            except json.JSONDecodeError:
                data = {}
        data.update(extra)
        data["windows"] = list(self.windows)
        data["tmux_session"] = self.tmux_session
        data["max_concurrent"] = self.slots
        path.write_text(json.dumps(data, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


CURRENT: ContextVar[RunState | None] = ContextVar("wf_run", default=None)


def get_state() -> RunState:
    st = CURRENT.get()
    if st is None:
        raise RuntimeError("wf 原语只能在 wf run 执行的脚本里使用")
    return st
