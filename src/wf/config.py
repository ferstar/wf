"""并发槽和 tmux 会话名。优先级：CLI > 环境变量 > 配置文件 > 默认值。"""

from __future__ import annotations

import os
import tomllib
from pathlib import Path

DEFAULT_MAX_CONCURRENT = 3
DEFAULT_TMUX_SESSION = "pi"

_ENV_MAX = "WF_MAX_CONCURRENT"
_ENV_SESSION = "WF_TMUX_SESSION"
_ENV_CONFIG = "WF_CONFIG"


def default_config_path() -> Path:
    return Path.home() / ".config" / "wf" / "config.toml"


def config_file_path() -> Path | None:
    """WF_CONFIG 指向的文件；未设置时，默认路径存在才读。"""
    raw = os.environ.get(_ENV_CONFIG)
    if raw is not None:
        if raw.strip() == "":
            raise ValueError("WF_CONFIG 不能是空字符串")
        return Path(raw).expanduser()
    path = default_config_path()
    if path.is_file():
        return path
    return None


def _parse_max(raw: object, source: str) -> int:
    if isinstance(raw, bool) or not isinstance(raw, (int, str)):
        raise ValueError(f"{source} 的 max_concurrent 必须是 >= 1 的整数，收到 {raw!r}")
    if isinstance(raw, str):
        text = raw.strip()
        if not text.isdigit():
            raise ValueError(f"{source} 的 max_concurrent 必须是 >= 1 的整数，收到 {raw!r}")
        value = int(text)
    else:
        value = raw
    if value < 1:
        raise ValueError(f"{source} 的 max_concurrent 必须是 >= 1 的整数，收到 {value}")
    return value


def _parse_session(raw: object, source: str) -> str:
    if not isinstance(raw, str) or raw.strip() == "":
        raise ValueError(f"{source} 的 tmux session 名不能为空")
    if raw != raw.strip():
        raise ValueError(f"{source} 的 tmux session 名不能是空白或带首尾空白")
    return raw


def _file_values(path: Path) -> tuple[object | None, object | None]:
    if not path.is_file():
        return None, None
    try:
        data = tomllib.loads(path.read_text(encoding="utf-8"))
    except (OSError, tomllib.TOMLDecodeError) as exc:
        raise ValueError(f"无法读取配置 {path}: {exc}") from exc
    if not isinstance(data, dict):
        raise ValueError(f"配置 {path} 必须是表")
    return data.get("max_concurrent"), data.get("tmux_session")


def resolve_run_settings(
    *,
    cli_max: str | int | None = None,
    cli_session: str | None = None,
) -> tuple[int, str]:
    """返回 (max_concurrent, tmux_session)。非法值抛 ValueError。"""
    file_max: object | None = None
    file_session: object | None = None
    path = config_file_path()
    if path is not None:
        file_max, file_session = _file_values(path)

    if cli_max is not None:
        max_concurrent = _parse_max(cli_max, "CLI --max-concurrent")
    elif _ENV_MAX in os.environ:
        max_concurrent = _parse_max(os.environ.get(_ENV_MAX), _ENV_MAX)
    elif file_max is not None:
        max_concurrent = _parse_max(file_max, f"配置 {path}")
    else:
        max_concurrent = DEFAULT_MAX_CONCURRENT

    if cli_session is not None:
        tmux_session = _parse_session(cli_session, "CLI --session")
    elif _ENV_SESSION in os.environ:
        tmux_session = _parse_session(os.environ.get(_ENV_SESSION), _ENV_SESSION)
    elif file_session is not None:
        tmux_session = _parse_session(file_session, f"配置 {path}")
    else:
        tmux_session = DEFAULT_TMUX_SESSION

    return max_concurrent, tmux_session
