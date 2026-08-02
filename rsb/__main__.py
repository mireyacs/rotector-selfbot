"""Entry point: ``python -m rsb``."""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

from .config import Config, candidate_paths


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="rsb",
        description=(
            "Scan the members of a Discord server against the Rotector database "
            "and flag accounts with detected violations."
        ),
        epilog="Data: Rotector (https://rotector.com)",
    )
    parser.add_argument("-c", "--config", type=Path, help="path to config.toml")
    parser.add_argument("--token", help="Discord user token (overrides config and env)")
    parser.add_argument(
        "--bot-token",
        help="Discord bot token; used only when no user token is set. "
             "Servers only, but every member of them.",
    )
    parser.add_argument("--api-key", help="Rotector API key (overrides config and env)")
    parser.add_argument(
        "--rate-limit", type=int, help="requests allowed per window (default 50)"
    )
    parser.add_argument(
        "--window", type=float, help="rate limit window in seconds (default 10)"
    )
    parser.add_argument(
        "--reserve",
        type=int,
        help="request units held back as safety headroom (default 5)",
    )
    parser.add_argument(
        "--max-members",
        type=int,
        help="cap members scanned per server (0 = no cap)",
    )
    parser.add_argument(
        "--include-bots", action="store_true", help="do not skip bot accounts"
    )
    parser.add_argument(
        "--proxies",
        action="store_true",
        help="route Rotector lookups over the configured proxies",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="with 'migrate', report what is missing without writing anything",
    )
    parser.add_argument(
        "--no-proxies",
        action="store_true",
        help="ignore configured proxies for this run",
    )
    parser.add_argument(
        "command",
        nargs="?",
        default="scan",
        choices=("scan", "proxies", "migrate"),
        help=(
            "'scan' (default) opens the scanner; 'proxies' opens the proxy "
            "tester; 'migrate' adds newly added settings to an older config"
        ),
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)

    if args.config and not args.config.is_file():
        print(f"config file not found: {args.config}", file=sys.stderr)
        return 2

    config = Config.load(args.config)

    if args.token:
        config.token = args.token.strip()
    if args.bot_token:
        config.bot_token = args.bot_token.strip()
    if args.api_key:
        config.rotector.api_key = args.api_key.strip()
    if args.rate_limit is not None:
        config.rotector.rate_limit = args.rate_limit
    if args.window is not None:
        config.rotector.window = args.window
    if args.reserve is not None:
        config.rotector.reserve = args.reserve
    if args.max_members is not None:
        config.scan.max_members = args.max_members
    if args.include_bots:
        config.scan.skip_bots = False
    if args.proxies:
        config.proxy.enabled = True
    if args.no_proxies:
        config.proxy.enabled = False

    if args.command == "migrate":
        from .migrate import migrate_config

        target = args.config or config.config_path()
        report = migrate_config(target, dry_run=args.dry_run)
        if args.dry_run:
            if report.changed:
                print(f"{target} is missing settings:")
                for name in report.added_sections:
                    print(f"  [{name}]  (whole section)")
                for name in report.added_keys:
                    print(f"  {name}")
                print("\nRun without --dry-run to add them.")
            else:
                print(f"{target} is already up to date.")
            return 0
        print(report.describe())
        if report.backup:
            print(f"Previous version saved as {report.backup}")
            print(
                "Note: that backup contains whatever your config did, token "
                "included. It is covered by .gitignore here; if you keep your "
                "config elsewhere, make sure the .bak is ignored too."
            )
        return 0

    if args.command == "proxies":
        # The tester needs no Discord token -- it only talks to proxies.
        from .tui.proxies import ProxyTesterApp

        ProxyTesterApp(config).run()
        return 0

    problems = config.validate()
    if problems:
        for problem in problems:
            print(f"error: {problem}", file=sys.stderr)
        print("\nlooked for a config file at:", file=sys.stderr)
        for path in candidate_paths():
            print(f"  {path}", file=sys.stderr)
        return 2

    from .tui.app import ScannerApp

    ScannerApp(config).run()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
