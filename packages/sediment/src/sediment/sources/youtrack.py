"""YouTrack issues + articles fetcher."""

import argparse
import json
import os
import re
import urllib.parse
from collections.abc import Mapping
from datetime import datetime
from pathlib import Path
from typing import Any

from knowledge_schema import make_space

from sediment._common import HttpError, http_get, safe_path_component, sanitize
from sediment.sources import FetchWindow, Source, SpaceDerivationError

# YouTrack returns this error per unknown project short-id inside `error_children`.
# Whole batch query is rejected if any single project is invalid → we strip them and retry.
_INVALID_PROJECT_RE = re.compile(r'The value "([^"]+)" isn\'t used for the project field')


def fetch_youtrack_issues(profile: dict[str, Any], since_date: str, until_date: str,
                          articles_only: bool = False, only_projects: list[str] | None = None):
    yt = profile["youtrack"]
    base_url = yt["url"].rstrip("/")
    token_env = yt["token_env"]
    token = os.environ.get(token_env, "")
    if not token:
        raise RuntimeError(f"Missing env var {token_env}")

    raw_dir = Path(profile["vault_path"]).expanduser() / "raw" / "youtrack"
    raw_dir.mkdir(parents=True, exist_ok=True)

    if only_projects:
        enabled = only_projects
    else:
        enabled = [p["short"] for p in yt.get("projects", []) if p.get("enabled")]

    if not articles_only:
        def build_query(projects: list[str]) -> str:
            if projects:
                project_filter = "project: " + ", ".join(f"{{{p}}}" for p in projects)
                return f"{project_filter} updated: {since_date} .. {until_date}"
            return f"updated: {since_date} .. {until_date}"

        query = build_query(enabled)
        fields = "idReadable,summary,description,created,updated,resolved,reporter(login,fullName),comments(author(login,fullName),text,created)"
        page_size = 100
        issues: list[dict[str, Any]] = []
        skip = 0
        while True:
            params = urllib.parse.urlencode({"query": query, "fields": fields, "$top": page_size, "$skip": skip})
            url = f"{base_url}/api/issues?{params}"
            try:
                page = json.loads(http_get(url, headers={
                    "Authorization": f"Bearer {token}",
                    "Accept": "application/json",
                }))
            except HttpError as e:
                if e.code == 400 and skip == 0 and enabled:
                    bad = _INVALID_PROJECT_RE.findall(e.body.decode("utf-8", errors="replace"))
                    bad_in_filter = [p for p in bad if p in enabled]
                    if bad_in_filter:
                        kept = [p for p in enabled if p not in bad_in_filter]
                        print(f"  WARNING: YouTrack rejected projects {bad_in_filter}, dropping and retrying with {len(kept)} remaining")
                        if not kept:
                            print("  WARNING: no projects left after filtering, skipping issues fetch")
                            break
                        enabled = kept
                        query = build_query(enabled)
                        continue
                raise
            issues.extend(page)
            if len(page) < page_size:
                break
            skip += page_size

        print(f"  Found {len(issues)} issues")

        new_count = 0
        updated_count = 0

        for issue in issues:
            issue_id = safe_path_component(issue.get("idReadable", "UNKNOWN"), "YouTrack issue id")
            raw_file = raw_dir / f"{issue_id}.md"

            created = issue.get("created")
            updated = issue.get("updated")
            created_str = datetime.fromtimestamp(created / 1000).strftime("%Y-%m-%d %H:%M") if created else ""
            updated_str = datetime.fromtimestamp(updated / 1000).strftime("%Y-%m-%d %H:%M") if updated else ""
            reporter = issue.get("reporter", {})
            reporter_name = reporter.get("fullName") or reporter.get("login", "") if reporter else ""

            lines = [
                f"# {issue_id}: {issue.get('summary', '')}",
                "",
                f"**Created:** {created_str}  ",
                f"**Updated:** {updated_str}  ",
                f"**Reporter:** {reporter_name}",
                "",
            ]

            desc = issue.get("description", "")
            if desc:
                lines.append("## Description")
                lines.append("")
                lines.append(desc)
                lines.append("")

            comments = issue.get("comments", [])
            if comments:
                lines.append("## Comments")
                lines.append("")
                for c in comments:
                    author = c.get("author", {})
                    author_name = author.get("fullName") or author.get("login", "") if author else ""
                    c_created = c.get("created")
                    c_date = datetime.fromtimestamp(c_created / 1000).strftime("%Y-%m-%d %H:%M") if c_created else ""
                    lines.append(f"**{author_name}** [{c_date}]:")
                    lines.append(c.get("text") or "")
                    lines.append("")

            text = sanitize("\n".join(lines))
            existed = raw_file.exists()
            if existed and raw_file.read_text() == text:
                continue
            raw_file.write_text(text)
            if existed:
                updated_count += 1
            else:
                new_count += 1

        print(f"  Issues: {new_count} new, {updated_count} updated")

    # Fetch articles (optional, some instances don't have them)
    if not yt.get("fetch_articles", True):
        return

    enabled_shorts = [p["short"] for p in yt.get("projects", []) if p.get("enabled")]
    art_fields = "idReadable,summary,content,created,updated,author(login,fullName),project(shortName)"
    art_page_size = 200
    articles = []
    art_skip = 0
    while True:
        art_params = urllib.parse.urlencode({"fields": art_fields, "$top": art_page_size, "$skip": art_skip})
        art_url = f"{base_url}/api/articles?{art_params}"
        page = json.loads(http_get(art_url, headers={
            "Authorization": f"Bearer {token}",
            "Accept": "application/json",
        }))
        articles.extend(page)
        if len(page) < art_page_size:
            break
        art_skip += art_page_size

    if enabled_shorts:
        articles = [a for a in articles if a.get("project", {}).get("shortName") in enabled_shorts]
    print(f"  Found {len(articles)} articles")

    art_new = 0
    art_updated = 0
    for article in articles:
        art_id = safe_path_component(article.get("idReadable", "UNKNOWN"), "YouTrack article id")
        raw_file = raw_dir / f"{art_id}.md"

        created = article.get("created")
        updated = article.get("updated")
        created_str = datetime.fromtimestamp(created / 1000).strftime("%Y-%m-%d %H:%M") if created else ""
        updated_str = datetime.fromtimestamp(updated / 1000).strftime("%Y-%m-%d %H:%M") if updated else ""
        author = article.get("author", {})
        author_name = author.get("fullName") or author.get("login", "") if author else ""
        project = article.get("project", {}).get("shortName", "")

        lines = [
            f"# [{project}] {art_id}: {article.get('summary', '')}",
            "",
            f"**Created:** {created_str}  ",
            f"**Updated:** {updated_str}  ",
            f"**Author:** {author_name}",
            "",
        ]

        content = article.get("content", "")
        if content:
            lines.append(content)
            lines.append("")

        text = sanitize("\n".join(lines))
        existed = raw_file.exists()
        if existed and raw_file.read_text() == text:
            continue
        raw_file.write_text(text)
        if existed:
            art_updated += 1
        else:
            art_new += 1

    print(f"  Articles: {art_new} new, {art_updated} updated")


def _add_arguments(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--articles-only", action="store_true", help="Fetch only articles (YouTrack)")
    parser.add_argument("--projects", nargs="+", help="Fetch only these projects (YouTrack short names, e.g. GEO BAR)")


def _fetch(profile: dict[str, Any], window: FetchWindow, options: Mapping[str, Any]) -> None:
    fetch_youtrack_issues(
        profile,
        since_date=window.since_date,
        until_date=window.until_date,
        articles_only=options["articles_only"],
        only_projects=options["projects"],
    )


def _derive_space(rel_path: str, context: None) -> tuple[str, str]:
    """youtrack/<PROJ-123>.md, articles youtrack/<PROJ-A-5>.md — the project is the id prefix."""
    stem = rel_path.split("/")[-1].removesuffix(".md")
    project, sep, rest = stem.partition("-")
    if not sep or not project or not rest:
        raise SpaceDerivationError(rel_path, f"YouTrack id {stem!r} has no PROJECT- prefix")
    return make_space("youtrack", project), project


SOURCE = Source(
    name="youtrack",
    fetch=_fetch,
    derive_space=_derive_space,
    add_arguments=_add_arguments,
)
