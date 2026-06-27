"""WebUI image studio API routes."""

from typing import Annotated

from fastapi import APIRouter, Depends, File, Form, UploadFile
from fastapi.responses import JSONResponse

from app.control.model import registry as model_registry
from app.platform.auth.middleware import WebUIUser, verify_webui_key
from app.platform.errors import ValidationError
from app.products.openai.router import (
    _coalesce_uploads,
    _uploads_to_data_uris,
    _validate_image_edit_n,
    _validate_image_n,
)
from app.products.openai.schemas import ImageGenerationRequest

router = APIRouter(prefix="/webui/api", tags=["WebUI - Images"])


def _model_payload(spec) -> dict:
    if spec.is_image_edit():
        capability = "image_edit"
    elif spec.is_image():
        capability = "image"
    else:
        capability = "unknown"
    return {
        "id": spec.model_name,
        "name": spec.public_name,
        "capability": capability,
        "upstream_profile": spec.upstream_profile,
    }


@router.get("/images/models")
async def list_webui_image_models(_user: WebUIUser = Depends(verify_webui_key)):
    generation = [
        _model_payload(spec)
        for spec in model_registry.list_enabled()
        if spec.is_image()
    ]
    edits = [
        _model_payload(spec)
        for spec in model_registry.list_enabled()
        if spec.is_image_edit()
    ]
    return JSONResponse(
        {
            "object": "list",
            "generation": generation,
            "edits": edits,
        }
    )


@router.post("/images/generations")
async def webui_image_generations(
    req: ImageGenerationRequest,
    _user: WebUIUser = Depends(verify_webui_key),
):
    spec = model_registry.get(req.model)
    if spec is None or not spec.enabled or not spec.is_image():
        raise ValidationError(f"Model {req.model!r} is not an image model", param="model")
    _validate_image_n(req.model, req.n or 1, param="n")

    from app.products.openai.images import generate as image_generate

    result = await image_generate(
        model=req.model,
        prompt=req.prompt,
        n=req.n or 1,
        size=req.size or "1024x1024",
        response_format=req.response_format or "url",
        stream=False,
        chat_format=False,
    )
    return JSONResponse(result)


@router.post("/images/edits")
async def webui_image_edits(
    prompt: Annotated[str, Form(...)],
    model: Annotated[str, Form()] = "grok-imagine-image-edit",
    image: Annotated[list[UploadFile] | None, File(alias="image")] = None,
    image_array: Annotated[list[UploadFile] | None, File(alias="image[]")] = None,
    mask: Annotated[UploadFile | None, File()] = None,
    n: Annotated[int, Form()] = 1,
    size: Annotated[str, Form()] = "1024x1024",
    response_format: Annotated[str, Form()] = "url",
    _user: WebUIUser = Depends(verify_webui_key),
):
    spec = model_registry.get(model)
    if spec is None or not spec.enabled or not spec.is_image_edit():
        raise ValidationError(f"Model {model!r} is not an image-edit model", param="model")
    _validate_image_edit_n(n, param="n")

    image_uploads = _coalesce_uploads(image, image_array)
    if not image_uploads:
        raise ValidationError("image is required", param="image")

    from app.products.openai.images import edit as image_edit

    image_inputs = await _uploads_to_data_uris(image_uploads, mask=mask)
    content = [{"type": "text", "text": prompt}]
    content.extend(
        {"type": "image_url", "image_url": {"url": image_input}}
        for image_input in image_inputs
    )
    result = await image_edit(
        model=model,
        messages=[{"role": "user", "content": content}],
        n=n,
        size=size or "1024x1024",
        response_format=response_format or "url",
        stream=False,
        chat_format=False,
    )
    return JSONResponse(result)


__all__ = ["router"]
