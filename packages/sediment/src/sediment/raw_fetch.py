#!/usr/bin/env python3 -u
"""Fetch raw data from every installed source into the knowledge vault."""

import argparse
import logging
from datetime import date
from pathlib import Path

from sediment._common import load_profile
from sediment.fetch_state import fetch_incremental, fetch_window
from sediment.registry import available_sources


def main(argv: list[str] | None = None):
    sources = available_sources()

    parser = argparse.ArgumentParser(description="Fetch raw data from sources into knowledge vault")
    parser.add_argument(
        "--config-dir",
        default=str(Path.home() / "code" / "gardev" / "sediment"),
        help="Path to config directory with _profile.yaml and .env",
    )
    parser.add_argument("--profile", default="all", help="Profile name (default: all)")
    parser.add_argument(
        "--source",
        choices=[*sources, "all"],
        default="all",
        help="Data source (default: all)",
    )
    parser.add_argument("--since", type=date.fromisoformat, help="Manual fetch start date YYYY-MM-DD; does not update automatic progress")
    parser.add_argument("--until", type=date.fromisoformat, help="Manual fetch end date YYYY-MM-DD (default: today)")
    parser.add_argument("--initial-since", type=date.fromisoformat, help="Required initial date for automatic collection when no checkpoint exists")
    common_options = {action.dest for action in parser._actions}
    for source in sources.values():
        source.add_arguments(parser)
    args = parser.parse_args(argv)
    options = vars(args)
    restricted = any(
        options[action.dest] != action.default
        for action in parser._actions if action.dest not in common_options
    )
    if args.until is not None and args.since is None:
        parser.error("--until requires --since for a manual fetch")
    if restricted and args.since is None:
        parser.error("Source-specific options require --since for a manual fetch")
    if args.initial_since is not None and args.since is not None:
        parser.error("--initial-since cannot be combined with a manual --since")
    today = date.today()
    window = None
    if args.since is not None:
        until = args.until if args.until is not None else today
        if args.since > until or until > today:
            parser.error("Manual dates must satisfy since <= until <= today")
        window = fetch_window(args.since, until)

    config = load_profile(args.config_dir)
    profiles = config["profiles"]
    if not profiles:
        raise ValueError("No fetch profiles configured")

    if args.profile == "all":
        target_profiles = profiles
    else:
        if args.profile not in profiles:
            raise ValueError(f"Unknown profile: {args.profile}. Available: {', '.join(profiles)}")
        target_profiles = {args.profile: profiles[args.profile]}

    selected = list(sources) if args.source == "all" else [args.source]
    if not any(name in profile for profile in target_profiles.values() for name in selected):
        raise ValueError("No configured sources match this fetch request")

    failed = []
    for profile_name, profile in target_profiles.items():
        print(f"\n=== {profile_name} ===")
        for name in selected:
            if name not in profile:
                continue
            try:
                if window is not None:
                    print(f"--- {name} ({window.since_date} .. {window.until_date}, manual) ---")
                    sources[name].fetch(profile, window, options)
                else:
                    fetch_incremental(profile_name, profile, sources[name], options, today, args.initial_since)
            except Exception as e:  # per-source isolation boundary — one failed source shouldn't kill others
                logging.getLogger(__name__).exception(
                    "Fetch failed for %s/%s: %s", profile_name, name, e,
                    extra={"operation": "fetch", "profile": profile_name, "source": name},
                )
                failed.append(f"{profile_name}/{name}")

    if failed:
        print(f"\nFailed sources: {', '.join(failed)}")
        raise SystemExit(1)


if __name__ == "__main__":
    main()
