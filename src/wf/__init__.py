"""在 tmux 里调度 pi/grok agent 的小型 Python 编排库。

脚本里 `from wf import phase, log, agent, parallel, pipeline, args`。
这些名字只在 `wf run` 建立的运行上下文里有意义。
"""

from wf.primitives import agent, log, parallel, phase, pipeline

# CLI 在开跑前就地更新这个 dict，所以 `from wf import args` 看到的是同一次注入。
args: dict[str, str] = {}

__all__ = ["phase", "log", "agent", "parallel", "pipeline", "args"]
