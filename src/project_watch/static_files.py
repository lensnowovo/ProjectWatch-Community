"""Serve product assets independently of host MIME registry overrides."""

import os
from pathlib import Path

from fastapi.staticfiles import StaticFiles
from starlette.responses import FileResponse, Response
from starlette.types import Scope

_ASSET_MEDIA_TYPES = {
    '.js': 'text/javascript', '.mjs': 'text/javascript', '.css': 'text/css',
    '.html': 'text/html', '.svg': 'image/svg+xml', '.woff': 'font/woff',
    '.woff2': 'font/woff2', '.png': 'image/png', '.ico': 'image/x-icon',
    '.jpg': 'image/jpeg', '.jpeg': 'image/jpeg', '.json': 'application/json',
}


class ProductStaticFiles(StaticFiles):
    def file_response(self, full_path: str | os.PathLike[str], stat_result: os.stat_result,
                      scope: Scope, status_code: int = 200) -> Response:
        response = super().file_response(full_path, stat_result, scope, status_code)
        media_type = _ASSET_MEDIA_TYPES.get(Path(full_path).suffix.lower())
        # Keep Starlette's path, ETag, HEAD/range and 304 behavior. Only replace
        # the type of known product files; never mutate process-wide MIME maps.
        if media_type and isinstance(response, FileResponse):
            response.media_type = media_type
            response.headers['content-type'] = (
                media_type + '; charset=utf-8' if media_type.startswith('text/') else media_type
            )
        return response


class NoStoreStaticFiles(ProductStaticFiles):
    async def get_response(self, path: str, scope: Scope) -> Response:
        response = await super().get_response(path, scope)
        response.headers["Cache-Control"] = "no-store"
        return response
