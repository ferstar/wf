"""共享 bare/mirror 缓存，以及从缓存拉出的 worktree。

缓存目录默认 `~/.cache/wf/mirrors/<host>/<path>.git`。
GitLab 用 `GITLAB_TOKEN` askpass，GitHub 用 `GH_TOKEN` / `GITHUB_TOKEN` 或 `gh auth token`。
认证或 clone/fetch 失败会打到 stderr，并在有运行上下文时写入 events.jsonl。
"""

from __future__ import annotations

import fcntl
import os
import subprocess
import sys
from pathlib import Path
from urllib.parse import urlparse

from wf.state import get_state

def cache_root() -> Path:
    return Path(os.environ.get("WF_CACHE_ROOT") or (Path.home() / ".cache" / "wf" / "mirrors"))


def cache_dir_for(url: str) -> Path:
    root = cache_root()
    parsed = urlparse(url)
    host = parsed.hostname or "local"
    path = (parsed.path or "").strip("/")
    if not path:
        raise ValueError(f"无法从 URL 得到仓库路径: {url}")
    if not path.endswith(".git"):
        path += ".git"
    return root / host / path


def _fail(message: str) -> None:
    print(message, file=sys.stderr, flush=True)
    try:
        state = get_state()
    except RuntimeError:
        state = None
    if state is not None:
        state.emit("clone_error", {"message": message[:4000]})
    raise RuntimeError(message)


def _run_git(cmd: list[str], env: dict[str, str]) -> subprocess.CompletedProcess[str]:
    try:
        return subprocess.run(cmd, capture_output=True, text=True, env=env, check=False)
    except OSError as exc:
        _fail(f"无法执行 git: {exc}")
        raise


def _github_token() -> str | None:
    token = os.environ.get("GH_TOKEN") or os.environ.get("GITHUB_TOKEN")
    if token:
        return token
    try:
        proc = subprocess.run(
            ["gh", "auth", "token"],
            capture_output=True,
            text=True,
            check=False,
        )
    except OSError:
        return None
    if proc.returncode == 0 and proc.stdout.strip():
        return proc.stdout.strip()
    return None


def _write_askpass(directory: Path, kind: str) -> Path:
    directory.mkdir(parents=True, exist_ok=True)
    path = directory / f"askpass-{kind}.sh"
    if kind == "gitlab":
        user = "oauth2"
        var = "GITLAB_TOKEN"
    else:
        user = "x-access-token"
        var = "GH_TOKEN"
    path.write_text(
        "#!/bin/sh\n"
        'case "$1" in\n'
        f"  *sername*) printf '%s\\n' {user} ;;\n"
        f"  *) printf '%s\\n' \"${var}\" ;;\n"
        "esac\n",
        encoding="utf-8",
    )
    path.chmod(0o700)
    return path


def git_env_for(url: str) -> dict[str, str]:
    env = os.environ.copy()
    env["GIT_TERMINAL_PROMPT"] = "0"
    parsed = urlparse(url)
    host = (parsed.hostname or "").lower()
    scheme = (parsed.scheme or "").lower()
    if scheme == "file" or host in {"", "localhost"}:
        return env
    ask_dir = cache_root() / "_askpass"
    if "gitlab" in host or (host and "github.com" not in host and env.get("GITLAB_TOKEN")):
        if env.get("GITLAB_TOKEN"):
            env["GIT_ASKPASS"] = str(_write_askpass(ask_dir, "gitlab"))
        return env
    if "github.com" in host or host.endswith(".github.com"):
        token = _github_token()
        if token:
            env["GH_TOKEN"] = token
            env["GIT_ASKPASS"] = str(_write_askpass(ask_dir, "github"))
        return env
    return env


def _ref_exists(cache: Path, branch: str, env: dict[str, str]) -> bool:
    proc = _run_git(
        ["git", "-C", str(cache), "rev-parse", "--verify", "--quiet", f"refs/heads/{branch}"],
        env,
    )
    return proc.returncode == 0


def ensure_checkout(url: str, dest: Path, *, branch: str = "main") -> Path:
    """镜像进缓存（已有则 fetch），再 `git worktree add` 到 dest。"""
    dest = Path(dest)
    cache = cache_dir_for(url)
    cache.parent.mkdir(parents=True, exist_ok=True)
    env = git_env_for(url)
    lock_path = cache_root() / "_locks" / (str(cache).replace("/", "_") + ".lock")
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    with lock_path.open("a", encoding="utf-8") as lock:
        fcntl.flock(lock.fileno(), fcntl.LOCK_EX)
        if not (cache / "HEAD").exists():
            cache.parent.mkdir(parents=True, exist_ok=True)
            proc = _run_git(["git", "clone", "--mirror", url, str(cache)], env)
            if proc.returncode != 0:
                err = (proc.stderr or proc.stdout or "").strip()
                _fail(f"git clone --mirror 失败 url={url} code={proc.returncode} stderr={err}")
        else:
            proc = _run_git(["git", "-C", str(cache), "fetch", "--prune"], env)
            if proc.returncode != 0:
                err = (proc.stderr or proc.stdout or "").strip()
                _fail(f"git fetch 失败 url={url} cache={cache} code={proc.returncode} stderr={err}")
        if dest.exists() and (dest / ".git").exists():
            return dest
        use = branch
        if not _ref_exists(cache, use, env):
            if branch == "main" and _ref_exists(cache, "master", env):
                use = "master"
            else:
                _fail(f"缓存里没有分支 {branch}: {cache}")
        dest.parent.mkdir(parents=True, exist_ok=True)
        add = _run_git(
            ["git", "-C", str(cache), "worktree", "add", "--detach", str(dest), use],
            env,
        )
        if add.returncode != 0:
            err = (add.stderr or add.stdout or "").strip()
            _fail(f"git worktree add 失败 url={url} dest={dest} code={add.returncode} stderr={err}")
    return dest
