"""Headless run of the proxy tester UI.

Probes are real: `direct` goes out to the network, `127.0.0.1:1` is a closed
port. No proxy servers are needed to exercise both the pass and fail paths.
"""
import asyncio
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import rsb.tui.proxies as mod
from rsb.config import Config
from rsb.proxy import DIRECT_NAME
from textual.widgets import DataTable, Input, Static

ok = lambda m: print(f"[ok] {m}")


async def main():
    tmp = Path(tempfile.mkdtemp())
    proxy_file = tmp / "proxies.txt"
    proxy_file.write_text(
        "# a comment that must be ignored\n"
        "127.0.0.1:1\n"
        "\n"
        "127.0.0.1:2\n",
        encoding="utf-8",
    )

    cfg = Config()
    cfg.proxy.file = str(proxy_file)
    cfg.proxy.timeout = 6.0
    cfg.proxy.probe_concurrency = 4

    app = mod.ProxyTesterApp(cfg)

    async with app.run_test(size=(130, 34)) as pilot:
        await pilot.pause(0.5)
        table = app.query_one("#table", DataTable)

        assert table.row_count == 3, f"rows={table.row_count} (direct + 2 proxies)"
        assert app.entries[0] == DIRECT_NAME
        ok(f"loaded {table.row_count} rows from the list file (comments/blanks skipped)")

        await pilot.press("t")
        for _ in range(120):
            await pilot.pause(0.25)
            if len(app.results) >= 3:
                break
        assert len(app.results) == 3, f"only {len(app.results)} probed"
        ok(f"probed all {len(app.results)} entries")

        verdicts = {e: r.verdict for e, r in app.results.items()}
        ok(f"verdicts: {verdicts}")
        assert verdicts["127.0.0.1:1"] == "FAIL"
        assert verdicts["127.0.0.1:2"] == "FAIL"
        assert app.results["127.0.0.1:1"].error
        ok("dead proxies reported FAIL with a concrete error")

        direct = app.results[DIRECT_NAME]
        if direct.ok:
            assert direct.latency_ms and direct.status
            # everything measured comes from Rotector's own response
            assert direct.rate_limit, "no rate-limit headers seen from Rotector"
            ok(f"direct probed via Rotector: HTTP {direct.status}, "
               f"{direct.latency_ms:.0f}ms, budget {direct.rate_limit}/window, "
               f"used {direct.used_in_window} -> "
               f"independent={direct.independent_budget}")
            assert direct.verdict in ("OK", "SHARED"), direct.verdict
        else:
            ok(f"direct unreachable in this environment: {direct.error}")

        # detail pane for a failing row
        table.move_cursor(row=1)
        await pilot.pause(0.3)
        detail = app.query_one("#detail-body", Static)
        ok("detail pane renders for the highlighted row")

        # saving with nothing working must refuse rather than write an empty file
        app.action_save()
        await pilot.pause(0.2)
        assert "No working proxies" in app._status_text, app._status_text
        assert proxy_file.read_text(encoding="utf-8").count("127.0.0.1") == 2, "file was overwritten"
        ok("save refuses when nothing works, leaving the list file intact")

        # add a malformed entry
        await pilot.press("a")
        await pilot.pause(0.2)
        entry = app.query_one("#entry", Input)
        entry.value = "definitely not a proxy"
        await pilot.press("enter")
        await pilot.pause(0.3)
        assert "Unrecognised" in app._status_text, app._status_text
        assert table.row_count == 3, "malformed entry was added anyway"
        ok("malformed entry rejected before any connection attempt")

        # delete a row
        table.move_cursor(row=1)
        await pilot.press("d")
        await pilot.pause(0.3)
        assert table.row_count == 2, table.row_count
        ok("row removed from the working list")

        # direct cannot be deleted -- it is not a proxy
        table.move_cursor(row=0)
        await pilot.press("d")
        await pilot.pause(0.2)
        assert DIRECT_NAME in app.entries
        ok("the direct route cannot be removed")

        # now pretend one works, and check the saved file
        app.results["127.0.0.1:2"].ok = True
        app.results["127.0.0.1:2"].status = 200
        app.results["127.0.0.1:2"].rate_limit = 50
        app.results["127.0.0.1:2"].rate_remaining = 49
        app.action_save()
        await pilot.pause(0.2)
        saved = proxy_file.read_text(encoding="utf-8")
        assert "127.0.0.1:2" in saved and DIRECT_NAME not in saved
        assert saved.startswith("#"), "saved file should carry an explanatory header"
        ok("save writes only working proxies, with a header, excluding 'direct'")

    print("\nALL PROXY TUI TESTS PASSED")


asyncio.run(main())
