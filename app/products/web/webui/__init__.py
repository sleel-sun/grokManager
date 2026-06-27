"""WebUI product package."""

from fastapi import APIRouter

from .attachments import router as attachments_router
from .chat import router as chat_router
from .code_preview import router as code_preview_router
from .images import router as images_router
from .imagine import router as imagine_router
from .mcp import router as mcp_router
from .pages import router as pages_router
from .voice import router as voice_router

router = APIRouter()
router.include_router(attachments_router)
router.include_router(chat_router)
router.include_router(code_preview_router)
router.include_router(images_router)
router.include_router(imagine_router, prefix="/webui/api")
router.include_router(mcp_router)
router.include_router(voice_router)
router.include_router(pages_router)

__all__ = ["router"]
