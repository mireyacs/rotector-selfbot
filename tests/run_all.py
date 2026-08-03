"""Run every test script. Usage: python tests/run_all.py [--offline]"""
import subprocess, sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
OFFLINE = {"test_units.py", "test_progress.py", "test_gateway_scrape.py",
           "test_proxy_speedup.py", "test_export.py",
           "test_purge.py", "test_png_export.py", "test_member_coverage.py",
           "test_gateway_resilience.py", "test_bulk_actions.py",
           "test_cards_html.py", "test_bot_token.py",
           "test_caution_notice.py",
           "test_settings_appeal.py", "test_thumbnail.py",
           "test_okappiki.py", "test_theme_persist.py"}
NETWORK = {"test_live_api.py", "test_tui.py", "test_routing.py",
           "test_proxy_tui.py", "test_streaming.py",
           "test_moderation_tui.py", "test_sources.py",
           "test_layout_retention.py", "test_sorting.py",
           "test_stop.py", "test_listing_inbox.py",
           "test_settings_ui.py", "test_reload_recovery.py"}

only_offline = "--offline" in sys.argv
suites = sorted(OFFLINE if only_offline else OFFLINE | NETWORK)

failed = []
for name in suites:
    print(f"\n{'=' * 70}\n  {name}\n{'=' * 70}")
    if subprocess.run([sys.executable, str(HERE / name)]).returncode:
        failed.append(name)

print(f"\n{'=' * 70}")
print(f"FAILED: {', '.join(failed)}" if failed else f"All {len(suites)} suite(s) passed.")
sys.exit(1 if failed else 0)
