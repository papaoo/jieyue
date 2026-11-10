"""Authenticated, same-origin free-channel status and bounded public smoke test."""
from copy import copy

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, ConfigDict

from api.v1.endpoints.research_delivery import SafeManagementRoute, require_admin
from src.config import get_config
from src.llm.free_models import (
    FreeModelError, FreeModelStore, SUPPORTED_MODELS, apply_runtime_route, catalog, completion,
)

router = APIRouter(route_class=SafeManagementRoute, dependencies=[Depends(require_admin)])


class TestRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    model: str


@router.get("/status")
def status():
    return FreeModelStore(get_config()).status(get_config())


@router.post("/refresh")
def refresh():
    try:
        return catalog(get_config(), refresh=True)
    except FreeModelError as exc:
        raise HTTPException(503, detail={"code": exc.code, "message": str(exc)}) from exc


@router.post("/test")
def test(body: TestRequest):
    if body.model not in SUPPORTED_MODELS:
        raise HTTPException(422, detail={"code": "unknown_model", "message": "不支持的免费模型"})
    config = copy(get_config())
    config.free_model_enabled = True  # Admin smoke is allowed before activation.
    config.free_model_name = body.model
    apply_runtime_route(config)
    try:
        response = completion(config, messages=[{"role": "user", "content": "Reply exactly OK."}],
                              max_tokens=512, timeout=45)
        return {"success": response.choices[0].message.content.strip() == "OK",
                "model": body.model, "reply": response.choices[0].message.content[:200],
                "usage": response.usage.model_dump() if response.usage else None,
                "status": FreeModelStore(config).status(get_config())}
    except FreeModelError as exc:
        return {"success": False, "model": body.model, "code": exc.code, "message": str(exc),
                "status": FreeModelStore(config).status(get_config())}
