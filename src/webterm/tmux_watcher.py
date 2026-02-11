"""tmux session/window watcher for dynamic dashboard updates."""

from __future__ import annotations

import asyncio
import contextlib
import logging
import subprocess
from typing import TYPE_CHECKING, Callable

from .slugify import slugify

if TYPE_CHECKING:
    from .session_manager import SessionManager

log = logging.getLogger("webterm")


class TmuxWatcher:
    """Watch tmux sessions/windows and manage terminal sessions dynamically."""

    def __init__(
        self,
        session_manager: SessionManager,
        poll_interval: float = 2.0,
        on_change: Callable[[], None] | None = None,
    ) -> None:
        self._session_manager = session_manager
        self._poll_interval = poll_interval
        self._on_change = on_change
        self._running = False
        self._task: asyncio.Task | None = None
        self._managed_windows: dict[str, str] = {}
        self._tmux_missing = False

    async def start(self) -> None:
        if self._running:
            return
        self._running = True
        self._task = asyncio.create_task(self._poll_loop())

    async def stop(self) -> None:
        self._running = False
        if self._task:
            self._task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self._task

    def _run_tmux(self, args: list[str]) -> tuple[int, str]:
        try:
            result = subprocess.run(
                ["tmux", *args],
                capture_output=True,
                text=True,
                check=False,
            )
        except FileNotFoundError:
            if not self._tmux_missing:
                log.error("tmux not found; disabling tmux watch mode")
            self._tmux_missing = True
            self._running = False
            return 127, ""
        return result.returncode, result.stdout

    def _list_sessions(self) -> list[str]:
        code, out = self._run_tmux(["list-sessions", "-F", "#{session_name}"])
        if code != 0:
            return []
        return [line.strip() for line in out.splitlines() if line.strip()]

    def _list_windows(self, session: str) -> list[dict[str, str]]:
        code, out = self._run_tmux(
            ["list-windows", "-t", session, "-F", "#{window_index}|#{window_name}|#{window_id}"]
        )
        if code != 0:
            return []
        windows: list[dict[str, str]] = []
        for line in out.splitlines():
            line = line.strip()
            if not line:
                continue
            parts = line.split("|", 2)
            if len(parts) != 3:
                continue
            index, name, window_id = parts
            windows.append(
                {
                    "session": session,
                    "index": index,
                    "name": name,
                    "window_id": window_id,
                }
            )
        return windows

    async def _poll_loop(self) -> None:
        while self._running:
            try:
                await self._sync()
            except Exception as exc:  # pragma: no cover - defensive
                log.exception("tmux watch error: %s", exc)
            await asyncio.sleep(self._poll_interval)

    async def _sync(self) -> None:
        sessions = self._list_sessions()
        current_windows: dict[str, dict[str, str]] = {}

        for session in sessions:
            for window in self._list_windows(session):
                window_id = window["window_id"]
                slug = slugify(f"{session}-{window_id}")
                window["slug"] = slug
                current_windows[slug] = window

        # Add or update windows
        for slug, window in current_windows.items():
            name = f"{window['index']}: {window['name']}"
            command = f"tmux attach-session -t {window['session']}:{window['index']}"
            group = window["session"]

            if slug in self._managed_windows:
                app = self._session_manager.apps_by_slug.get(slug)
                if app is not None:
                    app.name = name
                    app.command = command
                    app.group = group
                    app.tmux_session = window["session"]
                    app.tmux_window = window["index"]
                continue

            self._managed_windows[slug] = window["window_id"]
            self._session_manager.add_app(
                name=name,
                command=command,
                slug=slug,
                terminal=True,
                group=group,
                tmux_session=window["session"],
                tmux_window=window["index"],
            )

        # Remove windows that disappeared
        removed = [slug for slug in list(self._managed_windows) if slug not in current_windows]
        for slug in removed:
            del self._managed_windows[slug]
            app = self._session_manager.apps_by_slug.pop(slug, None)
            if app and app in self._session_manager.apps:
                self._session_manager.apps.remove(app)
            session = self._session_manager.get_session_by_route_key(slug)
            if session:
                session_id = self._session_manager.routes.get(slug)
                if session_id:
                    await self._session_manager.close_session(session_id)

        if (current_windows or removed) and self._on_change:
            self._on_change()
