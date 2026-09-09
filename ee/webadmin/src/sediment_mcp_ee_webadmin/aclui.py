"""Structured shared drafts and explicit, atomic managed-access activation."""

import copy
import functools
import logging
import re
from typing import Callable

import anyio.to_thread
import httpx
import yaml
from fastmcp import FastMCP
from starlette.requests import Request
from starlette.responses import PlainTextResponse, RedirectResponse, Response

from sediment_mcp import server as core
from sediment_mcp.acl import AclConfigError
from sediment_mcp_ee_access import check_seats
from sediment_mcp_ee_access.model import changes, encode, restore_document, validate
from sediment_mcp_ee_access.store import AccessStore, ConflictError
from sediment_mcp_ee_webadmin.auth import LOGIN_PATH, AdminAuth

logger = logging.getLogger(__name__)


async def work(fn, *args, **kwargs):
    return await anyio.to_thread.run_sync(functools.partial(fn, *args, **kwargs))


def _number(form, key: str) -> int:
    value = str(form.get(key, ""))
    if not value.isdigit():
        raise ValueError(f"{key} is required and must be a non-negative integer")
    return int(value)


def _items(form, key: str) -> list[str]:
    return [p.strip() for p in str(form.get(key, "")).split(",") if p.strip()]


async def github_profile(login: str) -> dict:
    if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9-]{0,38}", login):
        raise ValueError("Enter a valid GitHub login")
    async with httpx.AsyncClient(timeout=15) as client:
        response = await client.get(f"https://api.github.com/users/{login}", headers={"Accept": "application/vnd.github+json"})
        response.raise_for_status()
        profile = response.json()
    if type(profile.get("id")) is not int or not isinstance(profile.get("login"), str):
        raise ValueError("GitHub returned an invalid profile")
    if profile.get("type") != "User":
        raise ValueError("Select a GitHub user account, not an organization")
    return {"github_id": str(profile["id"]), "github_login": profile["login"]}


def edit_document(document: dict, form, profile: dict | None = None) -> dict:
    doc = copy.deepcopy(document)
    operation = str(form.get("operation", ""))
    if operation == "user_add":
        user = {"principal": str(form.get("principal", "")).strip().lower(),
                "github_id": None, "github_login": None, "enabled": True}
        if profile is not None:
            user.update(profile)
        doc["users"].append(user)
    elif operation == "user_toggle":
        matched = [u for u in doc["users"] if u["principal"] == form.get("principal")]
        if len(matched) != 1:
            raise ValueError("User no longer exists in this draft")
        matched[0]["enabled"] = not matched[0]["enabled"]
    elif operation in {"group_save", "group_delete", "space_group_save", "space_group_delete"}:
        kind = "space_groups" if operation.startswith("space_") else "user_groups"
        name = str(form.get("name", "")).strip()
        if not name or len(name) > 128:
            raise ValueError("Group name must contain 1–128 characters")
        groups = doc["policy"].setdefault(kind, {})
        if operation.endswith("delete"):
            if name not in groups:
                raise ValueError("Group no longer exists")
            del groups[name]
        else:
            groups[name] = _items(form, "members")
    elif operation in {"grant_save", "grant_delete"}:
        grants = doc["policy"]["grants"]
        index = _number(form, "index") if str(form.get("index", "")) else None
        if index is not None and index >= len(grants):
            raise ValueError("Grant no longer exists")
        if operation == "grant_delete":
            if index is None:
                raise ValueError("Select a grant to remove")
            del grants[index]
        else:
            grant = {"collections": _items(form, "collection")}
            for key in ["users", "user_groups", "space_groups", "spaces"]:
                values = _items(form, key)
                if values:
                    grant[key] = values
            grant["write"] = form.get("write") == "on"
            grant["unrestricted"] = form.get("unrestricted") == "on"
            if index is None:
                grants.append(grant)
            else:
                grants[index] = grant
    else:
        raise ValueError("Unknown draft operation")
    validate(doc)
    return doc


def register_acl_routes(mcp: FastMCP, auth: AdminAuth, render: Callable[..., Response]) -> None:
    source = core.ACCESS
    store = source if isinstance(source, AccessStore) else None

    async def workspace(who: str, *, error=None, status=200, profile=None, submitted=None):
        if store is None:
            snap = source.snapshot()
            policy = snap.acl.config if snap.acl is not None else {}
            return render("acl.html", who, active="acl", source="file (loaded at startup)", editable=False,
                          enforced=snap.acl is not None, user_groups=policy.get("user_groups", {}),
                          space_groups=policy.get("space_groups", {}), grants=policy.get("grants", []), names={})
        active = await work(store.latest)
        draft = await work(store.draft)
        return render("access_editor.html", who, active="acl", current=active, draft=draft,
                      dirty=encode(active.document) != encode(draft.document), error=error,
                      profile=profile, submitted=submitted, csrf=auth.csrf_token(who), status_code=status)

    @mcp.custom_route("/admin/acl", methods=["GET"])
    async def acl_view(request: Request) -> Response:
        who = auth.principal(request)
        if who is None:
            return RedirectResponse(LOGIN_PATH, status_code=302)
        return await workspace(who)

    @mcp.custom_route("/admin/acl/edit", methods=["GET", "POST"])
    async def legacy_editor(request: Request) -> Response:
        if auth.principal(request) is None:
            return RedirectResponse(LOGIN_PATH, status_code=302)
        if request.method == "GET":
            return RedirectResponse("/admin/acl", status_code=303)
        return PlainTextResponse("Direct YAML activation was removed. Import into a draft and preview it.", status_code=409)

    @mcp.custom_route("/admin/acl/preview", methods=["GET"])
    async def preview(request: Request) -> Response:
        who = auth.principal(request)
        if who is None:
            return RedirectResponse(LOGIN_PATH, status_code=302)
        if store is None:
            return PlainTextResponse("File ACL is read-only", status_code=409)
        active, draft = await work(store.latest), await work(store.draft)
        snapshot = validate(draft.document)
        assert snapshot.active_principals is not None
        error = None
        try:
            check_seats(snapshot.active_principals)
        except (ValueError, RuntimeError) as exc:
            logger.exception("operation=access_preview_license author=%s", who)
            error = str(exc)
        return render("access_preview.html", who, active="acl", current=active, draft=draft,
                      changes=changes(active.document, draft.document), error=error,
                      dirty=encode(active.document) != encode(draft.document), csrf=auth.csrf_token(who))

    @mcp.custom_route("/admin/acl/history", methods=["GET"])
    async def history(request: Request) -> Response:
        who = auth.principal(request)
        if who is None:
            return RedirectResponse(LOGIN_PATH, status_code=302)
        if store is None:
            return PlainTextResponse("File ACL history belongs to the operator", status_code=409)
        return render("access_history.html", who, active="acl", versions=await work(store.history),
                      draft=await work(store.draft), csrf=auth.csrf_token(who))

    @mcp.custom_route("/admin/acl/export", methods=["GET"])
    async def export(request: Request) -> Response:
        if auth.principal(request) is None:
            return RedirectResponse(LOGIN_PATH, status_code=302)
        if store is None:
            return PlainTextResponse("Use the configured Community YAML file", status_code=409)
        version = await work(store.latest)
        return Response(yaml.safe_dump(version.document, sort_keys=False), media_type="application/yaml",
                        headers={"Content-Disposition": f'attachment; filename="access-v{version.version}.yaml"'})

    async def mutate(request: Request) -> Response:
        who = auth.principal(request)
        if who is None:
            return RedirectResponse(LOGIN_PATH, status_code=302)
        if store is None:
            return PlainTextResponse("File ACL is read-only", status_code=409)
        body = await request.body()
        if len(body) > 2_097_152:
            return PlainTextResponse("Request exceeds 2 MiB", status_code=413)
        form = await request.form()
        if not auth.csrf_verify(who, str(form.get("csrf", ""))):
            return PlainTextResponse("Invalid or expired CSRF token", status_code=403)
        operation = request.url.path.rsplit("/", 1)[-1]
        try:
            revision, base = _number(form, "revision"), _number(form, "base_version")
            draft = await work(store.draft)
            if (draft.revision, draft.base_version) != (revision, base):
                raise ConflictError("Shared draft changed; your submitted values are shown below")
            if operation == "github":
                profile = await github_profile(str(form.get("github_login", "")))
                return await workspace(who, profile=profile)
            if operation == "apply":
                await work(store.apply, author=who, revision=revision, base_version=base, check_seats=check_seats)
                return RedirectResponse("/admin/acl", status_code=303)
            if operation == "import":
                doc = yaml.safe_load(str(form.get("document", "")))
            elif operation == "restore":
                old = await work(store.get, _number(form, "version"))
                current = await work(store.latest)
                doc = restore_document(current.document, old.document)
            elif operation == "discard":
                doc = (await work(store.latest)).document
            else:
                profile = None
                if form.get("operation") == "user_add" and form.get("github_login"):
                    profile = await github_profile(str(form["github_login"]))
                    if profile["github_id"] != str(form.get("github_id", "")):
                        raise ValueError("GitHub identity changed; look up and confirm the profile again")
                doc = edit_document(draft.document, form, profile)
            await work(store.save_draft, doc, author=who, revision=revision, base_version=base)
        except (AclConfigError, ValueError, yaml.YAMLError, ConflictError, KeyError) as exc:
            logger.warning("operation=access_%s author=%s rejected=%s", operation, who, exc)
            submitted = {k: str(v) for k, v in form.items() if k != "csrf"}
            return await workspace(who, error=str(exc), status=409 if isinstance(exc, ConflictError) else 400,
                                   submitted=submitted)
        except (httpx.HTTPStatusError, httpx.RequestError):
            logger.exception("operation=github_profile author=%s login=%s", who, form.get("github_login"))
            raise
        return RedirectResponse("/admin/acl/preview" if operation in {"restore", "import"} else "/admin/acl", status_code=303)

    for path in ["draft", "github", "apply", "import", "restore", "discard"]:
        mcp.custom_route(f"/admin/acl/{path}", methods=["POST"])(mutate)
