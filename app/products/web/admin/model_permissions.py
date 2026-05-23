"""Admin model-permission diagnostics for account pools."""

from dataclasses import dataclass
from typing import TYPE_CHECKING, Awaitable, Callable, Literal

import orjson
from fastapi import APIRouter, Depends, Query
from fastapi.responses import Response
from pydantic import BaseModel, Field

from app.control.account.state_machine import is_manageable
from app.control.model import registry as model_registry
from app.control.model.enums import ModeId
from app.control.model.spec import ModelSpec
from app.dataplane.reverse.protocol.xai_chat import classify_line
from app.platform.errors import UpstreamError, ValidationError

if TYPE_CHECKING:
    from app.control.account.models import AccountRecord
    from app.control.account.repository import AccountRepository

from . import get_repo

router = APIRouter(prefix="/model-permissions", tags=["Admin - Model Permissions"])

_POOL_ID_TO_NAME = {0: "basic", 1: "super", 2: "heavy"}
_VALID_POOLS = {"basic", "super", "heavy"}
_CLOUDFLARE_MARKERS = ("just a moment", "cf-challenge", "cf-mitigated", "cloudflare")

PermissionStatus = Literal[
    "supported",
    "no_accounts",
    "pool_not_routed",
    "unsupported_probe_type",
    "no_quota_or_entitlement",
    "upstream_model_not_found",
    "invalid_credentials",
    "cloudflare_challenge",
    "upstream_error",
]


class ModelPermissionCheckRequest(BaseModel):
    models: list[str] = Field(default_factory=list)
    pools: list[str] = Field(default_factory=list)
    sample_size: int = Field(1, ge=1, le=20)
    timeout_s: float = Field(30.0, ge=5.0, le=180.0)


@dataclass(slots=True)
class ProbeOutcome:
    status: PermissionStatus
    message: str = ""
    status_code: int | None = None
    upstream_body: str = ""


ProbeFunc = Callable[[str, ModelSpec, float], Awaitable[ProbeOutcome]]


def _json(data) -> Response:
    return Response(content=orjson.dumps(data), media_type="application/json")


def _mask(token: str) -> str:
    return f"{token[:8]}...{token[-8:]}" if len(token) > 20 else token


def _capability_name(spec: ModelSpec) -> str:
    if spec.is_image_edit():
        return "image_edit"
    if spec.is_image():
        return "image"
    if spec.is_video():
        return "video"
    if spec.is_voice():
        return "voice"
    return "chat"


def _pool_names(spec: ModelSpec) -> list[str]:
    return [_POOL_ID_TO_NAME[pool_id] for pool_id in spec.pool_candidates()]


def _normalize_pools(pools: list[str] | None) -> list[str]:
    if not pools:
        return ["basic", "super", "heavy"]
    normalized: list[str] = []
    for pool in pools:
        value = str(pool or "").strip().lower()
        if value not in _VALID_POOLS:
            raise ValidationError(
                "pool must be one of [basic, super, heavy]",
                param="pools",
            )
        if value not in normalized:
            normalized.append(value)
    return normalized


def _normalize_models(models: list[str] | None) -> list[ModelSpec]:
    if not models:
        return [spec for spec in model_registry.list_enabled() if spec.is_chat()]
    result: list[ModelSpec] = []
    for model in models:
        spec = model_registry.get(model)
        if spec is None or not spec.enabled:
            raise ValidationError(
                f"Model {model!r} does not exist.",
                param="models",
                code="model_not_found",
            )
        if spec not in result:
            result.append(spec)
    return result


def _is_cloudflare_403(exc: UpstreamError) -> bool:
    if exc.status != 403:
        return False
    body = str(getattr(exc, "details", {}).get("body", "") or "")
    haystack = f"{body} {exc}".lower()
    return any(marker in haystack for marker in _CLOUDFLARE_MARKERS)


def _outcome_from_error(exc: UpstreamError) -> ProbeOutcome:
    body = str(getattr(exc, "details", {}).get("body", "") or "")
    if exc.status == 404:
        return ProbeOutcome(
            status="upstream_model_not_found",
            message=str(exc),
            status_code=exc.status,
            upstream_body=body[:400],
        )
    if exc.status in {403, 429} and not _is_cloudflare_403(exc):
        return ProbeOutcome(
            status="no_quota_or_entitlement",
            message=str(exc),
            status_code=exc.status,
            upstream_body=body[:400],
        )
    if exc.status == 401:
        return ProbeOutcome(
            status="invalid_credentials",
            message=str(exc),
            status_code=exc.status,
            upstream_body=body[:400],
        )
    if exc.status == 403 and _is_cloudflare_403(exc):
        return ProbeOutcome(
            status="cloudflare_challenge",
            message=str(exc),
            status_code=exc.status,
            upstream_body=body[:400],
        )
    return ProbeOutcome(
        status="upstream_error",
        message=str(exc),
        status_code=exc.status,
        upstream_body=body[:400],
    )


async def _probe_chat_model(
    token: str,
    spec: ModelSpec,
    timeout_s: float,
) -> ProbeOutcome:
    from app.products.openai.chat import _stream_chat

    try:
        async for line in _stream_chat(
            token=token,
            mode_id=ModeId(spec.mode_id),
            message="[user]: reply OK only",
            files=[],
            spec=spec,
            request_overrides=None,
            timeout_s=timeout_s,
        ):
            event_type, data = classify_line(line)
            if event_type == "done":
                break
            if event_type == "data" and data:
                break
    except UpstreamError as exc:
        return _outcome_from_error(exc)
    return ProbeOutcome(status="supported", message="probe succeeded", status_code=200)


def _aggregate_failures(outcomes: list[ProbeOutcome]) -> ProbeOutcome:
    for status in (
        "upstream_model_not_found",
        "no_quota_or_entitlement",
        "invalid_credentials",
        "cloudflare_challenge",
        "upstream_error",
    ):
        for outcome in outcomes:
            if outcome.status == status:
                return outcome
    return ProbeOutcome(status="upstream_error", message="probe failed")


async def _detect_one(
    spec: ModelSpec,
    pool: str,
    records: list["AccountRecord"],
    *,
    sample_size: int,
    timeout_s: float,
    probe_func: ProbeFunc,
) -> dict:
    required_pools = _pool_names(spec)
    base = {
        "model": spec.model_name,
        "public_name": spec.public_name,
        "capability": _capability_name(spec),
        "pool": pool,
        "required_pools": required_pools,
        "upstream_profile": spec.upstream_profile,
        "upstream_model": spec.upstream_model_name(),
        "mode_id": int(spec.mode_id),
    }

    if pool not in required_pools:
        return {
            **base,
            "status": "pool_not_routed",
            "message": "This pool is not in the model's routing candidates.",
            "accounts_checked": 0,
            "status_code": None,
            "sampled_accounts": [],
        }

    if not spec.is_chat():
        return {
            **base,
            "status": "unsupported_probe_type",
            "message": "Only chat models are probed by the account-pool permission checker.",
            "accounts_checked": 0,
            "status_code": None,
            "sampled_accounts": [],
        }

    candidates = [record for record in records if record.pool == pool]
    if not candidates:
        return {
            **base,
            "status": "no_accounts",
            "message": "No active/manageable accounts are available in this pool.",
            "accounts_checked": 0,
            "status_code": None,
            "sampled_accounts": [],
        }

    sampled = candidates[:sample_size]
    outcomes: list[ProbeOutcome] = []
    for record in sampled:
        try:
            outcome = await probe_func(record.token, spec, timeout_s)
        except UpstreamError as exc:
            outcome = _outcome_from_error(exc)
        except Exception as exc:
            outcome = ProbeOutcome(status="upstream_error", message=str(exc), status_code=502)
        outcomes.append(outcome)
        if outcome.status == "supported":
            return {
                **base,
                "status": "supported",
                "message": outcome.message or "At least one sampled account can use this model.",
                "accounts_checked": len(outcomes),
                "status_code": outcome.status_code or 200,
                "sampled_accounts": [_mask(r.token) for r in sampled[: len(outcomes)]],
            }

    final = _aggregate_failures(outcomes)
    return {
        **base,
        "status": final.status,
        "message": final.message,
        "accounts_checked": len(outcomes),
        "status_code": final.status_code,
        "upstream_body": final.upstream_body,
        "sampled_accounts": [_mask(r.token) for r in sampled],
    }


async def detect_model_permissions(
    repo: "AccountRepository",
    *,
    models: list[str] | None = None,
    pools: list[str] | None = None,
    sample_size: int = 1,
    timeout_s: float = 30.0,
    probe_func: ProbeFunc = _probe_chat_model,
) -> dict:
    specs = _normalize_models(models)
    pool_names = _normalize_pools(pools)
    sample_size = max(1, min(20, int(sample_size)))
    timeout_s = max(5.0, min(180.0, float(timeout_s)))

    snapshot = await repo.runtime_snapshot()
    manageable = [record for record in snapshot.items if is_manageable(record)]

    results: list[dict] = []
    for spec in specs:
        for pool in pool_names:
            results.append(
                await _detect_one(
                    spec,
                    pool,
                    manageable,
                    sample_size=sample_size,
                    timeout_s=timeout_s,
                    probe_func=probe_func,
                )
            )

    counts: dict[str, int] = {}
    for item in results:
        status = str(item.get("status") or "unknown")
        counts[status] = counts.get(status, 0) + 1

    return {
        "status": "success",
        "summary": {
            "models": len(specs),
            "pools": len(pool_names),
            "checked": len(results),
            "by_status": counts,
        },
        "results": results,
    }


@router.post("/check")
async def check_model_permissions(
    req: ModelPermissionCheckRequest,
    repo: "AccountRepository" = Depends(get_repo),
):
    return _json(
        await detect_model_permissions(
            repo,
            models=req.models,
            pools=req.pools,
            sample_size=req.sample_size,
            timeout_s=req.timeout_s,
        )
    )


@router.get("/check")
async def check_model_permissions_get(
    models: str = Query("", description="Comma-separated model ids"),
    pools: str = Query("", description="Comma-separated pools"),
    sample_size: int = Query(1, ge=1, le=20),
    timeout_s: float = Query(30.0, ge=5.0, le=180.0),
    repo: "AccountRepository" = Depends(get_repo),
):
    model_list = [part.strip() for part in models.split(",") if part.strip()]
    pool_list = [part.strip() for part in pools.split(",") if part.strip()]
    return _json(
        await detect_model_permissions(
            repo,
            models=model_list,
            pools=pool_list,
            sample_size=sample_size,
            timeout_s=timeout_s,
        )
    )
