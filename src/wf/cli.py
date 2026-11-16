"""wf 命令行。"""

from __future__ import annotations

import argparse
import asyncio
import difflib
import hashlib
import json
import os
import sys
from pathlib import Path

from wf.config import DEFAULT_TMUX_SESSION, resolve_run_settings
from wf.engine import kill_recorded_windows
from wf.runner import RUNS_ROOT, allocate_run_dir, execute

APPROVED = RUNS_ROOT / "_approved"


def _parse_args_list(pairs: list[str]) -> dict[str, str]:
    out: dict[str, str] = {}
    for item in pairs:
        if "=" not in item:
            raise SystemExit(f"--args 需要 k=v，收到: {item}")
        key, value = item.split("=", 1)
        if not key:
            raise SystemExit(f"--args 的键不能为空: {item}")
        out[key] = value
    return out


def _preview(script: Path) -> str:
    text = script.read_text(encoding="utf-8")
    APPROVED.mkdir(parents=True, exist_ok=True)
    digest = hashlib.sha256(str(script.resolve()).encode()).hexdigest()[:16]
    prev = APPROVED / f"{digest}.py"
    if not prev.exists():
        return text
    old = prev.read_text(encoding="utf-8")
    if old == text:
        return "与上次批准的脚本相同\n"
    diff = "".join(
        difflib.unified_diff(
            old.splitlines(keepends=True),
            text.splitlines(keepends=True),
            fromfile="上次批准",
            tofile=str(script),
        )
    )
    return diff or "与上次批准的脚本相同\n"


def _remember(script: Path) -> None:
    APPROVED.mkdir(parents=True, exist_ok=True)
    digest = hashlib.sha256(str(script.resolve()).encode()).hexdigest()[:16]
    prev = APPROVED / f"{digest}.py"
    prev.write_text(script.read_text(encoding="utf-8"), encoding="utf-8")


def _resolve_run(token: str) -> Path:
    RUNS_ROOT.mkdir(parents=True, exist_ok=True)
    direct = RUNS_ROOT / token
    if direct.is_dir():
        return direct
    matches = sorted(p for p in RUNS_ROOT.iterdir() if p.is_dir() and token in p.name and p.name != "_approved")
    if len(matches) == 1:
        return matches[0]
    if not matches:
        raise SystemExit(f"找不到 run: {token}")
    raise SystemExit("匹配到多个 run:\n" + "\n".join(p.name for p in matches))


def _read_summary(run: Path) -> dict:
    path = run / "summary.json"
    if not path.exists():
        meta = run / "meta.json"
        if meta.exists():
            try:
                return json.loads(meta.read_text(encoding="utf-8"))
            except json.JSONDecodeError:
                return {}
        return {}
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError:
        return {}


def _line_buffer() -> None:
    os.environ["PYTHONUNBUFFERED"] = "1"
    for stream in (sys.stdout, sys.stderr):
        reconfigure = getattr(stream, "reconfigure", None)
        if reconfigure is None:
            continue
        try:
            reconfigure(line_buffering=True)
        except (OSError, ValueError):
            pass


def _load_meta(run: Path) -> dict:
    meta_path = run / "meta.json"
    if not meta_path.exists():
        return {}
    try:
        return json.loads(meta_path.read_text(encoding="utf-8"))
    except json.JSONDecodeError:
        return {}


def _remove_worktree(path: Path, repo: str | None, branch: str | None) -> None:
    import subprocess

    git_dir = repo
    if not git_dir and path.exists():
        proc = subprocess.run(
            ["git", "-C", str(path), "rev-parse", "--git-common-dir"],
            capture_output=True,
            text=True,
        )
        if proc.returncode == 0 and proc.stdout.strip():
            common = Path(proc.stdout.strip())
            if not common.is_absolute():
                common = (path / common).resolve()
            git_dir = str(common.parent if common.name == ".git" else common)
    if not git_dir:
        return
    subprocess.run(
        ["git", "-C", git_dir, "worktree", "remove", "--force", str(path)],
        capture_output=True,
        text=True,
    )
    if branch:
        subprocess.run(
            ["git", "-C", git_dir, "branch", "-D", branch],
            capture_output=True,
            text=True,
        )


def cleanup_run_worktrees(run: Path) -> list[str]:
    """删掉这次 run 里没有 keep=True 的 worktree。"""
    removed: list[str] = []
    seen: set[str] = set()
    agents = run / "agents"
    metas: list[tuple[Path, dict]] = []
    if agents.is_dir():
        for meta_path in sorted(agents.glob("*/meta.json")):
            try:
                metas.append((meta_path, json.loads(meta_path.read_text(encoding="utf-8"))))
            except json.JSONDecodeError:
                continue
    protected: set[str] = set()
    for _meta_path, meta in metas:
        wt = meta.get("worktree")
        if meta.get("keep") is True and wt:
            protected.add(str(wt))
    for _meta_path, meta in metas:
        wt = meta.get("worktree")
        if not wt or wt in seen:
            continue
        seen.add(wt)
        path = Path(wt)
        if not path.exists():
            continue
        if wt in protected:
            continue
        _remove_worktree(path, meta.get("repo"), meta.get("branch"))
        removed.append(wt)
    worktrees = run / "worktrees"
    if worktrees.is_dir():
        for path in sorted(p for p in worktrees.iterdir() if p.is_dir()):
            if str(path) in protected or str(path) in seen:
                continue
            _remove_worktree(path, None, None)
            if not path.exists():
                removed.append(str(path))
    return removed


def _finished(run: Path) -> bool:
    summary = _read_summary(run)
    meta = _load_meta(run)
    status = summary.get("status") or meta.get("status")
    return status in {"done", "failed", "killed"}


def cmd_run(ns: argparse.Namespace) -> int:
    _line_buffer()
    script = Path(ns.script).resolve()
    if not script.is_file():
        print(f"脚本不存在: {script}", file=sys.stderr)
        return 2
    if not ns.yes:
        if not sys.stdin.isatty():
            print("非交互环境拒绝运行。请加上 --yes。", file=sys.stderr)
            return 2
        sys.stdout.write(_preview(script))
        sys.stdout.write("\n运行该脚本？ [y/N] ")
        sys.stdout.flush()
        answer = sys.stdin.readline().strip().lower()
        if answer not in {"y", "yes"}:
            print("已取消")
            return 2
    name = ns.name or script.stem
    if "/" in name or name in {"", ".", ".."}:
        print("非法 --name", file=sys.stderr)
        return 2
    _remember(script)
    run_dir = allocate_run_dir(name)
    args = _parse_args_list(ns.args or [])
    engine_bin = os.environ.get("WF_ENGINE_BIN") or None
    try:
        max_concurrent, tmux_session = resolve_run_settings(
            cli_max=ns.max_concurrent,
            cli_session=ns.session,
        )
    except ValueError as exc:
        print(str(exc), file=sys.stderr)
        return 2
    code = asyncio.run(
        execute(
            script,
            run_dir=run_dir,
            cwd=Path.cwd(),
            args=args,
            engine_bin=engine_bin,
            max_concurrent=max_concurrent,
            tmux_session=tmux_session,
        )
    )
    print(f"WF_RUN_DIR={run_dir}")
    return code


def cmd_ls(_: argparse.Namespace) -> int:
    if not RUNS_ROOT.exists():
        return 0
    for path in sorted(p for p in RUNS_ROOT.iterdir() if p.is_dir() and p.name != "_approved"):
        summary = _read_summary(path)
        status = summary.get("status", "?")
        elapsed = summary.get("elapsed_s", "")
        print(f"{path.name}\t{status}\t{elapsed}")
    return 0


def cmd_status(ns: argparse.Namespace) -> int:
    run = _resolve_run(ns.run)
    summary = _read_summary(run)
    print(f"run: {run}")
    print(f"status: {summary.get('status', 'unknown')}")
    print(f"elapsed_s: {summary.get('elapsed_s', '')}")
    print(f"agents: {summary.get('agents', '')} succeeded={summary.get('succeeded', '')} failed={summary.get('failed', '')}")
    if summary.get("error"):
        first = str(summary["error"]).strip().splitlines()[-1]
        print(f"error: {first}")
    return 0


def cmd_show(ns: argparse.Namespace) -> int:
    run = _resolve_run(ns.run)
    summary = _read_summary(run)
    print(f"run: {run}")
    print(f"status: {summary.get('status')} elapsed_s={summary.get('elapsed_s')} agents={summary.get('agents')}")
    for phase in summary.get("phases") or []:
        print(f"phase: {phase.get('name')}")
        for line in phase.get("logs") or []:
            print(f"  log: {line}")
        for aid in phase.get("agents") or []:
            print(f"  agent: {aid}")
    for detail in summary.get("agents_detail") or []:
        print(
            f"result: {detail.get('id')} role={detail.get('role')} verdict={detail.get('verdict')} "
            f"path={detail.get('result')}"
        )
    for kept in summary.get("kept_worktrees") or []:
        print(f"kept_worktree: {kept}")
    return 0


def _kill_one(run: Path) -> list[str]:
    meta_path = run / "meta.json"
    meta = {}
    if meta_path.exists():
        try:
            meta = json.loads(meta_path.read_text(encoding="utf-8"))
        except json.JSONDecodeError:
            meta = {}
    session = meta.get("tmux_session") or DEFAULT_TMUX_SESSION
    windows = list(meta.get("windows") or [])
    # 再扫一遍 session，避免 meta 没跟上。
    import subprocess

    probe = subprocess.run(
        ["tmux", "list-windows", "-t", session, "-F", "#{window_name}"],
        capture_output=True,
        text=True,
    )
    prefix = f"wf-{run.name}"
    # 窗口名做过字符清洗，和 run 目录名一致（run 名只允许我们生成的安全字符）。
    safe = []
    for ch in run.name:
        safe.append(ch if ch.isalnum() or ch in "-_" else "-")
    prefix = "wf-" + "".join(safe)
    for name in probe.stdout.split():
        if name.startswith(prefix) and name not in windows:
            windows.append(name)
    killed = kill_recorded_windows(session, windows)
    pgid = meta.get("pgid")
    if isinstance(pgid, int) and pgid > 1 and pgid != os.getpgrp():
        try:
            os.killpg(pgid, 15)
            killed.append(f"pgid:{pgid}")
        except ProcessLookupError:
            pass
        except PermissionError:
            pass
    meta["status"] = "killed"
    meta["killed_windows"] = killed
    meta_path.write_text(json.dumps(meta, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    removed = cleanup_run_worktrees(run)
    if removed:
        killed.extend(f"worktree:{path}" for path in removed)
    summary_path = run / "summary.json"
    if summary_path.exists():
        try:
            summary = json.loads(summary_path.read_text(encoding="utf-8"))
        except json.JSONDecodeError:
            summary = {}
        if summary.get("status") == "running":
            summary["status"] = "killed"
            summary_path.write_text(json.dumps(summary, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    return killed


def cmd_kill(ns: argparse.Namespace) -> int:
    RUNS_ROOT.mkdir(parents=True, exist_ok=True)
    if ns.run == "all":
        targets = [p for p in RUNS_ROOT.iterdir() if p.is_dir() and p.name != "_approved"]
    else:
        targets = [_resolve_run(ns.run)]
    any_killed = False
    for run in targets:
        killed = _kill_one(run)
        if killed:
            any_killed = True
            print(f"{run.name}: killed {', '.join(killed)}")
        else:
            print(f"{run.name}: nothing alive")
    return 0 if True else 1


def cmd_gc(_: argparse.Namespace) -> int:
    """清理已结束或被 kill 的 run 上、没有 keep=True 的 worktree。"""
    if not RUNS_ROOT.exists():
        return 0
    for run in sorted(p for p in RUNS_ROOT.iterdir() if p.is_dir() and p.name != "_approved"):
        if not _finished(run):
            print(f"{run.name}: skip (still running)")
            continue
        removed = cleanup_run_worktrees(run)
        if removed:
            print(f"{run.name}: removed {', '.join(removed)}")
        else:
            print(f"{run.name}: nothing to clean")
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="wf", description="Run pi, grok, claude, or codex agents in tmux")
    sub = parser.add_subparsers(dest="cmd", required=True)

    run = sub.add_parser("run", help="运行脚本")
    run.add_argument("script")
    run.add_argument("--args", action="append", default=[])
    run.add_argument("--yes", action="store_true")
    run.add_argument("--name", default=None)
    run.add_argument(
        "--max-concurrent",
        default=None,
        help="同时运行的引擎窗口上限，>= 1。覆盖 WF_MAX_CONCURRENT 和配置文件",
    )
    run.add_argument(
        "--session",
        default=None,
        help="tmux 会话名（不是 pi 的 --session）。覆盖 WF_TMUX_SESSION 和配置文件",
    )
    run.set_defaults(func=cmd_run)

    ls = sub.add_parser("ls", help="列出 run")
    ls.set_defaults(func=cmd_ls)

    status = sub.add_parser("status", help="简短状态")
    status.add_argument("run")
    status.set_defaults(func=cmd_status)

    show = sub.add_parser("show", help="完整摘要")
    show.add_argument("run")
    show.set_defaults(func=cmd_show)

    kill = sub.add_parser("kill", help="杀掉 run 的 tmux 窗口")
    kill.add_argument("run", help="run 目录名，或 all")
    kill.set_defaults(func=cmd_kill)

    gc = sub.add_parser("gc", help="清理已结束 run 的未保留 worktree")
    gc.set_defaults(func=cmd_gc)
    return parser


def main() -> None:
    parser = build_parser()
    ns = parser.parse_args()
    try:
        code = ns.func(ns)
    except KeyboardInterrupt:
        code = 130
    raise SystemExit(code)
