"""测试用假引擎。读 prompt，按标记往 stdout 打 JSON，不调用 pi/grok。

标记（写在用户 prompt 里）：
- `__RETURN__{...}` 原样输出该 JSON（对象里不要嵌套花括号）
- `__BAD_ONCE__` 第一次输出非 JSON，之后输出 `{"ok": true}` 或 `__RETURN__`
- `__ALWAYS_BAD__` 永远输出非 JSON
- `__FAIL_FAST__` 马上非 0 退出
- `__SLEEP__` 睡 20 秒再输出 `{"ok": true}`
- `__ECHO_PREV__` 输出本 session 上一轮记住的 JSON
"""

from __future__ import annotations

import json
import re
import sys
import time
from pathlib import Path


def _args(argv: list[str]) -> tuple[str, Path, str]:
    prompt_parts: list[str] = []
    session_dir = Path("/tmp/wf-fake-session")
    session_id = "none"
    i = 0
    while i < len(argv):
        a = argv[i]
        if a in {"-p", "--print", "--single"}:
            i += 1
            continue
        if a in {
            "--session-dir",
            "--session-id",
            "--cwd",
            "--mode",
            "--output-format",
            "--json-schema",
            "--resume",
            "--session",
            "--engine",
            "--append-system-prompt",
            "--name",
            "--model",
            "--tools",
            "--exclude-tools",
            "--disallowed-tools",
        }:
            if i + 1 >= len(argv):
                break
            val = argv[i + 1]
            if a == "--session-dir":
                session_dir = Path(val)
            elif a == "--session-id":
                session_id = val
            elif a == "--resume" and session_id == "none":
                session_id = val
            i += 2
            continue
        if a.startswith("--"):
            i += 1
            continue
        prompt_parts.append(a)
        i += 1
    return "\n".join(prompt_parts), session_dir, session_id


def main() -> None:
    prompt, session_dir, session_id = _args(sys.argv[1:])
    session_dir.mkdir(parents=True, exist_ok=True)
    state_path = session_dir / "fake_state.json"
    if state_path.exists():
        state = json.loads(state_path.read_text(encoding="utf-8"))
    else:
        state = {"n": 0, "last": None, "session_id": session_id}
    state["n"] = int(state.get("n") or 0) + 1
    state["session_id"] = session_id

    def remember(text: str) -> None:
        try:
            state["last"] = json.loads(text)
        except json.JSONDecodeError:
            pass
        state_path.write_text(json.dumps(state), encoding="utf-8")

    if "__FAIL_FAST__" in prompt:
        remember("")
        sys.exit(3)
    if "__SLEEP__" in prompt:
        time.sleep(20)
        text = '{"ok": true}'
        remember(text)
        print(text)
        return
    if "__ALWAYS_BAD__" in prompt:
        remember("")
        print("this is not json")
        return
    if "__BAD_ONCE__" in prompt and "上次输出未通过校验" not in prompt and state["n"] == 1:
        remember("")
        print("this is not json")
        return
    if "__ECHO_PREV__" in prompt:
        text = json.dumps(state.get("last") if state.get("last") is not None else {"ok": False})
        remember(text)
        print(text)
        return
    match = re.search(r"__RETURN__(\{[^{}]*\})", prompt)
    if match:
        text = match.group(1).strip()
        remember(text)
        print(text)
        return
    text = '{"ok": true}'
    remember(text)
    print(text)


if __name__ == "__main__":
    main()
