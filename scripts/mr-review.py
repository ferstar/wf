"""只读评审一个 GitHub PR 的文本 diff。禁止评论、改 PR、push、打标签。

    wf run scripts/mr-review.py --yes --args repo=owner/name pr=123
"""

import json
import os
import re
import subprocess

from wf import agent, args, log, parallel, phase

_ANSI = re.compile(r"\x1b\[[0-9;]*m")

REVIEW_SCHEMA = {
    "type": "object",
    "required": ["file", "verdict", "issues"],
    "properties": {
        "file": {"type": "string"},
        "verdict": {"enum": ["ok", "issues"]},
        "issues": {
            "type": "array",
            "items": {
                "type": "object",
                "required": ["line", "severity", "desc"],
                "properties": {
                    "line": {"type": ["integer", "string"]},
                    "severity": {"enum": ["info", "warning", "error"]},
                    "desc": {"type": "string"},
                },
            },
        },
    },
}

SKIP_SUFFIXES = (
    ".lock",
    ".png",
    ".jpg",
    ".jpeg",
    ".gif",
    ".webp",
    ".ico",
    ".pdf",
    ".zip",
    ".gz",
    ".woff",
    ".woff2",
)


def _gh(cmd: list[str]) -> str:
    env = os.environ.copy()
    env.pop("FORCE_COLOR", None)
    env["NO_COLOR"] = "1"
    env["GH_FORCE_TTY"] = "0"
    proc = subprocess.run(cmd, check=True, capture_output=True, text=True, env=env)
    return _ANSI.sub("", proc.stdout)


def split_diff(diff: str) -> dict[str, str]:
    chunks: dict[str, list[str]] = {}
    path = None
    for line in diff.splitlines(keepends=True):
        if line.startswith("diff --git "):
            # diff --git a/foo b/foo
            parts = line.strip().split(" b/")
            path = parts[-1] if len(parts) > 1 else None
            if path:
                chunks[path] = [line]
            continue
        if path:
            chunks[path].append(line)
    return {k: "".join(v) for k, v in chunks.items()}


repo = args.get("repo")
pr = args.get("pr")
if not repo or not pr:
    raise SystemExit("需要 --args repo=<owner/name> pr=<number>")
pr = str(pr)

phase("Discover")
view = json.loads(
    _gh(["gh", "pr", "view", pr, "--repo", repo, "--json", "title,url,files"])
)
log("title", view.get("title", ""))
log("url", view.get("url", ""))
diff = _gh(["gh", "pr", "diff", pr, "--repo", repo])
by_file = split_diff(diff)
files = []
for item in view.get("files") or []:
    path = item.get("path") or ""
    lower = path.lower()
    if not path or lower.endswith(SKIP_SUFFIXES) or "/lock" in lower:
        continue
    patch = by_file.get(path) or item.get("patch") or ""
    if not patch.strip():
        continue
    files.append({"path": path, "diff": patch})
if not files:
    # view 没给 files 时，退回 diff 里的路径。
    for path, patch in by_file.items():
        if path.lower().endswith(SKIP_SUFFIXES):
            continue
        files.append({"path": path, "diff": patch})
log("files", ",".join(item["path"] for item in files) or "(none)")

phase("Review")


def review(item: dict):
    prompt = (
        f"只读代码评审。不要修改文件，不要调用工具，diff 已完整附在下面。\n"
        f"文件: {item['path']}\n"
        f"请给出 verdict=ok 或 issues，并列出具体问题。回答的最后一段必须是 JSON。\n\n"
        f"{item['diff']}"
    )
    return agent(prompt, REVIEW_SCHEMA, role=f"Review {item['path']}", engine="pi", timeout=480)


reviews = await parallel(files, review)

phase("Summarize")
for rev in reviews:
    issues = rev.get("issues") or []
    brief = "; ".join(str(i.get("desc", "")) for i in issues) or "-"
    log(rev.get("file"), rev.get("verdict"), brief)
print(json.dumps(reviews, ensure_ascii=False))
