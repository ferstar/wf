"""并行概括一个目录里的文本文件。

用法（在仓库根目录，已安装 wf）::

    wf run examples/summarize_files.py --yes --args path=.

``path`` 是要扫描的目录，相对路径相对 ``wf run`` 的当前目录。
只看 ``.py`` / ``.md`` / ``.txt``，最多 8 个文件。默认引擎是 ``pi``。
换成 Claude Code 或 Codex 时，把下面的 ``engine`` 改成 ``claude`` 或 ``codex``。

没有真引擎时可以用假引擎（输出是占位 JSON，不是真摘要）::

    WF_ENGINE_BIN=wf-fake wf run examples/summarize_files.py --yes --args path=.
"""

import os
from pathlib import Path

from wf import agent, args, log, parallel, phase

SCHEMA = {
    "type": "object",
    "properties": {"summary": {"type": "string"}},
    "required": ["summary"],
}

phase("summarize")
root = Path(args["path"]).resolve()
files = [
    p
    for p in sorted(root.rglob("*"))
    if p.is_file() and p.suffix in {".py", ".md", ".txt"} and ".git" not in p.parts
][:8]
log("files", len(files))


async def one(path: Path):
    text = path.read_text(encoding="utf-8", errors="replace")[:4000]
    prompt = f"用一句话概括文件 {path.name} 的内容。\n\n{text}\n"
    if os.environ.get("WF_ENGINE_BIN"):
        prompt += '\n__RETURN__{"summary": "stub"}'
    return await agent(prompt, SCHEMA, role="summarize", engine="pi", tools="none")


rows = await parallel(files, one)
for row in rows:
    log(row["summary"])
