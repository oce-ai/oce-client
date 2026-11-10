from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path
from collections.abc import Iterable, Sequence

from .context import WorkspaceContext
from .defaults import DEFAULT_API_KEY, DEFAULT_API_URL
from .http import OceHttpClient


class ClientConfigurationError(ValueError):
    """Raised when a CLI/MCP runtime cannot be configured safely."""


@dataclass(frozen=True)
class ClientSettings:
    root: Path
    api_url: str
    api_key: str
    state_path: Path | None = None
    runtime_patterns: tuple[str, ...] = ()

    @classmethod
    def from_environment(
        cls,
        *,
        root: str | os.PathLike[str] | None = None,
        api_url: str | None = None,
        api_key: str | None = None,
        state_path: str | os.PathLike[str] | None = None,
        runtime_patterns: Iterable[str] | None = None,
        require_api_key: bool = True,
    ) -> "ClientSettings":
        resolved_root = Path(
            root if root is not None else os.environ.get("OCE_WORKSPACE", ".")
        ).expanduser().resolve()
        resolved_url = (
            api_url if api_url is not None else os.environ.get("OCE_API_URL", DEFAULT_API_URL)
        ).strip()
        resolved_key = api_key if api_key is not None else os.environ.get(
            "OCE_API_KEY", DEFAULT_API_KEY
        )
        resolved_state = _resolve_path_option(state_path, "OCE_STATE_PATH")
        if not resolved_url:
            raise ClientConfigurationError("OCE API URL must not be empty")
        if require_api_key and not resolved_key:
            raise ClientConfigurationError(
                "OCE API key is required; set OCE_API_KEY"
            )
        return cls(
            root=resolved_root,
            api_url=resolved_url.rstrip("/"),
            api_key=resolved_key,
            state_path=resolved_state,
            runtime_patterns=tuple(
                runtime_patterns
                if runtime_patterns is not None
                else iter_runtime_patterns((os.environ.get("OCE_IGNORE", ""),))
            ),
        )


@dataclass(frozen=True)
class McpConfiguration:
    """Fully resolved configuration for the long-running MCP process."""

    client: ClientSettings
    workspace_roots: tuple[Path, ...]
    state_dir: Path | None
    debounce_ms: int
    initial_sync: str
    ready_timeout: float
    log_level: str

    @classmethod
    def from_environment(
        cls,
        *,
        workspace_roots: Sequence[str | os.PathLike[str]] | None = None,
        api_url: str | None = None,
        state_path: str | os.PathLike[str] | None = None,
        state_dir: str | os.PathLike[str] | None = None,
        runtime_patterns: Iterable[str] | None = None,
        debounce_ms: int | None = None,
        initial_sync: str | None = None,
        ready_timeout: float | None = None,
        log_level: str | None = None,
    ) -> "McpConfiguration":
        roots = _resolve_mcp_roots(workspace_roots)

        resolved_state_dir = _resolve_path_option(state_dir, "OCE_STATE_DIR")
        client = ClientSettings.from_environment(
            root=roots[0],
            api_url=api_url,
            state_path=state_path,
            runtime_patterns=runtime_patterns,
            require_api_key=True,
        )
        if client.state_path is not None and resolved_state_dir is not None:
            raise ClientConfigurationError(
                "MCP state configuration is ambiguous; choose OCE_STATE_PATH/--state-path "
                "or OCE_STATE_DIR/--state-dir"
            )
        if len(roots) > 1 and client.state_path is not None and resolved_state_dir is None:
            raise ClientConfigurationError(
                "multiple MCP workspaces cannot share OCE_STATE_PATH; use --state-dir "
                "or OCE_STATE_DIR"
            )

        resolved_debounce = _resolve_int_option(
            debounce_ms, "OCE_DEBOUNCE_MS", 500
        )
        if resolved_debounce < 0:
            raise ClientConfigurationError("debounce-ms must not be negative")
        resolved_initial = (
            initial_sync
            if initial_sync is not None
            else os.environ.get("OCE_INITIAL_SYNC", "background")
        ).strip().lower()
        if resolved_initial not in {"background", "blocking", "off"}:
            raise ClientConfigurationError(
                "initial-sync must be one of: background, blocking, off"
            )
        resolved_timeout = _resolve_float_option(
            ready_timeout, "OCE_READY_TIMEOUT", 3.0
        )
        if resolved_timeout < 0:
            raise ClientConfigurationError("ready-timeout must not be negative")
        resolved_log_level = (
            log_level
            if log_level is not None
            else os.environ.get("OCE_LOG_LEVEL", "warning")
        ).strip().lower()
        if resolved_log_level not in {"debug", "info", "warning", "error", "critical"}:
            raise ClientConfigurationError(
                "log-level must be one of: debug, info, warning, error, critical"
            )
        return cls(
            client=client,
            workspace_roots=roots,
            state_dir=resolved_state_dir,
            debounce_ms=resolved_debounce,
            initial_sync=resolved_initial,
            ready_timeout=resolved_timeout,
            log_level=resolved_log_level,
        )


class ClientRuntime:
    """Lazy, closeable context shared by one CLI command or MCP process."""

    def __init__(self, settings: ClientSettings) -> None:
        self.settings = settings
        self._context: WorkspaceContext | None = None

    def context(self) -> WorkspaceContext:
        if self._context is None:
            if not self.settings.root.is_dir():
                raise ClientConfigurationError(
                    f"workspace is not a directory: {self.settings.root}"
                )
            api = OceHttpClient(self.settings.api_url, self.settings.api_key)
            try:
                self._context = WorkspaceContext.open(
                    self.settings.root,
                    api,
                    state_path=self.settings.state_path,
                    runtime_patterns=self.settings.runtime_patterns,
                )
            except Exception:
                api.close()
                raise
        return self._context

    def close(self) -> None:
        if self._context is not None:
            self._context.close()
            self._context = None

    def __enter__(self) -> "ClientRuntime":
        return self

    def __exit__(self, *_: object) -> None:
        self.close()


def iter_runtime_patterns(values: Iterable[str]) -> tuple[str, ...]:
    patterns: list[str] = []
    for value in values:
        for comma_group in value.replace("\r", "\n").split(","):
            patterns.extend(
                line.strip() for line in comma_group.splitlines() if line.strip()
            )
    return tuple(patterns)


def _resolve_path_option(
    value: str | os.PathLike[str] | None,
    environment_name: str,
) -> Path | None:
    raw = value if value is not None else os.environ.get(environment_name)
    return None if raw is None or not str(raw).strip() else Path(raw).expanduser().resolve()


def _resolve_mcp_roots(
    values: Sequence[str | os.PathLike[str]] | None,
) -> tuple[Path, ...]:
    if values is None:
        raw_values: Sequence[str | os.PathLike[str]]
        configured = os.environ.get("OCE_WORKSPACES")
        if configured:
            raw_values = tuple(part for part in configured.split(os.pathsep) if part)
        else:
            single = os.environ.get("OCE_WORKSPACE")
            raw_values = () if single is None or not single.strip() else (single,)
    else:
        raw_values = values
    roots = tuple(dict.fromkeys(Path(value).expanduser().resolve() for value in raw_values))
    if roots:
        return roots
    # 零配置回退：MCP 宿主（编码代理等）以项目目录为工作目录拉起本进程，
    # 未显式配置工作区时，把当前目录当作项目根——agent 在哪个项目运行，
    # 哪个路径就是项目地址，无需手动配置。
    fallback = Path.cwd().resolve()
    if fallback == Path(fallback.anchor):
        raise ClientConfigurationError(
            "MCP requires at least one workspace; pass --workspace or set "
            "OCE_WORKSPACE/OCE_WORKSPACES (refusing to index the filesystem "
            "root as an implicit fallback)"
        )
    return (fallback,)


def _resolve_int_option(value: int | None, environment_name: str, default: int) -> int:
    raw = value if value is not None else os.environ.get(environment_name)
    if raw is None:
        return default
    try:
        return int(raw)
    except (TypeError, ValueError) as exc:
        raise ClientConfigurationError(f"{environment_name} must be an integer") from exc


def _resolve_float_option(value: float | None, environment_name: str, default: float) -> float:
    raw = value if value is not None else os.environ.get(environment_name)
    if raw is None:
        return default
    try:
        return float(raw)
    except (TypeError, ValueError) as exc:
        raise ClientConfigurationError(f"{environment_name} must be a number") from exc
