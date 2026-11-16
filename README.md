# wf

在 tmux 里调度 pi/grok agent 的小型 Python 编排库。脚本用 `phase` / `log` / `agent` / `parallel` / `pipeline` 把工作拆开；每个 agent 在 tmux 会话 `pi` 的新窗口里非交互执行 `pi -p` 或 `grok -p`。

本项目与 Google、Antigravity 无关，也不是它们的官方实现或附属产品。

没有沙箱。脚本里的 `import` 和子进程都会真的执行。闸门是跑之前给人看脚本（或显式 `--yes`）。

## 安装

```bash
git clone https://github.com/ferstar/wf
cd wf
uv venv .venv
uv pip install -e ".[dev]" --python .venv/bin/python
ln -sfn "$(pwd)/.venv/bin/wf" "$HOME/.local/bin/wf"
ln -sfn "$(pwd)/.venv/bin/wf-fake" "$HOME/.local/bin/wf-fake"
which wf
```

若系统不允许 `pip install --user`，用上面的虚拟环境再把入口接到 `~/.local/bin` 即可。

依赖：`jsonschema`（校验 agent 输出）、`pytest`（测试）。

测试走假引擎，不调用真的 pi/grok：

```bash
export WF_ENGINE_BIN=$HOME/.local/bin/wf-fake   # 安装后自带
pytest
```

`WF_ENGINE_BIN` 若设置，则 pi 和 grok 都改用这个可执行文件。假引擎认 prompt 里的 `__RETURN__` / `__BAD_ONCE__` / `__ALWAYS_BAD__` / `__FAIL_FAST__` / `__SLEEP__` / `__ECHO_PREV__`。

## 用法作弊条

1. `wf run script.py --yes --name 名字 --args k=v`：非 tty 必须 `--yes`，否则拒绝。
2. `phase("名")` 之后的 log 和 agent 记到该阶段；`log(a, b)` 至少要一个参数。
3. `await agent(prompt, schema, role="...", engine="pi")` 是一次性的；同一对象再 await 直接返回上次结果。
4. `a = agent("常驻说明", schema=S)` 然后 `await a(prompt, schema)` 才是多轮；不能对同一个对象又一次性 await 又 call。
5. `await parallel(items, fn)` 先对每个 item 调用 `fn`，再并发等待，结果按原顺序；一项失败就取消其余并杀掉已开的 tmux 窗口。`pipeline` 逐个 await，失败后不再调用后面的 `fn`。

运行目录默认是 `~/.cache/wf/runs/<name>-<本地上海时间 YYYYMMDD-HHMMSS>/`（可用环境变量 `WF_RUNS_ROOT` 覆盖）。里面有脚本副本、`events.jsonl`、`summary.json`。每一轮、每一次尝试写在 `agents/<id>/turn-N/attempt-M/`（`prompt.txt`、`output.txt`、`result.json`），不覆盖上一轮。`agents/<id>/session/` 仍在各轮之间共享。`agents/<id>/result.json` 是最近一轮的副本。

其它命令：`wf ls`、`wf status <run>`、`wf show <run>`、`wf kill <run|all>`、`wf gc`。

`wf gc` 清理**已经结束或被 kill** 的 run 留下的 worktree。`keep=True`（写在 agent 的 `meta.json` 里）的 worktree 会留着。`wf kill` 也会清掉该 run 里没有 keep 的 worktree。

`log()` 过长时只在 events / `logs.txt` 里留摘要，全文在 `logs/log-NNNN.txt`。`wf run` 的标准输出按行缓冲。

`args` 是 dict，值都是字符串。也注入了同名全局量，脚本可以 `from wf import args`。

示例脚本 `scripts/mr-review.py` 只读评审一个 GitHub PR：`--args repo=owner/name pr=123`（两项都必填）。

## 工具模式

`agent(..., tools="none"|"read"|"full")`，默认 `"none"`。

- `none`：pi 加 `--no-tools`。grok 去掉读写和 shell 工具。不自动批准。
- `read`：pi 用 `--tools read,grep,find,ls,bash` 和 `--exclude-tools edit,write`。grok 用 `--tools read_file,grep,list_dir,run_terminal_cmd`，**不加** `--always-approve`。prompt 里禁止改文件；bash 只应用于跑测试和只读的 `git` / `gh` / `glab`。这种模式不要把大段 diff 预先塞进 prompt。
- `full`：**只有** `workspace="branch"` 才允许，否则 `ValueError`。pi 带齐内置工具。grok 加 `--always-approve`。agent 在 worktree 里直接改文件，调用方不要再靠 JSON hunk 去套补丁。schema 结论仍然要有。

`tools="none"` 时若脚本用 `wf.textclip.clip_for_prompt` 截断正文，上限是 10 万字符，并在 events 里记 `truncation`（标签、原长度、留下多少）。

## 仓库缓存

`wf.repo.ensure_checkout(url, dest)` 把 clone 放进共享镜像，默认 `~/.cache/wf/mirrors/<host>/<path>.git`（可用 `WF_CACHE_ROOT` 改）。第一次 `git clone --mirror`，之后 `git fetch`，再 `git worktree add` 到目标目录。GitLab 用 `GITLAB_TOKEN` 的 askpass；GitHub 用 `GH_TOKEN` / `GITHUB_TOKEN`，否则用 `gh auth token`。clone / fetch / 认证失败会打到 **stderr**，并在 `wf run` 里写 `events.jsonl` 的 `clone_error`。

## 多轮 session

全局同时最多 **3** 个引擎窗口。开新窗口前会数 tmux 会话 `pi` 里已经存在的非占位窗口（不算 `idle` 和 `wf-keep`），再加本进程已经拿下的槽。所有 bot 共用这 3 个槽。满了就排队，直到有空位再 `new-window`。这是本库的限制。

- **pi：真 session。** 同一 agent 固定 `--session-id` 和 `--session-dir`（目录在该 agent 的 `session/`）。第一轮创建会话；之后（含校验失败后的那一次重试）用同一对参数接上，不另传 `--session`（pi 会拒绝 `--session-id` 与 `--session` 同时出现）。常驻说明用 `--append-system-prompt` 放在系统侧。
- **grok：真 session。** 第一轮 `--session-id <uuid>` 打开这个 id 的新会话；之后 `--resume <uuid>`。不用 `--system-prompt-override`（会盖掉默认人设），常驻说明附在每轮用户 prompt 前面。`workspace=branch` 也不使用 grok 自带的 `--worktree`。

校验：先去掉 markdown 围栏、`----- stderr -----` 之后的内容，以及 pi 的 “No project session found…” 一类提示。有 schema 时取**最后一段能通过校验的 JSON 对象**。prompt 里会给出 `result.json` 的绝对路径；若该文件已经是合法结果，优先用文件。失败则把错误追加进**同一会话**的下一轮 prompt，**只重试一次**。再失败就抛错。没有单独的「结束」工具；等价物是「进程退出且输出里有通过校验的 JSON」。

`workspace="branch"`：在 run 目录用 `git worktree add -b` 给该 agent 一块隔离工作区（`cwd` 必须是 git 仓库）。`keep=False` 且 `keep_on_success=False`（默认）时，成功就删掉 worktree 和分支，失败则留在 `summary.json` 的 `kept_worktrees` 里，等 `wf gc` 或 `wf kill` 再清。`keep=True` 无论成败都留。`keep_on_success=True` 成功也留。`share` 会 `ValueError`。`inherit` 共用启动 `wf` 时的 cwd（或该 agent 的 `cwd`），并行写会互相踩，这是刻意的。

`timeout` 是该 agent 的墙钟秒数，超时会杀掉对应 tmux 窗口。

子 agent 默认**不**加 `pi --approve` 或 `grok --always-approve`。pi 额外加了 `--no-tools --no-extensions --no-mcp --no-skills --thinking low`，避免无人值守时停在工具批准上，也避免默认 medium thinking 把墙钟耗光。需要读盘的上下文请写进 prompt。引擎在 tmux 里用 `stdbuf -oL` 包一层，否则非 tty 的 stdout 全缓冲，进程不退出时 JSON 读不出来。

## 已知限制

不提供共享工作区、预算信封、外部 workflow 导入或模型档映射。脚本里未定义的名字会 `NameError`。不禁止本地 `import`。`wf run` 在前台阻塞到结束，没有主进程通知。无沙箱，跑前靠人看脚本。

## 许可证

MIT。Copyright 2026 Jeff Zhang。
