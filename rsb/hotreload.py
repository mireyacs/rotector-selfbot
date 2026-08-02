"""Reloading edited modules without restarting, and without losing a scan.

Editing a renderer or a verdict rule mid-scan and having to throw away twenty
minutes of member fetching to see the change is the problem this solves. Scan
state lives on the app instance and in the client, neither of which is touched;
what gets swapped is the code behind them.

    Reloading has real limits, and they are worth stating plainly.

    * **Functions and constants pick up changes; live objects do not.** An
      object built before a reload keeps the class it was built from. Editing a
      method body affects the next object created, not the ones already on
      screen.
    * **The UI modules are excluded.** Reloading the class of a running Textual
      app mid-frame does not end well, so :data:`PINNED` never reloads.
    * **Re-binding is best effort.** ``from x import y`` copies a reference, so
      after reloading ``x`` every module that imported ``y`` still points at the
      old function. Those references are hunted down and repointed, which
      covers ordinary imports but not names captured in closures or defaults.

For anything beyond that, restart. This is a fast edit loop, not a promise of
correctness.
"""

from __future__ import annotations

import importlib
import importlib.util
import sys
from dataclasses import dataclass, field
from pathlib import Path
from types import FunctionType, ModuleType

#: modules whose reload would break a running app
PINNED = {
    "rsb.tui.app",
    "rsb.tui.commands",
    "rsb.tui.dialogs",
    "rsb.tui.settings",
    "rsb.tui.proxies",
    "rsb.hotreload",
    "rsb.__main__",
}

#: reload order, so a module is refreshed after anything it depends on
ORDER = [
    "rsb.verdict",
    "rsb.ratelimit",
    "rsb.eta",
    "rsb.proxy",
    "rsb.rotector",
    "rsb.export",
    "rsb.imagerender",
    "rsb.moderation",
    "rsb.purge",
    "rsb.migrate",
    "rsb.config",
    "rsb.sources",
    "rsb.discord.http",
    "rsb.discord.gateway",
]


@dataclass
class ReloadReport:
    reloaded: list[str] = field(default_factory=list)
    rebound: int = 0
    skipped: list[str] = field(default_factory=list)
    failed: list[tuple[str, str]] = field(default_factory=list)

    @property
    def changed(self) -> bool:
        return bool(self.reloaded or self.failed)

    def describe(self) -> str:
        if self.failed:
            names = ", ".join(f"{n} ({e})" for n, e in self.failed)
            return f"Reload failed for {names}"
        if not self.reloaded:
            return "Nothing has changed on disk."
        return (
            f"Reloaded {len(self.reloaded)}: {', '.join(self.reloaded)}"
            + (f"  ({self.rebound} references repointed)" if self.rebound else "")
        )


class HotReloader:
    """Watches source mtimes and reloads what has changed."""

    def __init__(
        self,
        pinned: set[str] | None = None,
        prefixes: tuple[str, ...] = ("rsb",),
    ) -> None:
        self.pinned = set(PINNED if pinned is None else pinned)
        self.prefixes = prefixes
        self._stamps: dict[str, float] = {}
        self.snapshot()

    # -- change detection --------------------------------------------------

    def _watched(self) -> list[tuple[str, ModuleType]]:
        out = []
        for name, module in list(sys.modules.items()):
            if not any(
                name == prefix or name.startswith(f"{prefix}.")
                for prefix in self.prefixes
            ):
                continue
            if name in self.pinned or module is None:
                continue
            if getattr(module, "__file__", None):
                out.append((name, module))
        return out

    @staticmethod
    def _mtime(module: ModuleType) -> float:
        try:
            return Path(module.__file__).stat().st_mtime
        except (OSError, TypeError):
            return 0.0

    def snapshot(self) -> None:
        """Record current mtimes, so only later edits count as changes."""
        for name, module in self._watched():
            self._stamps[name] = self._mtime(module)

    def changed(self) -> list[str]:
        out = []
        for name, module in self._watched():
            stamp = self._mtime(module)
            if stamp and stamp > self._stamps.get(name, 0.0):
                out.append(name)
        return out

    # -- reloading ---------------------------------------------------------

    def reload(self, names: list[str] | None = None) -> ReloadReport:
        report = ReloadReport()
        targets = names if names is not None else self.changed()
        if not targets:
            return report

        # dependency order first, then anything else that changed
        ordered = [n for n in ORDER if n in targets]
        ordered += [n for n in targets if n not in ordered]

        importlib.invalidate_caches()

        replacements: dict[str, ModuleType] = {}
        for name in ordered:
            if name in self.pinned:
                report.skipped.append(name)
                continue
            module = sys.modules.get(name)
            if module is None:
                continue
            _drop_bytecode(module)
            try:
                replacements[name] = importlib.reload(module)
                report.reloaded.append(name)
                self._stamps[name] = self._mtime(replacements[name])
            except Exception as exc:  # noqa: BLE001 - reported, not raised
                report.failed.append((name, f"{type(exc).__name__}: {exc}"))

        if replacements:
            report.rebound = rebind(replacements)
        return report


def _drop_bytecode(module: ModuleType) -> None:
    """Delete a module's cached bytecode so the reload really recompiles.

    The cache decides a .pyc is current by comparing the source's size and its
    mtime *truncated to whole seconds*. Two saves inside the same second, with
    the same length, are therefore indistinguishable to it -- and the reload
    quietly runs the previous version. Editing a file, undoing, and editing
    again is exactly that pattern, so this is not a corner case.
    """
    source = getattr(module, "__file__", None)
    if not source:
        return
    try:
        cached = importlib.util.cache_from_source(source)
    except (NotImplementedError, ValueError):
        return
    try:
        Path(cached).unlink()
    except OSError:
        pass


def rebind(replacements: dict[str, ModuleType]) -> int:
    """Repoint ``from x import y`` references at the reloaded objects.

    Reloading a module leaves every importer holding the *old* function, so
    without this a reload appears to do nothing wherever the name was imported
    directly -- which is almost everywhere.
    """
    count = 0
    for module_name, module in list(sys.modules.items()):
        if module_name.startswith("importlib") or module_name.startswith("_"):
            continue
        if module is None or module_name in replacements:
            continue
        for attr, value in list(vars(module).items()):
            origin = getattr(value, "__module__", None)
            if origin not in replacements:
                continue
            if not isinstance(value, (FunctionType, type)):
                continue
            fresh = getattr(replacements[origin], attr, None)
            if fresh is not None and fresh is not value:
                try:
                    setattr(module, attr, fresh)
                    count += 1
                except Exception:
                    pass
    return count
