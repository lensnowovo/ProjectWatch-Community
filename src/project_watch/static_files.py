"""Static browser assets must refresh together after an alpha upgrade."""

from fastapi.staticfiles import StaticFiles
from starlette.responses import Response
from starlette.types import Scope


class NoStoreStaticFiles(StaticFiles):
    async def get_response(self, path: str, scope: Scope) -> Response:
        response = await super().get_response(path, scope)
        response.headers["Cache-Control"] = "no-store"
        return response
