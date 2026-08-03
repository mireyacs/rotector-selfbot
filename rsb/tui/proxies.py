"""Interactive proxy tester: `python -m rsb proxies`.

Every proxy is probed against the Rotector API itself, so what is measured is
exactly what the scanner depends on: can this proxy reach Rotector, how fast,
and does it bring a rate budget of its own?

That last question is answered from Rotector's own ``X-RateLimit-*`` headers
rather than from any IP-echo service.  The probe is a single request, so an
exit reporting more than one request already spent this window is sharing its
budget with something else -- another proxy in the pool behind the same exit,
or unrelated traffic from that IP.  Those are marked SHARED: they look like
extra capacity but are not.
"""

from __future__ import annotations

import asyncio
import time
from pathlib import Path

from rich.text import Text
from textual import on, work
from textual.app import App, ComposeResult
from textual.containers import Vertical, VerticalScroll
from textual.widgets import DataTable, Footer, Header, Input, ProgressBar, Static

from ..config import Config
from ..eta import format_duration
from ..proxy import DIRECT_NAME, ProbeResult, parse_proxy, probe_proxy, summarise_pool
from .theme import register as register_theme

_VERDICT_STYLE = {
    "OK": "bold green",
    "SHARED": "yellow",
    "NO API": "yellow",
    "FAIL": "bold red",
}


class ProxyTesterApp(App):
    TITLE = "rotector-selfbot"
    SUB_TITLE = "proxy tester"

    CSS = """
    #head { height: auto; padding: 0 1; background: $panel-darken-1; }
    #table { height: 1fr; }
    #detail {
        height: 9;
        border-top: solid $panel-lighten-2;
        padding: 0 1;
        background: $surface;
    }
    #entry { display: none; }
    #entry.visible { display: block; }
    #status { height: 1; background: $panel; color: $text-muted; padding: 0 1; }
    #progress { height: 1; display: none; }
    #progress.visible { display: block; }
    """

    BINDINGS = [
        ("t", "test_all", "Test all"),
        ("r", "retest", "Retest row"),
        ("a", "add", "Add proxy"),
        ("d", "delete", "Remove row"),
        ("s", "save", "Save working"),
        ("x", "stop", "Stop"),
        ("escape", "close_entry", ""),
        ("q,ctrl+c", "quit", "Quit"),
    ]

    def __init__(self, config: Config) -> None:
        super().__init__()
        self.config = config
        self.entries: list[str] = []
        self.results: dict[str, ProbeResult] = {}
        # (window reset, probes we made) per entry, so a retest inside the same
        # window is not mistaken for someone else using the exit's budget
        self._own_probes: dict[str, tuple[float, int]] = {}
        self._status_text = ""

    # -- layout ------------------------------------------------------------

    def compose(self) -> ComposeResult:
        yield Header(show_clock=True)
        with Vertical():
            yield Static("", id="head")
            yield Input(placeholder="host:port  or  scheme://user:pass@host:port", id="entry")
            yield DataTable(id="table", cursor_type="row", zebra_stripes=True)
            with VerticalScroll(id="detail"):
                yield Static(self._intro(), id="detail-body")
        yield ProgressBar(id="progress", show_eta=False)
        yield Static("", id="status")
        yield Footer()

    def on_mount(self) -> None:
        register_theme(self)

        table = self.query_one("#table", DataTable)
        table.add_column("Proxy", width=32)
        table.add_column("Status", width=8)
        table.add_column("Latency", width=9)
        table.add_column("HTTP", width=6)
        table.add_column("Budget", width=8)
        table.add_column("Used", width=6)
        table.add_column("Note", width=42)

        self.entries = self.config.proxy_urls()
        # the direct connection is a route too; worth knowing if it works
        if DIRECT_NAME not in self.entries:
            self.entries.insert(0, DIRECT_NAME)
        self._rebuild()
        self._refresh_head()

        if len(self.entries) > 1:
            self._set_status("Press 't' to test every proxy.")
        else:
            self._set_status(
                f"No proxies found. Add one with 'a', or list them in "
                f"{self.config.proxy.file}."
            )
        table.focus()

    def _intro(self) -> Text:
        text = Text()
        text.append("Proxy tester\n\n", style="bold")
        text.append(
            "Every proxy is tested against the Rotector API itself - no "
            "third-party IP service is involved, so a proxy that reaches the "
            "internet but is blocked by Rotector is reported as useless rather "
            "than as working.\n\n"
        )
        text.append(
            "Rotector's own rate-limit headers reveal whether a proxy brings a "
            "fresh budget. Our probe is one request; if the exit reports more "
            "than one already spent this window, something else is drawing on "
            "the same budget and the proxy is marked ",
        )
        text.append("SHARED", style="yellow")
        text.append(" - it adds far less capacity than it appears to.\n\n")
        text.append("Rotector's Terms of Use prohibit ", style="yellow")
        text.append("circumventing rate limits by rotating IPs", style="bold yellow")
        text.append(
            ". An API key from panel.rotector.com raises the limit on a single "
            "connection and is the sanctioned way to go faster.",
            style="yellow",
        )
        return text

    # -- table -------------------------------------------------------------

    def _cells(self, entry: str) -> list[Text]:
        result = self.results.get(entry)
        label = DIRECT_NAME if entry == DIRECT_NAME else entry
        if result is None:
            return [
                Text(label, overflow="ellipsis"),
                Text("-", style="dim"),
                Text("-", style="dim", justify="right"),
                Text("-", style="dim", justify="right"),
                Text("-", style="dim", justify="right"),
                Text("-", style="dim", justify="right"),
                Text("not tested", style="dim", overflow="ellipsis"),
            ]

        note = result.error or ("; ".join(result.notes) if result.notes else "")
        healthy = result.status is not None and result.status < 400
        used = result.used_in_window
        return [
            Text(result.label or label, overflow="ellipsis"),
            Text(result.verdict, style=_VERDICT_STYLE.get(result.verdict, "")),
            Text(
                f"{result.latency_ms:.0f}ms" if result.latency_ms else "-",
                justify="right",
            ),
            Text(
                str(result.status) if result.status is not None else "-",
                style="green" if healthy else "red",
                justify="right",
            ),
            Text(
                f"{result.rate_limit}" if result.rate_limit else "-",
                justify="right",
            ),
            Text(
                str(used) if used is not None else "-",
                style="yellow" if (used or 0) > 1 else "",
                justify="right",
            ),
            Text(note, overflow="ellipsis", style="" if result.ok else "red"),
        ]

    def _rebuild(self) -> None:
        table = self.query_one("#table", DataTable)
        table.clear()
        for entry in self.entries:
            table.add_row(*self._cells(entry), key=entry)

    def _update_row(self, entry: str) -> None:
        table = self.query_one("#table", DataTable)
        try:
            for column, cell in zip(table.columns, self._cells(entry)):
                table.update_cell(entry, column, cell)
        except Exception:
            self._rebuild()

    def _refresh_head(self) -> None:
        tested = list(self.results.values())
        proxies = [e for e in self.entries if e != DIRECT_NAME]

        text = Text()
        text.append(f"{len(proxies)} proxies", style="bold")
        if tested:
            stats = summarise_pool(tested)
            text.append("   ")
            text.append(f"OK {stats['independent']}", style="bold green")
            text.append("  ")
            text.append(f"SHARED {stats['shared']}", style="yellow")
            text.append("  ")
            text.append(f"NO API {stats['no_api']}", style="yellow")
            text.append("  ")
            text.append(f"FAIL {stats['failed']}", style="bold red")
            if stats["combined_budget"]:
                text.append(
                    f"   combined budget {stats['combined_budget']}/window",
                    style="bold cyan",
                )
        enabled = self.config.proxy.enabled
        text.append(
            f"   proxy routing: {'ON' if enabled else 'OFF'}",
            style="green" if enabled else "dim",
        )
        self.query_one("#head", Static).update(text)

    # -- probing -----------------------------------------------------------

    def action_test_all(self) -> None:
        if not self.entries:
            self._set_status("Nothing to test.")
            return
        self.run_probes(list(self.entries))

    def action_retest(self) -> None:
        entry = self._selected()
        if entry:
            self.run_probes([entry])

    def action_stop(self) -> None:
        if self.workers.cancel_group(self, "probe"):
            self._set_status("Stopped.")
            self.query_one("#progress", ProgressBar).remove_class("visible")

    @work(exclusive=True, group="probe")
    async def run_probes(self, entries: list[str]) -> None:
        progress = self.query_one("#progress", ProgressBar)
        progress.add_class("visible")
        progress.update(total=len(entries), progress=0)

        sem = asyncio.Semaphore(max(1, self.config.proxy.probe_concurrency))
        done = 0

        async def one(entry: str) -> None:
            nonlocal done
            prior_reset, prior_count = self._own_probes.get(entry, (0.0, 0))
            own_prior = prior_count if time.time() < prior_reset else 0
            async with sem:
                result = await probe_proxy(
                    entry, timeout=self.config.proxy.timeout, own_prior=own_prior
                )
            if result.rate_reset:
                self._own_probes[entry] = (result.rate_reset, own_prior + 1)
            self.results[entry] = result
            done += 1
            progress.update(progress=done)
            self._update_row(entry)
            self._refresh_head()
            self._set_status(f"Testing proxies... {done}/{len(entries)}")

        try:
            await asyncio.gather(*(one(e) for e in entries))
            for entry in self.entries:
                self._update_row(entry)
            self._refresh_head()

            stats = summarise_pool(list(self.results.values()))
            self._set_status(
                f"Tested {len(entries)} - {stats['independent']} with their own "
                f"budget, {stats['shared']} shared, {stats['failed']} dead. "
                f"Combined {stats['combined_budget']} req/window. "
                f"Press 's' to save the working ones."
            )
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # noqa: BLE001
            self._set_status(f"{type(exc).__name__}: {exc}", "bold red")
        finally:
            progress.remove_class("visible")

    # -- editing -----------------------------------------------------------

    def _selected(self) -> str | None:
        table = self.query_one("#table", DataTable)
        if table.cursor_row < 0:
            return None
        try:
            return table.coordinate_to_cell_key(table.cursor_coordinate).row_key.value
        except Exception:
            return None

    def action_add(self) -> None:
        entry = self.query_one("#entry", Input)
        entry.add_class("visible")
        entry.focus()

    def action_close_entry(self) -> None:
        entry = self.query_one("#entry", Input)
        if "visible" in entry.classes:
            entry.remove_class("visible")
            entry.value = ""
            self.query_one("#table", DataTable).focus()

    @on(Input.Submitted, "#entry")
    def _on_add(self, event: Input.Submitted) -> None:
        raw = event.value.strip()
        self.action_close_entry()
        if not raw:
            return
        if parse_proxy(raw) is None:
            self._set_status(f"Unrecognised proxy format: {raw}", "bold red")
            return
        if raw in self.entries:
            self._set_status("Already in the list.")
            return
        self.entries.append(raw)
        self._rebuild()
        self._refresh_head()
        self.run_probes([raw])

    def action_delete(self) -> None:
        entry = self._selected()
        if not entry or entry == DIRECT_NAME:
            return
        self.entries.remove(entry)
        self.results.pop(entry, None)
        self._rebuild()
        self._refresh_head()
        self._set_status(f"Removed {entry} (not yet saved - press 's').")

    def action_save(self) -> None:
        working = [
            e
            for e in self.entries
            if e != DIRECT_NAME
            and self.results.get(e)
            and self.results[e].verdict in ("OK", "SHARED")
        ]
        if not working:
            self._set_status("No working proxies to save. Test with 't' first.", "yellow")
            return

        path = Path(self.config.proxy.file)
        if not path.is_absolute():
            base = self.config.source.parent if self.config.source else Path.cwd()
            path = base / path
        body = [
            "# Written by the rotector-selfbot proxy tester.",
            "# Only proxies that answered and reached Rotector are listed.",
            "# Set proxy.enabled = true in config.toml to route through them.",
            *working,
        ]
        path.write_text("\n".join(body) + "\n", encoding="utf-8")
        self._set_status(f"Saved {len(working)} working proxies to {path}")

    # -- detail ------------------------------------------------------------

    @on(DataTable.RowHighlighted, "#table")
    def _show_detail(self, event: DataTable.RowHighlighted) -> None:
        entry = event.row_key.value if event.row_key else None
        if not entry:
            return
        result = self.results.get(entry)
        body = self.query_one("#detail-body", Static)
        if result is None:
            text = Text()
            text.append(f"{entry}\n\n", style="bold")
            text.append("Not tested yet. Press 't' to test all, 'r' for this one.", style="dim")
            body.update(text)
            return

        text = Text()
        text.append(f"{result.label}\n", style="bold")
        text.append(result.verdict, style=_VERDICT_STYLE.get(result.verdict, ""))
        if result.url:
            text.append(f"   {result.url.split('://')[0]}", style="dim")
        text.append("\n\n")
        if result.latency_ms is not None:
            text.append(f"Latency      {result.latency_ms:.0f} ms\n")
        if result.status is not None:
            text.append(f"Rotector     HTTP {result.status}\n")
        if result.rate_limit is not None:
            text.append(f"Budget       {result.rate_limit} requests/window\n")
        if result.used_in_window is not None:
            text.append(f"Already used {result.used_in_window} this window")
            if result.independent_budget is False:
                text.append("   <- shared with another route", style="yellow")
            text.append("\n")
        if result.error:
            text.append(f"\nError        {result.error}\n", style="red")
        for note in result.notes:
            text.append(f"Note         {note}\n", style="yellow")
        body.update(text)

    # -- status ------------------------------------------------------------

    def _set_status(self, text: str, style: str = "") -> None:
        self._status_text = text
        try:
            self.query_one("#status", Static).update(
                Text(text, style=style, no_wrap=True, overflow="ellipsis")
            )
        except Exception:
            pass
