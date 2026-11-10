from __future__ import annotations

import asyncio
import json
import os
from pathlib import Path
import subprocess
import sys
import threading

import pytest

pytest.importorskip("mcp.server.fastmcp")

from oce_client.mcp_server import (
    build_parser as build_mcp_parser,
    create_server,
    mcp_configuration_from_args,
)
from oce_client.runtime import ClientConfigurationError, ClientSettings, McpConfiguration


def _call(server, name: str, arguments: dict[str, object]):
    return asyncio.run(server._tool_manager.call_tool(name, arguments))


def _stop(server) -> None:
    for indexer in server._oce_indexers.values():
        indexer.stop()


class FakeContext:
    def __init__(self, calls: list[str], sync_gate: threading.Event | None = None):
        self.calls = calls
        self.sync_gate = sync_gate

    def sync(self):
        self.calls.append("sync")
        if self.sync_gate is not None:
            self.sync_gate.wait(5)

    def sync_paths(self, paths: set[Path]):
        self.calls.append(f"sync_paths:{len(paths)}")

    def retrieve(self, information_request: str):
        self.calls.append(f"retrieve:{information_request}")
        return type(
            "Result",
            (),
            {"formatted_retrieval": "retrieved context", "elapsed_ms": 7},
        )()


class FakeRuntime:
    def __init__(
        self,
        settings: ClientSettings,
        calls: list[str],
        sync_gate: threading.Event | None = None,
    ):
        self.settings = settings
        self.calls = calls
        self._context = FakeContext(calls, sync_gate)

    def context(self) -> FakeContext:
        self.calls.append(f"context:{self.settings.root}")
        return self._context

    def close(self) -> None:
        self.calls.append(f"close:{self.settings.root}")


class FailingOnceContext(FakeContext):
    def __init__(self, calls: list[str]):
        super().__init__(calls)
        self.failed = False

    def sync_paths(self, paths: set[Path]):
        self.calls.append(f"sync_paths:{len(paths)}")
        if not self.failed:
            self.failed = True
            raise RuntimeError("incremental sync failed")


class FailingOnceRuntime(FakeRuntime):
    def __init__(self, settings: ClientSettings, calls: list[str]):
        super().__init__(settings, calls)
        self._context = FailingOnceContext(calls)


def test_mcp_exposes_only_codebase_retrieval_and_indexes_in_background(tmp_path: Path):
    calls: list[str] = []

    def factory(settings: ClientSettings) -> FakeRuntime:
        return FakeRuntime(settings, calls)

    server = create_server(
        ClientSettings(tmp_path, "http://oce.test", "test-key"),
        initial_sync="off",
        ready_timeout=1,
        runtime_factory=factory,
    )
    try:
        tools = asyncio.run(server.list_tools())
        assert {tool.name for tool in tools} == {"codebase-retrieval"}
        schema = tools[0].inputSchema
        assert schema["required"] == ["information_request"]
        assert schema["properties"]["information_request"]["description"] == (
            "A description of the information you need."
        )
        assert schema["properties"]["workspace_folder"]["description"].startswith(
            "Path to the workspace folder"
        )

        result = _call(server, "codebase-retrieval", {"information_request": "find x"})
        assert result == {
            "status": "ready",
            "workspace_folder": str(tmp_path.resolve()),
            "formatted_retrieval": "retrieved context",
            "elapsed_ms": 7,
        }
        assert calls[:2] == [f"context:{tmp_path.resolve()}", "sync"]
        assert calls[-2:] == [f"context:{tmp_path.resolve()}", "retrieve:find x"]

        indexer = server._oce_indexers[tmp_path.resolve()]
        indexer.notify_changes({tmp_path / "changed.py"})
        assert indexer.wait_until_ready(1) == "ready"
        assert "sync_paths:1" in calls
        assert calls.count("sync") == 1
    finally:
        _stop(server)


def test_mcp_returns_indexing_when_initial_sync_exceeds_timeout(tmp_path: Path):
    calls: list[str] = []
    sync_gate = threading.Event()

    def factory(settings: ClientSettings) -> FakeRuntime:
        return FakeRuntime(settings, calls, sync_gate)

    server = create_server(
        ClientSettings(tmp_path, "http://oce.test", "test-key"),
        initial_sync="off",
        ready_timeout=0.01,
        runtime_factory=factory,
    )
    try:
        result = _call(server, "codebase-retrieval", {"information_request": "find x"})
        assert result["status"] == "indexing"
        assert "formatted_retrieval" not in result
        sync_gate.set()
        server._oce_indexers[tmp_path.resolve()].wait_until_ready(1)
        result = _call(server, "codebase-retrieval", {"information_request": "find x"})
        assert result["status"] == "ready"
    finally:
        sync_gate.set()
        _stop(server)


def test_mcp_recovers_with_full_sync_after_incremental_failure(tmp_path: Path):
    calls: list[str] = []

    def factory(settings: ClientSettings) -> FakeRuntime:
        return FailingOnceRuntime(settings, calls)

    server = create_server(
        ClientSettings(tmp_path, "http://oce.test", "test-key"),
        initial_sync="off",
        ready_timeout=1,
        runtime_factory=factory,
    )
    try:
        first = _call(server, "codebase-retrieval", {"information_request": "find x"})
        assert first["status"] == "ready"

        indexer = server._oce_indexers[tmp_path.resolve()]
        indexer.notify_changes({tmp_path / "first.py"})
        assert indexer.wait_until_ready(1) == "error"

        indexer.notify_changes({tmp_path / "second.py"})
        assert indexer.wait_until_ready(1) == "ready"
        assert calls.count("sync") == 2
        assert calls.count("sync_paths:1") == 1
    finally:
        _stop(server)


def test_mcp_does_not_queue_ignored_file_changes(tmp_path: Path):
    calls: list[str] = []
    (tmp_path / ".gitignore").write_text("ignored.py\n", encoding="utf-8")

    def factory(settings: ClientSettings) -> FakeRuntime:
        return FakeRuntime(settings, calls)

    server = create_server(
        ClientSettings(tmp_path, "http://oce.test", "test-key"),
        initial_sync="off",
        ready_timeout=1,
        runtime_factory=factory,
    )
    try:
        result = _call(server, "codebase-retrieval", {"information_request": "find x"})
        assert result["status"] == "ready"

        indexer = server._oce_indexers[tmp_path.resolve()]
        before = indexer.status()["requested_generation"]
        indexer.notify_changes({tmp_path / "ignored.py"})
        assert indexer.status()["requested_generation"] == before
        assert not any(call.startswith("sync_paths:") for call in calls)
    finally:
        _stop(server)


def test_mcp_enforces_configured_workspace_allowlist(tmp_path: Path):
    calls: list[str] = []
    workspace = tmp_path / "workspace"
    outside = tmp_path / "outside"
    workspace.mkdir()
    outside.mkdir()

    def factory(settings: ClientSettings) -> FakeRuntime:
        return FakeRuntime(settings, calls)

    server = create_server(
        ClientSettings(tmp_path, "http://oce.test", "test-key"),
        workspace_roots=(tmp_path, workspace),
        initial_sync="off",
        ready_timeout=1,
        runtime_factory=factory,
    )
    try:
        with pytest.raises(Exception, match="workspace_folder is required"):
            _call(server, "codebase-retrieval", {"information_request": "find x"})
        with pytest.raises(Exception, match="workspace_folder is not configured"):
            _call(
                server,
                "codebase-retrieval",
                {"information_request": "find x", "workspace_folder": str(outside)},
            )
        result = _call(
            server,
            "codebase-retrieval",
            {"information_request": "find x", "workspace_folder": str(workspace)},
        )
        assert result["status"] == "ready"
        assert result["workspace_folder"] == str(workspace.resolve())
    finally:
        _stop(server)


def test_mcp_stdio_initialize_and_list_tools(tmp_path: Path):
    requests = [
        {
            "jsonrpc": "2.0",
            "id": 1,
            "method": "initialize",
            "params": {
                "protocolVersion": "2025-03-26",
                "capabilities": {},
                "clientInfo": {"name": "oce-client-test", "version": "1"},
            },
        },
        {"jsonrpc": "2.0", "method": "notifications/initialized", "params": {}},
        {"jsonrpc": "2.0", "id": 2, "method": "tools/list", "params": {}},
    ]
    environment = os.environ.copy()
    environment["OCE_API_KEY"] = "unused-in-test"
    process = subprocess.Popen(
        [
            sys.executable,
            "-m",
            "oce_client.mcp_server",
            "--workspace",
            str(tmp_path),
            "--initial-sync",
            "off",
        ],
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        env=environment,
    )
    responses: list[dict[str, object]] = []
    tools_response = threading.Event()

    def read_responses() -> None:
        assert process.stdout is not None
        for line in process.stdout:
            if not line.strip():
                continue
            response = json.loads(line)
            responses.append(response)
            if response.get("id") == 2:
                tools_response.set()

    reader = threading.Thread(target=read_responses, daemon=True)
    reader.start()
    try:
        assert process.stdin is not None
        process.stdin.write("\n".join(json.dumps(request) for request in requests) + "\n")
        process.stdin.flush()
        assert tools_response.wait(10), "MCP stdio server did not answer tools/list"
    finally:
        if process.stdin is not None:
            process.stdin.close()
        try:
            process.wait(timeout=5)
        except subprocess.TimeoutExpired:
            process.terminate()
            process.wait(timeout=5)
        reader.join(timeout=5)

    responses_by_id = {response.get("id"): response for response in responses}
    assert responses_by_id[1]["result"]["serverInfo"]["name"] == "oce-client"
    tools = responses_by_id[2]["result"]["tools"]
    assert {tool["name"] for tool in tools} == {"codebase-retrieval"}
    schema = tools[0]["inputSchema"]
    assert schema["required"] == ["information_request"]


def test_mcp_configuration_has_one_precedence_chain(tmp_path: Path, monkeypatch):
    env_workspace = tmp_path / "from-env"
    cli_workspace = tmp_path / "from-cli"
    env_workspace.mkdir()
    cli_workspace.mkdir()
    monkeypatch.setenv("OCE_WORKSPACE", str(env_workspace))
    monkeypatch.setenv("OCE_API_URL", "http://env.test")
    monkeypatch.setenv("OCE_DEBOUNCE_MS", "900")
    monkeypatch.setenv("OCE_INITIAL_SYNC", "blocking")
    monkeypatch.setenv("OCE_READY_TIMEOUT", "4.5")
    monkeypatch.setenv("OCE_LOG_LEVEL", "info")
    monkeypatch.setenv("OCE_IGNORE", "env-a, env-b")

    args = build_mcp_parser().parse_args(
        [
            "--workspace",
            str(cli_workspace),
            "--api-url",
            "http://cli.test",
            "--debounce-ms",
            "250",
            "--initial-sync",
            "off",
            "--ready-timeout",
            "1.25",
            "--log-level",
            "error",
            "--ignore",
            "cli-a,cli-b",
        ]
    )
    config = mcp_configuration_from_args(args)
    assert config.workspace_roots == (cli_workspace.resolve(),)
    assert config.client.api_url == "http://cli.test"
    assert config.debounce_ms == 250
    assert config.initial_sync == "off"
    assert config.ready_timeout == 1.25
    assert config.log_level == "error"
    assert config.client.runtime_patterns == ("cli-a", "cli-b")


def test_mcp_configuration_loads_environment_defaults(tmp_path: Path, monkeypatch):
    monkeypatch.setenv("OCE_WORKSPACE", str(tmp_path))
    monkeypatch.setenv("OCE_API_URL", "http://env.test/")
    monkeypatch.setenv("OCE_IGNORE", "*.cache\n*.generated.py")
    monkeypatch.setenv("OCE_DEBOUNCE_MS", "750")
    monkeypatch.setenv("OCE_INITIAL_SYNC", "off")
    monkeypatch.setenv("OCE_READY_TIMEOUT", "2.25")
    monkeypatch.setenv("OCE_LOG_LEVEL", "WARNING")

    config = McpConfiguration.from_environment()
    assert config.workspace_roots == (tmp_path.resolve(),)
    assert config.client.api_url == "http://env.test"
    assert config.client.runtime_patterns == ("*.cache", "*.generated.py")
    assert config.debounce_ms == 750
    assert config.initial_sync == "off"
    assert config.ready_timeout == 2.25
    assert config.log_level == "warning"


def test_standalone_mcp_without_workspace_falls_back_to_cwd(
    tmp_path: Path, monkeypatch
):
    monkeypatch.delenv("OCE_WORKSPACE", raising=False)
    monkeypatch.delenv("OCE_WORKSPACES", raising=False)
    monkeypatch.chdir(tmp_path)
    config = McpConfiguration.from_environment()
    assert config.workspace_roots == (tmp_path.resolve(),)


def test_mcp_cwd_fallback_refuses_filesystem_root(monkeypatch):
    from oce_client.mcp_server import main
    from oce_client.runtime import _resolve_mcp_roots

    monkeypatch.delenv("OCE_WORKSPACE", raising=False)
    monkeypatch.delenv("OCE_WORKSPACES", raising=False)
    monkeypatch.chdir("/")
    with pytest.raises(ClientConfigurationError, match="refusing to index"):
        _resolve_mcp_roots(None)
    # 独立入口在同样条件下应干净退出而不是挂起或误索引
    assert main(["--initial-sync", "off"]) == 1


def test_mcp_explicit_workspace_still_wins_over_cwd(tmp_path: Path, monkeypatch):
    monkeypatch.delenv("OCE_WORKSPACE", raising=False)
    monkeypatch.delenv("OCE_WORKSPACES", raising=False)
    monkeypatch.chdir(tmp_path)
    explicit = tmp_path / "explicit"
    explicit.mkdir()
    config = McpConfiguration.from_environment(workspace_roots=(explicit,))
    assert config.workspace_roots == (explicit.resolve(),)


def test_mcp_rejects_ambiguous_state_configuration(tmp_path: Path):
    with pytest.raises(ClientConfigurationError, match="state configuration is ambiguous"):
        McpConfiguration.from_environment(
            workspace_roots=(tmp_path,),
            state_path=tmp_path / "state.sqlite3",
            state_dir=tmp_path / "states",
        )
