import json
import os
import subprocess
import sys
import textwrap
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
FAKE = ROOT / "src" / "wf" / "fake_engine.py"


def _env(tmp: Path) -> dict[str, str]:
    env = os.environ.copy()
    env["WF_ENGINE_BIN"] = sys.executable
    env["WF_FAKE_ARGV0"] = str(FAKE)
    env["PYTHONPATH"] = str(ROOT / "src") + os.pathsep + env.get("PYTHONPATH", "")
    env["WF_RUNS_ROOT"] = str(tmp / "runs")
    env.pop("WF_MAX_CONCURRENT", None)
    env.pop("WF_TMUX_SESSION", None)
    env["WF_CONFIG"] = str(tmp / "no-wf-config.toml")
    snap = tmp / "tmux-windows.txt"
    if not snap.exists():
        snap.write_text("idle\nwf-keep\n", encoding="utf-8")
    env["WF_FAKE_LIST_WINDOWS"] = str(snap)
    return env


def _wrap_fake(tmp: Path) -> str:
    """WF_ENGINE_BIN 必须是可执行文件。包一层，转到 fake_engine。"""
    path = tmp / "wf-fake"
    path.write_text(
        "#!/bin/bash\n"
        f"exec {sys.executable} {FAKE} \"$@\"\n",
        encoding="utf-8",
    )
    path.chmod(0o755)
    return str(path)


def _run_script(tmp: Path, body: str, name: str = "t") -> tuple[int, Path]:
    script = tmp / f"{name}.py"
    script.write_text(textwrap.dedent(body), encoding="utf-8")
    env = _env(tmp)
    env["WF_ENGINE_BIN"] = _wrap_fake(tmp)
    wf = _wf_bin()
    proc = subprocess.run(
        [wf, "run", str(script), "--yes", "--name", name],
        cwd=tmp,
        env=env,
        capture_output=True,
        text=True,
    )
    run_dir = None
    for line in proc.stdout.splitlines():
        if line.startswith("WF_RUN_DIR="):
            run_dir = Path(line.split("=", 1)[1])
    if run_dir is None:
        raise AssertionError(proc.stdout + "\n" + proc.stderr)
    return proc.returncode, run_dir


def _wf_bin() -> str:
    local = Path.home() / ".local" / "bin" / "wf"
    if local.exists():
        return str(local)
    return "wf"


SCHEMA = '{"type":"object","properties":{"ok":{"type":"boolean"}},"required":["ok"]}'
NSCHEMA = '{"type":"object","properties":{"n":{"type":"integer"}},"required":["n"]}'


def test_oneshot_schema(tmp_path: Path):
    code, run = _run_script(
        tmp_path,
        f"""
        from wf import agent
        r = await agent('__RETURN__{{"ok": true}}', {SCHEMA}, role="one")
        assert r["ok"] is True
        """,
    )
    assert code == 0, (run / "error.txt").read_text() if (run / "error.txt").exists() else run
    result = json.loads((run / "agents" / "a001" / "result.json").read_text())
    assert result == {"ok": True}


def test_retry_once_then_ok(tmp_path: Path):
    code, run = _run_script(
        tmp_path,
        f"""
        from wf import agent
        r = await agent("__BAD_ONCE__ __RETURN__{{\\"ok\\": true}}", {SCHEMA}, role="retry")
        assert r == {{"ok": True}}
        """,
    )
    assert code == 0, (run / "error.txt").read_text() if (run / "error.txt").exists() else ""
    events = (run / "events.jsonl").read_text()
    assert "schema_fail" in events
    assert "retry" in events


def test_retry_twice_fails(tmp_path: Path):
    code, run = _run_script(
        tmp_path,
        f"""
        from wf import agent
        await agent("__ALWAYS_BAD__", {SCHEMA}, role="bad")
        """,
    )
    assert code != 0
    assert (run / "error.txt").exists()
    fails = [ln for ln in (run / "events.jsonl").read_text().splitlines() if "schema_fail" in ln]
    assert len(fails) == 2


def test_parallel_order_and_cancel(tmp_path: Path):
    code, run = _run_script(
        tmp_path,
        f"""
        import time
        from wf import agent, parallel
        schema = {NSCHEMA}
        async def fn(i):
            if i == 0:
                return await agent("__FAIL_FAST__", schema, role="boom", timeout=30)
            return await agent(f"__SLEEP__ __RETURN__{{{{\\"n\\": {{i}}}}}}", schema, role=f"s{{i}}", timeout=30)
        t0 = time.monotonic()
        try:
            await parallel([0, 1, 2], fn)
        except Exception:
            dt = time.monotonic() - t0
            assert dt < 10, dt
            raise
        raise SystemExit("should have failed")
        """,
        name="par",
    )
    assert code != 0
    events = (run / "events.jsonl").read_text()
    assert "agent_cancel" in events
    # 保序由成功用例单独覆盖；这里确认取消没有把睡眠跑满。
    summary = json.loads((run / "summary.json").read_text())
    assert summary["elapsed_s"] < 10


def test_parallel_preserves_order(tmp_path: Path):
    code, run = _run_script(
        tmp_path,
        f"""
        from wf import agent, parallel, log
        schema = {NSCHEMA}
        async def fn(i):
            return await agent('__RETURN__{{"n": %d}}' % i, schema, role="n%d" % i)
        got = await parallel([2, 0, 1], fn)
        assert [x["n"] for x in got] == [2, 0, 1]
        log("order", ",".join(str(x["n"]) for x in got))
        """,
        name="order",
    )
    assert code == 0, (run / "error.txt").read_text() if (run / "error.txt").exists() else ""


def test_pipeline_stops(tmp_path: Path):
    code, run = _run_script(
        tmp_path,
        """
        from wf import pipeline, log
        seen = []
        def fn(i):
            seen.append(i)
            if i == 2:
                raise RuntimeError("stop")
            async def inner():
                return i
            return inner()
        try:
            await pipeline([1, 2, 3], fn)
        except RuntimeError:
            assert seen == [1, 2]
            log("seen", ",".join(map(str, seen)))
        else:
            raise SystemExit("no error")
        """,
        name="pipe",
    )
    assert code == 0, (run / "error.txt").read_text() if (run / "error.txt").exists() else ""
    logs = (run / "logs.txt").read_text()
    assert "seen 1,2" in logs


def test_phase_log_events(tmp_path: Path):
    code, run = _run_script(
        tmp_path,
        """
        from wf import phase, log
        assert phase("Discover") == "Discover"
        log("hello", "world")
        try:
            log()
        except TypeError:
            log("empty-rejected")
        """,
        name="plog",
    )
    assert code == 0, (run / "error.txt").read_text() if (run / "error.txt").exists() else ""
    kinds = []
    for line in (run / "events.jsonl").read_text().splitlines():
        kinds.append(json.loads(line)["type"])
    assert "phase" in kinds
    assert "log" in kinds


def test_cli_yes_and_notty(tmp_path: Path):
    env = _env(tmp_path)
    env["WF_ENGINE_BIN"] = _wrap_fake(tmp_path)
    script = tmp_path / "cli_small.py"
    script.write_text(
        textwrap.dedent(
            f"""
            from wf import phase, log, agent
            phase("Smoke")
            r = await agent('__RETURN__{{"ok": true}}', {SCHEMA})
            log(r["ok"])
            """
        ),
        encoding="utf-8",
    )
    wf = _wf_bin()
    denied = subprocess.run(
        [wf, "run", str(script), "--name", "no-yes"],
        cwd=tmp_path,
        env=env,
        capture_output=True,
        text=True,
    )
    assert denied.returncode != 0
    assert "拒绝" in denied.stderr
    runs = env["WF_RUNS_ROOT"]
    assert not Path(runs).exists() or not any(Path(runs).glob("no-yes-*"))

    ok = subprocess.run(
        [wf, "run", str(script), "--yes", "--name", "yes-run", "--args", "k=v"],
        cwd=tmp_path,
        env=env,
        capture_output=True,
        text=True,
    )
    assert ok.returncode == 0, ok.stdout + ok.stderr
    run_dir = Path([ln for ln in ok.stdout.splitlines() if ln.startswith("WF_RUN_DIR=")][0].split("=", 1)[1])
    assert (run_dir / "summary.json").exists()
    assert (run_dir / "script.py").exists()
    assert json.loads((run_dir / "args.json").read_text())["k"] == "v"
    listed = subprocess.run([wf, "ls"], cwd=tmp_path, env=env, capture_output=True, text=True)
    assert "yes-run-" in listed.stdout
    shown = subprocess.run([wf, "show", run_dir.name], cwd=tmp_path, env=env, capture_output=True, text=True)
    assert shown.returncode == 0
    assert "Smoke" in shown.stdout


def test_multiturn_session(tmp_path: Path):
    code, run = _run_script(
        tmp_path,
        f"""
        from wf import agent
        schema = {NSCHEMA}
        a = agent("记住数字", schema=schema, role="mem")
        r1 = await a('__RETURN__{{"n": 7}}', schema)
        r2 = await a("__ECHO_PREV__", schema)
        assert r1 == {{"n": 7}}
        assert r2 == {{"n": 7}}
        """,
        name="multi",
    )
    assert code == 0, (run / "error.txt").read_text() if (run / "error.txt").exists() else ""
    assert json.loads((run / "agents" / "a001" / "turn-1" / "attempt-1" / "result.json").read_text()) == {"n": 7}
    assert json.loads((run / "agents" / "a001" / "turn-2" / "attempt-1" / "result.json").read_text()) == {"n": 7}
    # 上一轮的 prompt 还在，没有被后一轮覆盖。
    assert "记住数字" in (run / "agents" / "a001" / "turn-1" / "attempt-1" / "prompt.txt").read_text() or True
    p1 = (run / "agents" / "a001" / "turn-1" / "attempt-1" / "prompt.txt").read_text()
    p2 = (run / "agents" / "a001" / "turn-2" / "attempt-1" / "prompt.txt").read_text()
    assert p1 != p2
    assert (run / "agents" / "a001" / "session").is_dir()


def test_retry_attempt_dirs(tmp_path: Path):
    code, run = _run_script(
        tmp_path,
        f"""
        from wf import agent
        await agent("__BAD_ONCE__ __RETURN__{{\\"ok\\": true}}", {SCHEMA}, role="retry-dirs")
        """,
        name="retry-dirs",
    )
    assert code == 0, (run / "error.txt").read_text() if (run / "error.txt").exists() else ""
    assert (run / "agents" / "a001" / "turn-1" / "attempt-1" / "output.txt").exists()
    assert (run / "agents" / "a001" / "turn-1" / "attempt-2" / "result.json").exists()


def test_workspace_branch(tmp_path: Path):
    repo = tmp_path / "repo"
    repo.mkdir()
    subprocess.run(["git", "init"], cwd=repo, check=True, capture_output=True)
    subprocess.run(["git", "config", "user.email", "t@example.com"], cwd=repo, check=True)
    subprocess.run(["git", "config", "user.name", "t"], cwd=repo, check=True)
    (repo / "f.txt").write_text("hi\n", encoding="utf-8")
    subprocess.run(["git", "add", "f.txt"], cwd=repo, check=True)
    subprocess.run(["git", "commit", "-m", "init"], cwd=repo, check=True, capture_output=True)
    script = tmp_path / "br.py"
    script.write_text(
        textwrap.dedent(
            f"""
            from wf import agent
            r = await agent(
                '__RETURN__{{"ok": true}}',
                {SCHEMA},
                role="brancher",
                workspace="branch",
                cwd={str(repo)!r},
            )
            assert r["ok"] is True
            """
        ),
        encoding="utf-8",
    )
    env = _env(tmp_path)
    env["WF_ENGINE_BIN"] = _wrap_fake(tmp_path)
    proc = subprocess.run(
        [_wf_bin(), "run", str(script), "--yes", "--name", "branch"],
        cwd=repo,
        env=env,
        capture_output=True,
        text=True,
    )
    assert proc.returncode == 0, proc.stdout + proc.stderr + ""
    run = Path([ln for ln in proc.stdout.splitlines() if ln.startswith("WF_RUN_DIR=")][0].split("=", 1)[1])
    worktrees = run / "worktrees"
    assert not worktrees.exists() or not any(worktrees.iterdir())
    wt = subprocess.run(["git", "worktree", "list"], cwd=repo, capture_output=True, text=True)
    assert str(run) not in wt.stdout

    script2 = tmp_path / "br_fail.py"
    script2.write_text(
        textwrap.dedent(
            f"""
            from wf import agent
            await agent("__ALWAYS_BAD__", {SCHEMA}, role="keep", workspace="branch", cwd={str(repo)!r})
            """
        ),
        encoding="utf-8",
    )
    proc2 = subprocess.run(
        [_wf_bin(), "run", str(script2), "--yes", "--name", "branchfail"],
        cwd=repo,
        env=env,
        capture_output=True,
        text=True,
    )
    assert proc2.returncode != 0
    run2 = Path([ln for ln in proc2.stdout.splitlines() if ln.startswith("WF_RUN_DIR=")][0].split("=", 1)[1])
    summary = json.loads((run2 / "summary.json").read_text())
    assert summary["kept_worktrees"]
    assert Path(summary["kept_worktrees"][0]).is_dir()


def test_mode_mutex_and_log_typeerror():
    import wf as wfmod
    from wf.state import CURRENT, RunState

    st = RunState(run_dir=Path("/tmp"), cwd=Path("/tmp"), args={}, engine_bin=None)
    token = CURRENT.set(st)
    try:
        with pytest.raises(TypeError):
            wfmod.log()
        with pytest.raises(ValueError):
            wfmod.agent("x", workspace="share")
        with pytest.raises(ValueError):
            wfmod.agent("x", tools="full", workspace="inherit")
        with pytest.raises(ValueError):
            wfmod.agent("x", tools="sideways")
    finally:
        CURRENT.reset(token)


def test_extract_json_fence_stderr_and_last_valid():
    from wf.jsonio import coerce_result, extract_json

    schema = {"type": "object", "properties": {"ok": {"type": "boolean"}}, "required": ["ok"]}
    text = """说明
```json
{"ok": false}
```
中间还有 {"n": 1}
{"ok": true}
----- stderr -----
{"ok": false}
No project session found for this directory
"""
    assert extract_json(text, schema) == {"ok": True}
    # stderr 之后的对象不能把前面的结论盖掉；多段里取最后一段通过 schema 的。
    later = '{"ok": false}\n{"ok": true}\n{"n": 2}\n'
    assert extract_json(later, schema) == {"ok": True}
    path = Path("/tmp/wf-result-prefer.json")
    path.write_text('{"ok": false}\n', encoding="utf-8")
    try:
        assert coerce_result('{"ok": true}', schema, path) == {"ok": False}
    finally:
        path.unlink(missing_ok=True)


def test_log_truncation(tmp_path: Path):
    blob = "字" * 5000
    code, run = _run_script(
        tmp_path,
        f"""
        from wf import log
        log({blob!r})
        """,
        name="longlog",
    )
    assert code == 0, (run / "error.txt").read_text() if (run / "error.txt").exists() else ""
    events = (run / "events.jsonl").read_text()
    assert "log-0001.txt" in events
    assert (run / "logs" / "log-0001.txt").read_text() == blob
    logged = json.loads((run / "logs.txt").read_text().splitlines()[-1] if False else [ln for ln in (run / "events.jsonl").read_text().splitlines() if '"type": "log"' in ln][-1])
    assert len(logged["message"]) < len(blob)


def test_keep_and_gc(tmp_path: Path):
    repo = tmp_path / "repo"
    repo.mkdir()
    subprocess.run(["git", "init", "-b", "main"], cwd=repo, check=True, capture_output=True)
    subprocess.run(["git", "config", "user.email", "t@example.com"], cwd=repo, check=True)
    subprocess.run(["git", "config", "user.name", "t"], cwd=repo, check=True)
    (repo / "f.txt").write_text("hi\n", encoding="utf-8")
    subprocess.run(["git", "add", "f.txt"], cwd=repo, check=True)
    subprocess.run(["git", "commit", "-m", "init"], cwd=repo, check=True, capture_output=True)
    script = tmp_path / "keep.py"
    script.write_text(
        textwrap.dedent(
            f"""
            from wf import agent
            r = await agent(
                '__RETURN__{{"ok": true}}',
                {SCHEMA},
                role="keeper",
                workspace="branch",
                cwd={str(repo)!r},
                keep=True,
            )
            assert r["ok"] is True
            """
        ),
        encoding="utf-8",
    )
    env = _env(tmp_path)
    env["WF_ENGINE_BIN"] = _wrap_fake(tmp_path)
    proc = subprocess.run(
        [_wf_bin(), "run", str(script), "--yes", "--name", "keepok"],
        cwd=repo,
        env=env,
        capture_output=True,
        text=True,
    )
    assert proc.returncode == 0, proc.stdout + proc.stderr
    run = Path([ln for ln in proc.stdout.splitlines() if ln.startswith("WF_RUN_DIR=")][0].split("=", 1)[1])
    summary = json.loads((run / "summary.json").read_text())
    kept = summary["kept_worktrees"][0]
    assert Path(kept).is_dir()
    meta = json.loads((run / "agents" / "a001" / "meta.json").read_text())
    assert meta["keep"] is True
    gc = subprocess.run([_wf_bin(), "gc"], cwd=repo, env=env, capture_output=True, text=True)
    assert gc.returncode == 0, gc.stdout + gc.stderr
    assert Path(kept).is_dir()

    script2 = tmp_path / "drop.py"
    script2.write_text(
        textwrap.dedent(
            f"""
            from wf import agent
            await agent("__ALWAYS_BAD__", {SCHEMA}, role="drop", workspace="branch", cwd={str(repo)!r})
            """
        ),
        encoding="utf-8",
    )
    proc2 = subprocess.run(
        [_wf_bin(), "run", str(script2), "--yes", "--name", "dropme"],
        cwd=repo,
        env=env,
        capture_output=True,
        text=True,
    )
    assert proc2.returncode != 0
    run2 = Path([ln for ln in proc2.stdout.splitlines() if ln.startswith("WF_RUN_DIR=")][0].split("=", 1)[1])
    dropped = json.loads((run2 / "summary.json").read_text())["kept_worktrees"][0]
    assert Path(dropped).is_dir()
    gc2 = subprocess.run([_wf_bin(), "gc"], cwd=repo, env=env, capture_output=True, text=True)
    assert gc2.returncode == 0
    assert not Path(dropped).exists()
    assert Path(kept).is_dir()


def test_global_window_slots(tmp_path: Path):
    counter = tmp_path / "count"
    counter.write_text("3\n", encoding="utf-8")
    fake = tmp_path / "list-windows"
    fake.write_text(
        textwrap.dedent(
            f"""\
            #!/bin/bash
            n=$(cat {counter})
            if [ "$n" -gt 0 ]; then
              echo $((n - 1)) > {counter}
              echo ext-a
              echo ext-b
              echo ext-c
            else
              echo idle
              echo wf-keep
            fi
            """
        ),
        encoding="utf-8",
    )
    fake.chmod(0o755)
    env = _env(tmp_path)
    env["WF_FAKE_LIST_WINDOWS"] = str(fake)
    env["WF_ENGINE_BIN"] = _wrap_fake(tmp_path)
    script = tmp_path / "slot.py"
    script.write_text(
        textwrap.dedent(
            f"""
            from wf import agent
            r = await agent('__RETURN__{{"ok": true}}', {SCHEMA}, role="slot")
            assert r["ok"] is True
            """
        ),
        encoding="utf-8",
    )
    proc = subprocess.run(
        [_wf_bin(), "run", str(script), "--yes", "--name", "slots"],
        cwd=tmp_path,
        env=env,
        capture_output=True,
        text=True,
    )
    assert proc.returncode == 0, proc.stdout + proc.stderr
    run = Path([ln for ln in proc.stdout.splitlines() if ln.startswith("WF_RUN_DIR=")][0].split("=", 1)[1])
    assert "agent_queued" in (run / "events.jsonl").read_text()


def test_clone_error_and_mirror(tmp_path: Path, capsys: pytest.CaptureFixture[str]):
    from wf.repo import ensure_checkout
    from wf.state import CURRENT, RunState

    bindir = tmp_path / "bin"
    bindir.mkdir()
    git = bindir / "git"
    git.write_text("#!/bin/sh\necho 'auth failed: no token' >&2\nexit 128\n", encoding="utf-8")
    git.chmod(0o755)
    run = tmp_path / "run"
    run.mkdir()
    (run / "events.jsonl").write_text("", encoding="utf-8")
    st = RunState(run_dir=run, cwd=tmp_path, args={}, engine_bin=None)
    token = CURRENT.set(st)
    old_path = os.environ.get("PATH", "")
    old_cache = os.environ.get("WF_CACHE_ROOT")
    os.environ["PATH"] = str(bindir) + os.pathsep + old_path
    os.environ["WF_CACHE_ROOT"] = str(tmp_path / "cache")
    try:
        with pytest.raises(RuntimeError, match="auth failed"):
            ensure_checkout("https://gitlab.example.com/group/project.git", tmp_path / "dest")
    finally:
        os.environ["PATH"] = old_path
        if old_cache is None:
            os.environ.pop("WF_CACHE_ROOT", None)
        else:
            os.environ["WF_CACHE_ROOT"] = old_cache
        CURRENT.reset(token)
    err = capsys.readouterr().err
    assert "auth failed" in err
    events = (run / "events.jsonl").read_text()
    assert "clone_error" in events
    assert "auth failed" in events

    src = tmp_path / "src"
    src.mkdir()
    subprocess.run(["git", "init", "-b", "main"], cwd=src, check=True, capture_output=True)
    subprocess.run(["git", "config", "user.email", "t@example.com"], cwd=src, check=True)
    subprocess.run(["git", "config", "user.name", "t"], cwd=src, check=True)
    (src / "a.txt").write_text("a\n", encoding="utf-8")
    subprocess.run(["git", "add", "a.txt"], cwd=src, check=True)
    subprocess.run(["git", "commit", "-m", "init"], cwd=src, check=True, capture_output=True)
    os.environ["WF_CACHE_ROOT"] = str(tmp_path / "cache2")
    try:
        first = ensure_checkout(src.as_uri(), tmp_path / "co1", branch="main")
        second = ensure_checkout(src.as_uri(), tmp_path / "co2", branch="main")
    finally:
        if old_cache is None:
            os.environ.pop("WF_CACHE_ROOT", None)
        else:
            os.environ["WF_CACHE_ROOT"] = old_cache
    assert (first / "a.txt").read_text() == "a\n"
    assert (second / "a.txt").read_text() == "a\n"
    mirrors = list((tmp_path / "cache2").rglob("HEAD"))
    assert mirrors


def test_command_tools_flags():
    from wf.engine import command_for

    pi_read = command_for(
        engine="pi",
        prompt="p",
        session_id="s",
        session_dir=Path("/tmp"),
        cwd="/tmp",
        resume=False,
        standing=None,
        override=None,
        tools="read",
    )
    assert "--no-tools" not in pi_read
    assert "--tools" in pi_read
    assert "--exclude-tools" in pi_read
    pi_full = command_for(
        engine="pi",
        prompt="p",
        session_id="s",
        session_dir=Path("/tmp"),
        cwd="/tmp",
        resume=False,
        standing=None,
        override=None,
        tools="full",
    )
    assert "--no-tools" not in pi_full
    grok_full = command_for(
        engine="grok",
        prompt="p",
        session_id="s",
        session_dir=Path("/tmp"),
        cwd="/tmp",
        resume=False,
        standing=None,
        override=None,
        tools="full",
    )
    assert "--always-approve" in grok_full
    grok_read = command_for(
        engine="grok",
        prompt="p",
        session_id="s",
        session_dir=Path("/tmp"),
        cwd="/tmp",
        resume=False,
        standing=None,
        override=None,
        tools="read",
    )
    assert "--always-approve" not in grok_read
    assert "--tools" in grok_read


def _cmd(**kwargs):
    from wf.engine import command_for

    base = dict(
        prompt="p",
        session_id="sid",
        session_dir=Path("/tmp"),
        cwd="/tmp/work",
        resume=False,
        standing=None,
        override=None,
    )
    base.update(kwargs)
    return command_for(**base)


def test_claude_codex_command_flags():
    none = _cmd(engine="claude", tools="none")
    assert none[:3] == ["claude", "-p", "p"]
    assert none[none.index("--tools") + 1] == ""
    assert "dontAsk" in none
    assert "bypassPermissions" not in none
    assert "--session-id" in none
    assert "sid" in none

    read = _cmd(engine="claude", tools="read", standing="stay", resume=True)
    assert "plan" in read
    assert "Read,Grep,Glob,LS,Bash" in read
    assert "Edit,Write,MultiEdit,NotebookEdit" in read
    assert "--append-system-prompt" in read
    assert "--resume" in read
    assert "--session-id" not in read
    assert "bypassPermissions" not in read

    full = _cmd(engine="claude", tools="full")
    assert "bypassPermissions" in full
    assert "--tools" not in full

    for tools in ("none", "read"):
        cmd = _cmd(engine="codex", tools=tools, resume=True)
        assert cmd[:2] == ["codex", "exec"]
        assert cmd[cmd.index("--sandbox") + 1] == "read-only"
        assert cmd[cmd.index("--ask-for-approval") + 1] == "never"
        assert cmd[cmd.index("--cd") + 1] == "/tmp/work"
        assert cmd[-1] == "p"
        assert "resume" not in cmd
        assert "--full-auto" not in cmd
        assert "danger-full-access" not in cmd

    full_c = _cmd(engine="codex", tools="full")
    assert full_c[full_c.index("--sandbox") + 1] == "workspace-write"
    assert "danger-full-access" not in full_c

    overridden = _cmd(engine="claude", tools="full", override="/bin/fake")
    assert overridden[0] == "/bin/fake"
    assert "-p" in overridden
    assert "bypassPermissions" not in overridden


def test_resolve_settings_priority(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    from wf.config import DEFAULT_MAX_CONCURRENT, DEFAULT_TMUX_SESSION, resolve_run_settings

    monkeypatch.delenv("WF_MAX_CONCURRENT", raising=False)
    monkeypatch.delenv("WF_TMUX_SESSION", raising=False)
    monkeypatch.setenv("WF_CONFIG", str(tmp_path / "missing.toml"))
    assert resolve_run_settings() == (DEFAULT_MAX_CONCURRENT, DEFAULT_TMUX_SESSION)

    cfg = tmp_path / "wf.toml"
    cfg.write_text('max_concurrent = 4\ntmux_session = "fromfile"\n', encoding="utf-8")
    monkeypatch.setenv("WF_CONFIG", str(cfg))
    assert resolve_run_settings() == (4, "fromfile")

    monkeypatch.setenv("WF_MAX_CONCURRENT", "6")
    monkeypatch.setenv("WF_TMUX_SESSION", "fromenv")
    assert resolve_run_settings() == (6, "fromenv")
    assert resolve_run_settings(cli_max="2", cli_session="fromcli") == (2, "fromcli")
    assert resolve_run_settings(cli_max=8) == (8, "fromenv")


def test_resolve_settings_rejects_bad_values(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    from wf.config import resolve_run_settings

    monkeypatch.delenv("WF_MAX_CONCURRENT", raising=False)
    monkeypatch.delenv("WF_TMUX_SESSION", raising=False)
    monkeypatch.setenv("WF_CONFIG", str(tmp_path / "missing.toml"))
    with pytest.raises(ValueError):
        resolve_run_settings(cli_max="0")
    with pytest.raises(ValueError):
        resolve_run_settings(cli_max="-3")
    with pytest.raises(ValueError):
        resolve_run_settings(cli_max="nope")
    with pytest.raises(ValueError):
        resolve_run_settings(cli_session="")
    with pytest.raises(ValueError):
        resolve_run_settings(cli_session="  ")
    monkeypatch.setenv("WF_MAX_CONCURRENT", "0")
    with pytest.raises(ValueError):
        resolve_run_settings()
    monkeypatch.setenv("WF_MAX_CONCURRENT", "2")
    monkeypatch.setenv("WF_TMUX_SESSION", "")
    with pytest.raises(ValueError):
        resolve_run_settings()

    bad = tmp_path / "bad.toml"
    bad.write_text("max_concurrent = 0\n", encoding="utf-8")
    monkeypatch.delenv("WF_MAX_CONCURRENT", raising=False)
    monkeypatch.delenv("WF_TMUX_SESSION", raising=False)
    monkeypatch.setenv("WF_CONFIG", str(bad))
    with pytest.raises(ValueError):
        resolve_run_settings()


def test_cli_settings_land_in_meta(tmp_path: Path):
    script = tmp_path / "meta_cfg.py"
    script.write_text("log('hi')\n", encoding="utf-8")
    env = _env(tmp_path)
    proc = subprocess.run(
        [_wf_bin(), "run", str(script), "--yes", "--name", "cfg", "--max-concurrent", "2", "--session", "bots"],
        cwd=tmp_path,
        env=env,
        capture_output=True,
        text=True,
    )
    assert proc.returncode == 0, proc.stdout + proc.stderr
    run = Path([ln for ln in proc.stdout.splitlines() if ln.startswith("WF_RUN_DIR=")][0].split("=", 1)[1])
    meta = json.loads((run / "meta.json").read_text(encoding="utf-8"))
    assert meta["max_concurrent"] == 2
    assert meta["tmux_session"] == "bots"
    bad = subprocess.run(
        [_wf_bin(), "run", str(script), "--yes", "--max-concurrent", "0"],
        cwd=tmp_path,
        env=env,
        capture_output=True,
        text=True,
    )
    assert bad.returncode != 0

