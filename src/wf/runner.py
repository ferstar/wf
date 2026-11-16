"""读脚本、注入上下文、顶层 await，并写 run 目录。"""

from __future__ import annotations

import ast
import asyncio
import inspect
import json
import os
import sys
import traceback
from datetime import datetime
from pathlib import Path
from time import monotonic
from typing import Any
from zoneinfo import ZoneInfo

import wf
from wf.config import DEFAULT_MAX_CONCURRENT, DEFAULT_TMUX_SESSION
from wf.primitives import check_unused
from wf.state import CURRENT, RunState

RUNS_ROOT = Path(os.environ.get("WF_RUNS_ROOT") or (Path.home() / ".cache" / "wf" / "runs"))
TZ = ZoneInfo("Asia/Shanghai")


def shanghai_stamp() -> str:
    return datetime.now(TZ).strftime("%Y%m%d-%H%M%S")


def allocate_run_dir(name: str) -> Path:
    RUNS_ROOT.mkdir(parents=True, exist_ok=True)
    stamp = shanghai_stamp()
    candidate = RUNS_ROOT / f"{name}-{stamp}"
    n = 2
    while candidate.exists():
        candidate = RUNS_ROOT / f"{name}-{stamp}-{n}"
        n += 1
    candidate.mkdir(parents=True)
    return candidate


def _transform(source: str, filename: str) -> ast.AST:
    tree = ast.parse(source, filename=filename)
    if tree.body and isinstance(tree.body[-1], ast.Expr):
        expr = tree.body[-1].value
        tree.body[-1] = ast.Expr(
            value=ast.Call(
                func=ast.Name(id="print", ctx=ast.Load()),
                args=[
                    ast.Call(
                        func=ast.Name(id="repr", ctx=ast.Load()),
                        args=[expr],
                        keywords=[],
                    )
                ],
                keywords=[],
            )
        )
    ast.fix_missing_locations(tree)
    return tree


async def _exec_script(path: Path, run_dir: Path) -> str:
    source = path.read_text(encoding="utf-8")
    tree = _transform(source, str(path))
    code = compile(tree, str(path), "exec", flags=ast.PyCF_ALLOW_TOP_LEVEL_AWAIT)
    ns: dict[str, Any] = {
        "__name__": "__wf_script__",
        "__file__": str(path),
        "phase": wf.phase,
        "log": wf.log,
        "agent": wf.agent,
        "parallel": wf.parallel,
        "pipeline": wf.pipeline,
        "args": wf.args,
    }
    import io
    from contextlib import redirect_stdout

    buf = io.StringIO()
    with redirect_stdout(buf):
        if code.co_flags & inspect.CO_COROUTINE:
            await eval(code, ns)
        else:
            exec(code, ns)
    text = buf.getvalue()
    (run_dir / "output.txt").write_text(text, encoding="utf-8")
    return text


def _summary(state: RunState, *, ok: bool, elapsed: float, error: str | None) -> dict[str, Any]:
    agents_view = []
    agents_root = state.run_dir / "agents"
    if agents_root.exists():
        for folder in sorted(p for p in agents_root.iterdir() if p.is_dir()):
            result = folder / "result.json"
            meta_path = folder / "meta.json"
            meta = {}
            if meta_path.exists():
                try:
                    meta = json.loads(meta_path.read_text(encoding="utf-8"))
                except json.JSONDecodeError:
                    meta = {}
            verdict = None
            if result.exists():
                try:
                    body = json.loads(result.read_text(encoding="utf-8"))
                    if isinstance(body, dict):
                        verdict = body.get("verdict")
                except json.JSONDecodeError:
                    body = None
            agents_view.append(
                {
                    "id": folder.name,
                    "role": meta.get("role"),
                    "phase": meta.get("phase"),
                    "ok": result.exists(),
                    "verdict": verdict,
                    "result": str(result) if result.exists() else None,
                    "output": str(folder / "output.txt"),
                    "prompt": str(folder / "prompt.txt"),
                    "worktree": meta.get("worktree") if meta.get("worktree") in state.kept_worktrees or not result.exists() else None,
                }
            )
    phases = []
    for ph in state.phases:
        phases.append({"name": ph.name, "logs": ph.logs, "agents": ph.agents})
    return {
        "ok": ok,
        "status": "done" if ok else "failed",
        "elapsed_s": round(elapsed, 3),
        "agents": state.agent_runs,
        "succeeded": state.agent_ok,
        "failed": state.agent_fail,
        "phases": phases,
        "agents_detail": agents_view,
        "kept_worktrees": state.kept_worktrees,
        "error": error,
    }


async def execute(
    script: Path,
    *,
    run_dir: Path,
    cwd: Path,
    args: dict[str, str],
    engine_bin: str | None,
    max_concurrent: int = DEFAULT_MAX_CONCURRENT,
    tmux_session: str = DEFAULT_TMUX_SESSION,
) -> int:
    os.environ["PYTHONUNBUFFERED"] = "1"
    for stream in (sys.stdout, sys.stderr):
        reconfigure = getattr(stream, "reconfigure", None)
        if reconfigure is None:
            continue
        try:
            reconfigure(line_buffering=True)
        except (OSError, ValueError):
            pass
    script = script.resolve()
    (run_dir / "script.py").write_text(script.read_text(encoding="utf-8"), encoding="utf-8")
    (run_dir / "args.json").write_text(json.dumps(args, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    (run_dir / "events.jsonl").write_text("", encoding="utf-8")
    state = RunState(
        run_dir=run_dir,
        cwd=cwd.resolve(),
        args=dict(args),
        engine_bin=engine_bin,
        tmux_session=tmux_session,
        slots=max_concurrent,
    )
    try:
        os.setpgrp()
    except OSError:
        pass
    state.write_meta(
        pid=os.getpid(),
        pgid=os.getpgrp(),
        status="running",
        cwd=str(cwd.resolve()),
        script=str(script),
        name=run_dir.name,
    )
    token = CURRENT.set(state)
    wf.args.clear()
    wf.args.update(args)
    start = monotonic()
    code = 0
    error_text = None
    try:
        await _exec_script(script, run_dir)
        check_unused()
        status = "done"
    except Exception:
        code = 1
        status = "failed"
        error_text = traceback.format_exc()
        (run_dir / "error.txt").write_text(error_text, encoding="utf-8")
    finally:
        elapsed = monotonic() - start
        summary = _summary(state, ok=code == 0, elapsed=elapsed, error=error_text)
        summary["status"] = status
        (run_dir / "summary.json").write_text(
            json.dumps(summary, ensure_ascii=False, indent=2) + "\n",
            encoding="utf-8",
        )
        state.write_meta(status=status, elapsed_s=summary["elapsed_s"])
        CURRENT.reset(token)
        wf.args.clear()
    return code
