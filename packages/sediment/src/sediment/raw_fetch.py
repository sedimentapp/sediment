#!/usr/bin/env python3 -u
"""Fetch raw data from every installed source into the knowledge vault."""

import argparse
from datetime import datetime, timedelta
from pathlib import Path

from sediment._common import load_profile
from sediment.registry import available_sources
from sediment.sources import FetchWindow


def main():
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
    parser.add_argument("--since", help="Start date YYYY-MM-DD (default: 2 days ago)")
    parser.add_argument("--until", help="End date YYYY-MM-DD (default: today)")
    for source in sources.values():
        source.add_arguments(parser)
    args = parser.parse_args()

    config = load_profile(args.config_dir)
    profiles = config.get("profiles", {})

    if args.profile == "all":
        target_profiles = profiles
    else:
        if args.profile not in profiles:
            raise ValueError(f"Unknown profile: {args.profile}. Available: {', '.join(profiles)}")
        target_profiles = {args.profile: profiles[args.profile]}

    since_str = args.since or (datetime.now() - timedelta(days=2)).strftime("%Y-%m-%d")
    until_str = args.until or datetime.now().strftime("%Y-%m-%d")
    window = FetchWindow(
        since_date=since_str,
        until_date=until_str,
        since_dt=datetime.strptime(since_str, "%Y-%m-%d"),
        until_dt=datetime.strptime(until_str, "%Y-%m-%d") + timedelta(days=1),
    )

    selected = list(sources) if args.source == "all" else [args.source]
    options = vars(args)

    failed = []
    for profile_name, profile in target_profiles.items():
        print(f"\n=== {profile_name} ===")
        for name in selected:
            if name not in profile:
                continue
            print(f"--- {name} ({since_str} .. {until_str}) ---")
            try:
                sources[name].fetch(profile, window, options)
            except Exception as e:  # per-source isolation boundary — one failed source shouldn't kill others
                import traceback
                print(f"  ERROR [{profile_name}/{name}]: {type(e).__name__}: {e}")
                traceback.print_exc()
                failed.append(f"{profile_name}/{name}")

    if failed:
        print(f"\nFailed sources: {', '.join(failed)}")
        raise SystemExit(1)


if __name__ == "__main__":
    main()
