"""Admin UI extension: SSR pages on custom Starlette routes.

Registered as "webadmin" in the sediment_mcp.extensions entry-point group
(enabled via MCP_EXTENSIONS=webadmin). Reads the same module-level state
sediment_mcp.server uses for the MCP tools: the shared QdrantClient and the
ACL loaded at startup.
"""

import csv
import io
import json
import os
import time
from datetime import UTC, datetime

import anyio.to_thread
from fastmcp import FastMCP
from jinja2 import Environment, PackageLoader, select_autoescape
from qdrant_client.http.exceptions import UnexpectedResponse
from starlette.requests import Request
from starlette.responses import HTMLResponse, PlainTextResponse, RedirectResponse, Response

from sediment_mcp import server as core
from sediment_mcp_ee_license import require_ee
from sediment_mcp_ee_webadmin import inventory
from sediment_mcp_ee_webadmin.aclui import register_acl_routes
from sediment_mcp_ee_webadmin.audit import (
    DEFAULT_QUERY_LIMIT,
    RETENTION_DAYS,
    AuditLog,
    AuditMiddleware,
)
from sediment_mcp_ee_webadmin.auth import (
    LOGIN_PATH,
    LOGOUT_PATH,
    WEB_CALLBACK_PATH,
    GitHubAdminAuth,
    build_admin_auth,
)


def _age(ts: int | None) -> str:
    if not ts:
        return "—"
    delta = int(time.time()) - ts
    if delta < 0:
        return "in the future?"
    if delta < 2 * 3600:
        return f"{delta // 60}m ago"
    if delta < 48 * 3600:
        return f"{delta // 3600}h ago"
    return f"{delta // 86400}d ago"


def _iso(ts: int | None) -> str:
    if not ts:
        return "no ts on any point in this space"
    return datetime.fromtimestamp(ts, UTC).strftime("%Y-%m-%d %H:%M UTC")


def _csv_cell(value: object) -> object:
    if isinstance(value, str) and value.startswith(("=", "+", "-", "@", "\t", "\r")):
        return "'" + value
    return value


def register(mcp: FastMCP) -> None:
    require_ee("webadmin")
    auth = build_admin_auth()
    jinja = Environment(
        loader=PackageLoader("sediment_mcp_ee_webadmin", "templates"),
        autoescape=select_autoescape(["html"]),
    )
    jinja.filters["age"] = _age
    jinja.filters["iso"] = _iso
    # jinja's own tojson escapes non-ASCII (д…) — keep cyrillic readable
    jinja.filters["compact_json"] = lambda value: json.dumps(value, ensure_ascii=False)

    def render(template: str, principal: str, status_code: int = 200, **context) -> HTMLResponse:
        html = jinja.get_template(template).render(
            principal=principal, login_routes=auth.login_routes, **context
        )
        return HTMLResponse(html, status_code=status_code)

    register_acl_routes(mcp, auth, render)

    # usage audit (MCP_AUDIT_DB unset = off): every MCP tool call plus the
    # web UI's own mutating actions land in the same log
    audit_log = None
    audit_path = os.environ.get("MCP_AUDIT_DB")
    if audit_path:
        # int() on garbage is a fatal startup error by design
        retention_raw = os.environ.get("MCP_AUDIT_RETENTION_DAYS")
        retention_days = int(retention_raw) if retention_raw else RETENTION_DAYS
        audit_log = AuditLog(audit_path, retention_days=retention_days)
        mcp.add_middleware(AuditMiddleware(audit_log))

    @mcp.custom_route("/admin", methods=["GET"])
    async def overview(request: Request) -> Response:
        who = auth.principal(request)
        if who is None:
            return RedirectResponse(LOGIN_PATH, status_code=302)
        collections = await anyio.to_thread.run_sync(
            inventory.collections_overview, core.client
        )
        return render("overview.html", who, active="collections", collections=collections)

    @mcp.custom_route("/admin/collections/{collection}", methods=["GET"])
    async def spaces(request: Request) -> Response:
        who = auth.principal(request)
        if who is None:
            return RedirectResponse(LOGIN_PATH, status_code=302)
        collection = request.path_params["collection"]
        try:
            report = await anyio.to_thread.run_sync(
                inventory.spaces_inventory, core.client, collection
            )
        except UnexpectedResponse as e:
            if e.status_code == 404:
                return PlainTextResponse(f"No such collection: {collection}", status_code=404)
            raise
        return render("spaces.html", who, active="collections", report=report)

    @mcp.custom_route("/admin/access", methods=["GET"])
    async def access(request: Request) -> Response:
        who = auth.principal(request)
        if who is None:
            return RedirectResponse(LOGIN_PATH, status_code=302)

        acl = core.ACL
        principal_q = request.query_params.get("principal", "").strip().lower()
        space_q = request.query_params.get("space", "").strip()

        grant = None
        if acl is not None and principal_q:
            resolved = acl.resolve(principal_q)
            grant = {
                "collections": sorted(resolved.collections),
                "write": sorted(resolved.write_collections),
                "spaces": None if resolved.spaces is None else sorted(resolved.spaces),
            }

        viewers = None
        if acl is not None and space_q:
            viewers = []
            for p in sorted(acl.principals()):
                g = acl.resolve(p)
                if g.spaces is None:
                    viewers.append(
                        {"principal": p, "via": "unrestricted (*)",
                         "collections": sorted(g.collections)}
                    )
                elif space_q in g.spaces:
                    viewers.append(
                        {"principal": p, "via": "explicit grant",
                         "collections": sorted(g.collections)}
                    )

        return render(
            "access.html",
            who,
            active="access",
            acl_enabled=acl is not None,
            known_principals=sorted(acl.principals()) if acl is not None else [],
            principal_q=principal_q,
            space_q=space_q,
            grant=grant,
            viewers=viewers,
        )

    @mcp.custom_route("/admin/manual", methods=["GET"])
    async def manual(request: Request) -> Response:
        who = auth.principal(request)
        if who is None:
            return RedirectResponse(LOGIN_PATH, status_code=302)
        entries = await anyio.to_thread.run_sync(inventory.manual_entries, core.client)
        return render(
            "manual.html", who, active="manual",
            entries=entries,
            confirm=request.query_params.get("confirm", ""),
            csrf=auth.csrf_token(who),
        )

    @mcp.custom_route("/admin/manual/delete", methods=["POST"])
    async def manual_delete(request: Request) -> Response:
        who = auth.principal(request)
        if who is None:
            return RedirectResponse(LOGIN_PATH, status_code=302)
        form = await request.form()
        if not auth.csrf_verify(who, str(form.get("csrf", ""))):
            return PlainTextResponse("Invalid or expired CSRF token", status_code=403)
        collection = str(form.get("collection", ""))
        point_id = str(form.get("point_id", ""))
        if not collection or not point_id:
            return PlainTextResponse("collection and point_id are required", status_code=400)

        try:
            await anyio.to_thread.run_sync(
                inventory.delete_manual_point, core.client, collection, point_id
            )
        except inventory.ManualDeleteError as exc:
            return PlainTextResponse(str(exc), status_code=409)
        if audit_log is not None:
            await anyio.to_thread.run_sync(
                audit_log.record,
                who, "admin:delete_manual", collection,
                {"point_id": point_id}, True, None, 0,
            )
        return RedirectResponse("/admin/manual", status_code=303)

    @mcp.custom_route("/admin/audit", methods=["GET"])
    async def audit_page(request: Request) -> Response:
        who = auth.principal(request)
        if who is None:
            return RedirectResponse(LOGIN_PATH, status_code=302)
        if audit_log is None:
            return render(
                "audit.html", who, active="audit",
                enabled=False, events=[], stats=[],
                principal_q="", tool_q="", limit=DEFAULT_QUERY_LIMIT,
            )
        principal_q = request.query_params.get("principal", "").strip().lower()
        tool_q = request.query_params.get("tool", "").strip()
        limit_raw = request.query_params.get("limit", "").strip()
        if limit_raw and not limit_raw.isdigit():
            return PlainTextResponse(f"Bad limit: {limit_raw!r}", status_code=400)
        limit = max(1, min(int(limit_raw), 10_000)) if limit_raw else DEFAULT_QUERY_LIMIT
        events = await anyio.to_thread.run_sync(
            audit_log.query, principal_q or None, tool_q or None, limit
        )
        stats = await anyio.to_thread.run_sync(audit_log.stats)
        return render(
            "audit.html", who, active="audit",
            enabled=True, events=events, stats=stats,
            principal_q=principal_q, tool_q=tool_q, limit=limit,
        )

    @mcp.custom_route("/admin/audit.csv", methods=["GET"])
    async def audit_csv(request: Request) -> Response:
        who = auth.principal(request)
        if who is None:
            return RedirectResponse(LOGIN_PATH, status_code=302)
        if audit_log is None:
            return PlainTextResponse("Audit is disabled: MCP_AUDIT_DB is not set", status_code=409)
        principal_q = request.query_params.get("principal", "").strip().lower()
        tool_q = request.query_params.get("tool", "").strip()
        # the whole retained log for the current filters, not just the page
        events = await anyio.to_thread.run_sync(
            audit_log.query, principal_q or None, tool_q or None, None
        )

        out = io.StringIO()
        writer = csv.writer(out)
        writer.writerow(
            ["time_utc", "principal", "tool", "collection", "ok", "error", "duration_ms", "args"]
        )
        for e in events:
            writer.writerow([_csv_cell(value) for value in [
                datetime.fromtimestamp(e.ts, UTC).isoformat(),
                e.principal, e.tool, e.collection or "",
                int(e.ok), e.error or "", e.duration_ms,
                json.dumps(e.detail, ensure_ascii=False),
            ]])
        return Response(
            out.getvalue(),
            media_type="text/csv; charset=utf-8",
            headers={"Content-Disposition": 'attachment; filename="audit.csv"'},
        )

    if isinstance(auth, GitHubAdminAuth):

        @mcp.custom_route(LOGIN_PATH, methods=["GET"])
        async def login(request: Request) -> Response:
            return auth.login_redirect(request)

        @mcp.custom_route(WEB_CALLBACK_PATH, methods=["GET"])
        async def callback(request: Request) -> Response:
            return await auth.handle_callback(request)

        @mcp.custom_route(LOGOUT_PATH, methods=["GET"])
        async def logout(request: Request) -> Response:
            return auth.logout()
