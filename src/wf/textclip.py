"""tools=none 时放宽 prompt 上限，截断必须记到 events 和 log。

tools 为 read 或 full 时不把大段正文塞进 prompt，让 agent 自己读。
"""

from __future__ import annotations

NONE_LIMIT = 100_000
READ_HINT = 1_200


def clip_for_prompt(text: str, *, tools: str, label: str) -> str:
    body = text or ""
    if tools in ("read", "full"):
        if len(body) > READ_HINT:
            return f"（{label} 共 {len(body)} 字符，未写入 prompt。请到仓库里自己读取。）"
        return body
    if len(body) <= NONE_LIMIT:
        return body
    from wf.primitives import log
    from wf.state import get_state

    state = get_state()
    state.emit(
        "truncation",
        {"file": label, "original_chars": len(body), "kept_chars": NONE_LIMIT},
    )
    log("truncation", label, f"original={len(body)}", f"kept={NONE_LIMIT}")
    return body[:NONE_LIMIT] + f"\n…(truncated {label}: {len(body)} -> {NONE_LIMIT})…"
