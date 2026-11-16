"""从模型输出里抠 JSON，并用 jsonschema 校验。"""

from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any

import jsonschema


_FENCE = re.compile(r"```(?:json)?\s*([\s\S]*?)```", re.IGNORECASE)
_NOISE = re.compile(r"no project session found|no active session", re.IGNORECASE)


def parse_schema(schema: Any) -> dict[str, Any]:
    if schema is None or schema == "":
        raise ValueError("schema 必填（dict 或 JSON 字符串）")
    if isinstance(schema, str):
        try:
            schema = json.loads(schema)
        except json.JSONDecodeError as exc:
            raise ValueError(f"schema 不是合法 JSON: {exc}") from exc
    if not isinstance(schema, dict):
        raise ValueError("schema 必须是 object 或 JSON 对象字符串")
    return schema


def _clean_text(text: str) -> str:
    if "----- stderr -----" in text:
        text = text.split("----- stderr -----", 1)[0]
    text = _FENCE.sub(lambda m: "\n" + m.group(1) + "\n", text)
    kept = [line for line in text.splitlines() if not _NOISE.search(line)]
    return "\n".join(kept)


def _scan(text: str) -> list[Any]:
    decoder = json.JSONDecoder()
    values: list[Any] = []
    i = 0
    while i < len(text):
        if text[i] not in "{[":
            i += 1
            continue
        try:
            val, end = decoder.raw_decode(text[i:])
        except json.JSONDecodeError:
            i += 1
            continue
        values.append(val)
        i += max(end, 1)
    return values


def extract_json(text: str, schema: dict[str, Any] | None = None) -> Any:
    """去掉围栏、stderr 尾巴和 pi 的 session 提示后，取最后一段 JSON。

    若给了 schema，优先取最后一个能通过校验的对象。
    """
    if text is None:
        raise ValueError("引擎没有输出")
    values = _scan(_clean_text(text))
    objects = [v for v in values if isinstance(v, dict)]
    if schema is not None and objects:
        passed: list[Any] = []
        for obj in objects:
            try:
                validate(obj, schema)
            except jsonschema.ValidationError:
                continue
            passed.append(obj)
        if passed:
            return passed[-1]
    if objects:
        return objects[-1]
    if values:
        return values[-1]
    raise ValueError("输出中没有可解析的 JSON")


def validate(instance: Any, schema: dict[str, Any]) -> None:
    jsonschema.validate(instance, schema)


def coerce_result(output: str, schema: dict[str, Any], result_path: Path | None) -> Any:
    """引擎若已把合法 JSON 写到 result_path，优先用文件。"""
    if result_path is not None and result_path.is_file() and result_path.stat().st_size > 0:
        try:
            data = json.loads(result_path.read_text(encoding="utf-8"))
        except json.JSONDecodeError:
            data = None
        if data is not None:
            try:
                validate(data, schema)
                return data
            except jsonschema.ValidationError:
                pass
    value = extract_json(output, schema)
    validate(value, schema)
    return value


def build_prompt(
    user: str,
    schema: dict[str, Any],
    *,
    standing: str | None,
    retry_error: str | None,
    tools: str = "none",
    result_path: str | None = None,
) -> str:
    parts: list[str] = []
    if standing and standing.strip() and standing.strip() != user.strip():
        parts.append("常驻说明（每一轮都适用）：\n" + standing.strip())
    if not user or not str(user).strip():
        raise ValueError("prompt 不能为空")
    parts.append(str(user).strip())
    if tools == "none":
        parts.append("不要调用任何工具，不要修改文件。")
    elif tools == "read":
        parts.append(
            "只读模式：只能用 read / grep / find / ls，以及 bash 跑测试和只读的 git、gh、glab。"
            "禁止 edit/write，禁止改文件、禁止 commit、禁止 push、禁止在 GitHub/GitLab 上评论、改标签或关闭。"
        )
    elif tools == "full":
        parts.append(
            "你在隔离的 git worktree 里，请直接修改文件完成任务。"
            "不要 git push，不要在 GitHub/GitLab 上评论、改标签、关闭或触发 pipeline。"
            "结构化结论仍然要按 schema 输出。"
        )
    if result_path:
        parts.append(
            "请把最终 JSON 对象写入这个绝对路径（覆盖整个文件）：\n"
            f"{result_path}\n"
            "同时在 stdout 再输出同一段 JSON。"
        )
    parts.append(
        "只在最后给出符合下面 JSON Schema 的 JSON。"
        "用一个 ```json 围栏代码块，或一段纯 JSON。"
        "不要在 JSON 之后再写解释。"
    )
    parts.append(json.dumps(schema, ensure_ascii=False, indent=2))
    if retry_error:
        parts.append(
            "上次输出未通过校验。请只根据 schema 重答，不要重复解释。\n"
            f"校验错误：\n{retry_error}"
        )
    return "\n\n".join(parts)
