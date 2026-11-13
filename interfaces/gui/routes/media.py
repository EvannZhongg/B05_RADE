"""Signed display of run images produced by tools."""

import base64
import hashlib
import hmac
import json
import secrets
import time
from pathlib import Path

from fastapi import APIRouter, Body, HTTPException
from fastapi.responses import FileResponse

from agent_core import JsonlSessionStore, UnsupportedImageError, Workspace, probe_image

from .run_files import run_files


IMAGE_URL_TTL_SECONDS = 5 * 60


def create_media_router(
    store: JsonlSessionStore,
) -> APIRouter:
    router = APIRouter()
    image_url_secret = secrets.token_bytes(32)

    def resolve_image_path(
        files: Workspace,
        path: str,
    ) -> tuple[Path, str]:
        try:
            resolved = files.resolve_path(path)
            info = probe_image(resolved, max_bytes=None)
        except (UnsupportedImageError, OSError, ValueError) as error:
            raise HTTPException(status_code=404, detail="图片不存在。") from error
        return resolved, info.mime_type

    def sign_image_path(
        path: str,
        session_id: str,
    ) -> tuple[str, int]:
        expires_at = int(time.time()) + IMAGE_URL_TTL_SECONDS
        payload = json.dumps(
            {
                "session_id": session_id,
                "path": path,
                "expires_at": expires_at,
            },
            ensure_ascii=False,
            separators=(",", ":"),
        ).encode("utf-8")
        encoded = base64.urlsafe_b64encode(payload).rstrip(b"=")
        signature = hmac.new(
            image_url_secret,
            encoded,
            hashlib.sha256,
        ).digest()
        token = (
            encoded.decode("ascii")
            + "."
            + base64.urlsafe_b64encode(signature).rstrip(b"=").decode("ascii")
        )
        return f"/api/images/{token}", expires_at

    def decode_image_token(token: str) -> tuple[Workspace, str, int]:
        try:
            encoded, encoded_signature = token.split(".", 1)
            payload_bytes = encoded.encode("ascii")
            signature = base64.urlsafe_b64decode(
                encoded_signature + "=" * (-len(encoded_signature) % 4)
            )
        except ValueError as error:
            raise HTTPException(
                status_code=404,
                detail="图片链接无效。",
            ) from error
        expected = hmac.new(
            image_url_secret,
            payload_bytes,
            hashlib.sha256,
        ).digest()
        if not hmac.compare_digest(signature, expected):
            raise HTTPException(status_code=404, detail="图片链接无效。")
        try:
            payload = json.loads(
                base64.urlsafe_b64decode(
                    encoded + "=" * (-len(encoded) % 4)
                )
            )
        except ValueError as error:
            raise HTTPException(
                status_code=404,
                detail="图片链接无效。",
            ) from error
        if not isinstance(payload, dict):
            raise HTTPException(status_code=404, detail="图片链接无效。")
        path = payload.get("path")
        session_id = payload.get("session_id")
        expires_at = payload.get("expires_at")
        if (
            not isinstance(path, str)
            or not path
            or not isinstance(session_id, str)
            or not session_id
            or isinstance(expires_at, bool)
            or not isinstance(expires_at, int)
        ):
            raise HTTPException(status_code=404, detail="图片链接无效。")
        if expires_at <= int(time.time()):
            raise HTTPException(status_code=410, detail="图片链接已过期。")
        return run_files(store, session_id), path, expires_at


    @router.post("/api/image-url")
    async def create_image_url(payload: object = Body(...)) -> dict[str, object]:
        if not isinstance(payload, dict):
            raise HTTPException(status_code=400, detail="请求必须是对象。")
        path = payload.get("path")
        session_id = payload.get("session_id")
        if not isinstance(path, str) or not path:
            raise HTTPException(status_code=400, detail="path 不能为空。")
        if not isinstance(session_id, str) or not session_id:
            raise HTTPException(status_code=400, detail="session_id 无效。")
        files = run_files(store, session_id)
        resolve_image_path(files, path)
        url, expires_at = sign_image_path(path, session_id)
        return {"url": url, "expires_at": expires_at}

    @router.get("/api/images/{token}")
    async def get_signed_image(token: str) -> FileResponse:
        files, path, expires_at = decode_image_token(token)
        resolved, mime_type = resolve_image_path(files, path)
        return FileResponse(
            resolved,
            media_type=mime_type,
            headers={
                "Cache-Control": (
                    "private, max-age="
                    f"{max(0, expires_at - int(time.time()))}"
                )
            },
        )

    return router
