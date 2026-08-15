"""ACL section of the admin UI: structured view, YAML editor, version history.

Editing exists only when MCP_ACL_DB points at the SQLite store (ee feature);
without it the section is a read-only view of the file-based config and core
behaves exactly as before. With the store, the latest version becomes the
effective ACL at startup (seeded from MCP_ACL_CONFIG when the DB is empty)
and every save hot-swaps sediment_mcp.server.ACL — no restart.
"""

import os
from typing import Callable

import anyio.to_thread
import yaml
from fastmcp import FastMCP
from fastmcp.utilities.logging import get_logger
from starlette.requests import Request
from starlette.responses import PlainTextResponse, RedirectResponse, Response

from sediment_mcp import server as core
from sediment_mcp.acl import WILDCARD, AclConfigError
from sediment_mcp_ee_webadmin import inventory
from sediment_mcp_ee_webadmin.aclstore import AclStore, parse_and_validate
from sediment_mcp_ee_webadmin.auth import LOGIN_PATH, AdminAuth

logger = get_logger(__name__)


def _load_store() -> AclStore | None:
    db_path = os.environ.get("MCP_ACL_DB")
    if not db_path:
        return None
    if os.environ.get("MCP_ACL_DISABLE") == "1":
        raise RuntimeError(
            "MCP_ACL_DB with MCP_ACL_DISABLE=1 is contradictory: the store would "
            "silently re-enable ACL on a server started as allow-all — remove one"
        )
    store = AclStore(db_path)
    latest = store.latest()
    if latest is None:
        seed_path = os.environ.get("MCP_ACL_CONFIG")
        if seed_path:
            with open(seed_path) as f:
                text = f.read()
            version, acl = store.save(text, author="seed:MCP_ACL_CONFIG")
            core.ACL = acl
            logger.info("ACL DB %s seeded from %s as version %d", db_path, seed_path, version)
    else:
        # a stored version was validated at save time; if it no longer parses
        # (schema change), failing startup is correct — never come up half-protected
        core.ACL = parse_and_validate(latest.yaml_text)
        logger.info(
            "ACL loaded from DB %s version %d by %r (overrides MCP_ACL_CONFIG)",
            db_path, latest.version, latest.author,
        )
    return store


def _current_config(store: AclStore | None) -> tuple[str | None, str]:
    """Effective config as (yaml_text, source_label) — mirrors what enforcement uses."""
    if store is not None:
        latest = store.latest()
        if latest is not None:
            return latest.yaml_text, f"DB version {latest.version} by {latest.author}"
    path = os.environ.get("MCP_ACL_CONFIG")
    if path:
        with open(path) as f:
            return f.read(), f"file {path}"
    return None, "not configured"


def _config_spaces(config: dict) -> set[str]:
    spaces: set[str] = set()
    for group_spaces in (config.get("space_groups") or {}).values():
        spaces |= {s for s in group_spaces if isinstance(s, str)}
    for grant in config.get("grants") or []:
        spaces |= {s for s in grant.get("spaces") or [] if isinstance(s, str) and s != WILDCARD}
    return spaces


def register_acl_routes(
    mcp: FastMCP,
    auth: AdminAuth,
    render: Callable[..., Response],
) -> None:
    store = _load_store()

    @mcp.custom_route("/admin/acl", methods=["GET"])
    async def acl_view(request: Request) -> Response:
        who = auth.principal(request)
        if who is None:
            return RedirectResponse(LOGIN_PATH, status_code=302)
        text, source = _current_config(store)
        config = yaml.safe_load(text) if text else {}
        names = {}
        spaces = _config_spaces(config)
        if spaces:
            names = await anyio.to_thread.run_sync(
                inventory.space_names, core.client, spaces
            )
        return render(
            "acl.html",
            who,
            active="acl",
            source=source,
            editable=store is not None,
            enforced=core.ACL is not None,
            user_groups=config.get("user_groups") or {},
            space_groups=config.get("space_groups") or {},
            grants=config.get("grants") or [],
            names=names,
        )

    @mcp.custom_route("/admin/acl/edit", methods=["GET", "POST"])
    async def acl_edit(request: Request) -> Response:
        who = auth.principal(request)
        if who is None:
            return RedirectResponse(LOGIN_PATH, status_code=302)
        if store is None:
            return PlainTextResponse(
                "Editing is disabled: MCP_ACL_DB is not set", status_code=409
            )

        if request.method == "GET":
            text, source = _current_config(store)
            return render(
                "acl_edit.html", who, active="acl",
                yaml_text=text or "", source=source,
                error=None, csrf=auth.csrf_token(who),
            )

        form = await request.form()
        if not auth.csrf_verify(who, str(form.get("csrf", ""))):
            return PlainTextResponse("Invalid or expired CSRF token", status_code=403)
        text = str(form.get("yaml_text", "")).replace("\r\n", "\n")

        def reject(error: str) -> Response:
            return render(
                "acl_edit.html", who, active="acl",
                yaml_text=text, source="unsaved draft",
                error=error, csrf=auth.csrf_token(who),
                status_code=400,
            )

        if not text.strip():
            return reject("Config must not be empty — ACL cannot be disabled from the UI")
        try:
            version, acl = store.save(text, author=who)
        except (yaml.YAMLError, AclConfigError) as e:
            return reject(str(e))

        core.ACL = acl
        logger.info("ACL updated to version %d by %r", version, who)
        return RedirectResponse("/admin/acl", status_code=303)

    @mcp.custom_route("/admin/acl/history", methods=["GET"])
    async def acl_history(request: Request) -> Response:
        who = auth.principal(request)
        if who is None:
            return RedirectResponse(LOGIN_PATH, status_code=302)
        if store is None:
            return PlainTextResponse(
                "No version history: MCP_ACL_DB is not set", status_code=409
            )
        versions = store.history()
        selected = None
        v = request.query_params.get("v", "")
        if v.isdigit():
            selected = store.get(int(v))
            if selected is None:
                return PlainTextResponse(f"No such version: {v}", status_code=404)
        return render(
            "acl_history.html", who, active="acl",
            versions=versions, selected=selected,
            latest_version=versions[0].version if versions else None,
            csrf=auth.csrf_token(who),
        )

    @mcp.custom_route("/admin/acl/restore", methods=["POST"])
    async def acl_restore(request: Request) -> Response:
        who = auth.principal(request)
        if who is None:
            return RedirectResponse(LOGIN_PATH, status_code=302)
        if store is None:
            return PlainTextResponse(
                "Editing is disabled: MCP_ACL_DB is not set", status_code=409
            )
        form = await request.form()
        if not auth.csrf_verify(who, str(form.get("csrf", ""))):
            return PlainTextResponse("Invalid or expired CSRF token", status_code=403)
        v = str(form.get("version", ""))
        if not v.isdigit():
            return PlainTextResponse(f"Bad version: {v!r}", status_code=400)
        source = store.get(int(v))
        if source is None:
            return PlainTextResponse(f"No such version: {v}", status_code=404)

        version, acl = store.save(source.yaml_text, author=f"{who} (restore of v{source.version})")
        core.ACL = acl
        logger.info("ACL restored from version %d as version %d by %r", source.version, version, who)
        return RedirectResponse("/admin/acl", status_code=303)
