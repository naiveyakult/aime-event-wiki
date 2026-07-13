from __future__ import annotations

import inspect
import json
import secrets
from collections.abc import Callable
from pathlib import Path
from typing import Any

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import JSONResponse, RedirectResponse
from fastapi.templating import Jinja2Templates

_TEMPLATES = Jinja2Templates(directory=str(Path(__file__).with_name("templates")))


def _as_dict(value: Any) -> dict[str, Any]:
    if isinstance(value, dict):
        return value
    if hasattr(value, "model_dump"):
        return value.model_dump(mode="json")
    if hasattr(value, "_mapping"):
        return dict(value._mapping)
    data = getattr(value, "__dict__", None)
    return {key: item for key, item in (data or {}).items() if not key.startswith("_")}


def _pending(repository: Any) -> list[dict[str, Any]]:
    if hasattr(repository, "list_pending_patches"):
        values = repository.list_pending_patches()
    else:
        values = repository.list_review_patches(status="pending")
    return [_as_dict(value) for value in values]


def _get_patch(repository: Any, patch_id: str) -> dict[str, Any] | None:
    if hasattr(repository, "get_patch"):
        value = repository.get_patch(patch_id)
    else:
        value = repository.get_review_context(patch_id)
    return _as_dict(value) if value is not None else None


def _get_context(repository: Any, patch_id: str) -> dict[str, Any] | None:
    if hasattr(repository, "get_review_context"):
        try:
            value = repository.get_review_context(patch_id)
        except KeyError:
            return None
        if value is None:
            return None
        context = _as_dict(value)
        if "patch" in context:
            patch = _as_dict(context.pop("patch"))
            patch.update(context)
            return patch
        return context
    patch = _get_patch(repository, patch_id)
    if patch is None:
        return None
    evidence_ids = patch.get("evidence_ids", [])
    if evidence_ids and hasattr(repository, "list_evidence"):
        patch["evidence"] = [
            _as_dict(item) for item in repository.list_evidence(evidence_ids=evidence_ids)
        ]
    if patch.get("event_id") and hasattr(repository, "get_event"):
        patch["current_event"] = _as_dict(repository.get_event(patch["event_id"]))
    if hasattr(repository, "list_review_actions"):
        patch["review_history"] = [
            _as_dict(item) for item in repository.list_review_actions(patch_id)
        ]
    return patch


def _review(repository: Any, patch_id: str, decision: str, payload: dict | None = None) -> None:
    if hasattr(repository, "review_patch"):
        repository.review_patch(patch_id, decision, edited_payload=payload, reviewer="web")
    elif decision == "approve":
        repository.approve_patch(patch_id, payload=payload)
    elif decision == "reject":
        repository.reject_patch(patch_id)
    else:
        repository.mark_patch_rerun(patch_id)


def _reset_for_rerun(repository: Any, patch_id: str) -> None:
    resetter = getattr(repository, "reset_patch_for_rerun", None)
    if resetter is None:
        raise ValueError("repository does not support rerun revisions")
    resetter(patch_id, reviewer="web")


def _reject(repository: Any, patch_id: str, reason: str) -> None:
    if not hasattr(repository, "review_patch"):
        repository.reject_patch(patch_id, reason=reason)
        return
    parameters = inspect.signature(repository.review_patch).parameters
    if "reason" in parameters:
        repository.review_patch(patch_id, "reject", reviewer="web", reason=reason)
    else:
        repository.review_patch(
            patch_id,
            "reject",
            edited_payload={"review_reason": reason} if reason else None,
            reviewer="web",
        )


def create_app(
    repository: Any,
    resume_callback: Callable[[str], Any] | None = None,
    review_token: str | None = None,
) -> FastAPI:
    """Create the local human-review application around a repository instance."""
    app = FastAPI(title="AIME Event Wiki Review")

    @app.middleware("http")
    async def local_access_only(request: Request, call_next):
        host = (request.url.hostname or "").lower()
        loopback = host in {"localhost", "127.0.0.1", "::1", "testserver"}
        if review_token:
            authorization = request.headers.get("authorization", "")
            supplied = request.headers.get("x-review-token", "")
            if authorization.lower().startswith("bearer "):
                supplied = authorization[7:]
            if not secrets.compare_digest(supplied, review_token):
                return JSONResponse({"detail": "Review token required"}, status_code=401)
        elif not loopback:
            return JSONResponse(
                {"detail": "Review UI is local-only unless a review token is configured"},
                status_code=403,
            )
        return await call_next(request)

    async def resume(patch_id: str, decision: str) -> None:
        if resume_callback is None:
            return
        parameters = list(inspect.signature(resume_callback).parameters.values())
        accepts_decision = any(
            parameter.kind == parameter.VAR_POSITIONAL for parameter in parameters
        )
        accepts_decision = accepts_decision or len(parameters) >= 2
        result = (
            resume_callback(patch_id, decision) if accepts_decision else resume_callback(patch_id)
        )
        if inspect.isawaitable(result):
            await result

    @app.get("/reviews")
    async def reviews(request: Request):
        return _TEMPLATES.TemplateResponse(
            request=request,
            name="reviews.html",
            context={"patches": _pending(repository)},
        )

    @app.get("/reviews/{patch_id}")
    async def review_detail(request: Request, patch_id: str):
        patch = _get_context(repository, patch_id)
        if patch is None:
            raise HTTPException(status_code=404, detail="Patch not found")
        return _TEMPLATES.TemplateResponse(
            request=request,
            name="review_detail.html",
            context={
                "patch": patch,
                "payload_json": json.dumps(patch.get("payload", {}), indent=2),
                "current_event_json": json.dumps(
                    patch.get("current_event", {}), indent=2, default=str
                ),
                "review_history_json": json.dumps(
                    patch.get("review_history", []), indent=2, default=str
                ),
            },
        )

    @app.post("/reviews/{patch_id}/approve")
    async def approve(patch_id: str):
        if _get_patch(repository, patch_id) is None:
            raise HTTPException(status_code=404, detail="Patch not found")
        try:
            _review(repository, patch_id, "approve")
            await resume(patch_id, "approve")
        except ValueError as error:
            raise HTTPException(status_code=409, detail=str(error)) from error
        return RedirectResponse("/reviews", status_code=303)

    @app.post("/reviews/{patch_id}/reject")
    async def reject(request: Request, patch_id: str):
        if _get_patch(repository, patch_id) is None:
            raise HTTPException(status_code=404, detail="Patch not found")
        form = await request.form()
        reason = str(form.get("reason") or "")
        try:
            _reject(repository, patch_id, reason)
            await resume(patch_id, "reject")
        except ValueError as error:
            raise HTTPException(status_code=409, detail=str(error)) from error
        return RedirectResponse("/reviews", status_code=303)

    @app.post("/reviews/{patch_id}/edit")
    async def edit(request: Request, patch_id: str):
        if _get_patch(repository, patch_id) is None:
            raise HTTPException(status_code=404, detail="Patch not found")
        raw_payload = str((await request.form()).get("payload") or "")
        try:
            payload = json.loads(raw_payload)
        except json.JSONDecodeError as error:
            raise HTTPException(status_code=400, detail="Payload must be valid JSON") from error
        if not isinstance(payload, dict):
            raise HTTPException(status_code=400, detail="Payload must be a JSON object")
        try:
            _review(repository, patch_id, "approve", payload)
            await resume(patch_id, "approve")
        except ValueError as error:
            raise HTTPException(status_code=409, detail=str(error)) from error
        return RedirectResponse("/reviews", status_code=303)

    @app.post("/reviews/{patch_id}/rerun")
    async def rerun(patch_id: str):
        if _get_patch(repository, patch_id) is None:
            raise HTTPException(status_code=404, detail="Patch not found")
        try:
            _reset_for_rerun(repository, patch_id)
            await resume(patch_id, "rerun")
        except ValueError as error:
            raise HTTPException(status_code=409, detail=str(error)) from error
        return RedirectResponse("/reviews", status_code=303)

    return app
