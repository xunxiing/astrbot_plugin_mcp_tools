from __future__ import annotations

from typing import Any

from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.openapi.utils import get_openapi
from pydantic import BaseModel, ValidationError
from starlette.middleware.cors import CORSMiddleware
from starlette.responses import JSONResponse, Response

from astrbot.api import logger

from .registry import ENDPOINTS, collect_models
from .services import GatewayServices
from .utils import GatewayError


def build_app(plugin: Any) -> FastAPI:
    docs_enabled = bool(getattr(plugin.config, "enable_docs", True))
    docs_path = str(getattr(plugin.config, "docs_path", "/docs") or "/docs")
    openapi_path = str(
        getattr(plugin.config, "openapi_path", "/openapi.json") or "/openapi.json"
    )

    app = FastAPI(
        title="AstrBot REST Gateway",
        version=str(getattr(plugin, "version", "0.2.0")),
        docs_url=docs_path if docs_enabled else None,
        redoc_url=None,
        openapi_url=openapi_path if docs_enabled else None,
    )

    allow_origins = list(getattr(plugin.config, "cors_allow_origins", []) or [])
    app.add_middleware(
        CORSMiddleware,
        allow_origins=allow_origins or ["*"],
        allow_credentials=True,
        allow_methods=["*"],
        allow_headers=["*"],
    )

    services = GatewayServices(plugin)
    setattr(plugin, "_gateway_services", services)
    public_paths = {endpoint.path for endpoint in ENDPOINTS if endpoint.public}
    if docs_enabled:
        public_paths.add(openapi_path)
        public_paths.add(docs_path)

    @app.middleware("http")
    async def bearer_auth(request: Request, call_next):
        path = request.url.path
        if path in public_paths:
            return await call_next(request)

        expected = str(getattr(plugin.config, "auth_token", "") or "").strip()
        if not expected:
            return JSONResponse(
                status_code=503,
                content={
                    "ok": False,
                    "error": {"message": "auth_token is not configured"},
                },
            )
        header = request.headers.get("Authorization", "")
        prefix = "Bearer "
        if not header.startswith(prefix):
            return JSONResponse(
                status_code=401,
                content={"ok": False, "error": {"message": "missing bearer token"}},
            )
        token = header[len(prefix) :]
        if expected and token != expected:
            return JSONResponse(
                status_code=401,
                content={"ok": False, "error": {"message": "invalid bearer token"}},
            )
        return await call_next(request)

    @app.exception_handler(GatewayError)
    async def handle_gateway_error(request: Request, exc: GatewayError):
        return JSONResponse(
            status_code=exc.status_code,
            content={"ok": False, "error": {"message": exc.message}},
        )

    @app.exception_handler(ValidationError)
    async def handle_validation_error(request: Request, exc: ValidationError):
        return JSONResponse(
            status_code=422,
            content={
                "ok": False,
                "error": {"message": "validation error", "details": exc.errors()},
            },
        )

    @app.exception_handler(RequestValidationError)
    async def handle_request_validation_error(
        request: Request, exc: RequestValidationError
    ):
        return JSONResponse(
            status_code=422,
            content={
                "ok": False,
                "error": {"message": "validation error", "details": exc.errors()},
            },
        )

    @app.exception_handler(Exception)
    async def handle_unexpected_error(request: Request, exc: Exception):
        logger.error("Unhandled REST gateway error", exc_info=exc)
        return JSONResponse(
            status_code=500,
            content={"ok": False, "error": {"message": str(exc)}},
        )

    def make_handler(
        handler_name: str, request_model: type[BaseModel] | None, body_mode: str
    ):
        async def endpoint(request: Request):
            payload = None
            if body_mode == "multipart":
                form = await request.form()
                fields: dict[str, Any] = {}
                files: list[Any] = []
                for key, value in form.multi_items():
                    if hasattr(value, "filename"):
                        files.append(value)
                    else:
                        if key in fields:
                            existing = fields[key]
                            if isinstance(existing, list):
                                existing.append(value)
                            else:
                                fields[key] = [existing, value]
                        else:
                            fields[key] = value
                payload = {"form": fields, "files": files}
            elif request_model is not None:
                try:
                    body = await request.json()
                except ValueError:
                    body = {}
                payload = request_model.model_validate(body)
            handler = getattr(services, handler_name)
            result = await plugin.call_on_astr_loop(handler(request, payload))
            if isinstance(result, Response):
                return result
            return {"ok": True, "data": result}

        return endpoint

    ordered_endpoints = sorted(
        ENDPOINTS,
        key=lambda item: (item.path.count("{"), -len(item.path)),
    )

    for endpoint in ordered_endpoints:
        app.add_api_route(
            endpoint.path,
            make_handler(
                endpoint.handler_name, endpoint.request_model, endpoint.body_mode
            ),
            methods=[endpoint.method],
            tags=list(endpoint.tags),
            summary=endpoint.summary,
            name=endpoint.handler_name,
        )

    def merge_model_schema(components: dict[str, Any], model: type[BaseModel]) -> None:
        schema = model.model_json_schema(ref_template="#/components/schemas/{model}")
        defs = schema.pop("$defs", {})
        for key, value in defs.items():
            components[key] = value
        components[model.__name__] = schema

    def custom_openapi():
        if app.openapi_schema:
            return app.openapi_schema

        schema = get_openapi(
            title=app.title,
            version=app.version,
            routes=app.routes,
            description="Programmatic REST gateway for AstrBot managers, config, events, and runtime resources.",
        )
        components = schema.setdefault("components", {})
        schemas = components.setdefault("schemas", {})
        components.setdefault(
            "securitySchemes",
            {
                "BearerAuth": {
                    "type": "http",
                    "scheme": "bearer",
                    "bearerFormat": "token",
                }
            },
        )

        for model in collect_models():
            merge_model_schema(schemas, model)

        for endpoint in ENDPOINTS:
            operation = (
                schema.get("paths", {})
                .get(endpoint.path, {})
                .get(endpoint.method.lower())
            )
            if not operation:
                continue
            if not endpoint.public:
                operation["security"] = [{"BearerAuth": []}]
            if endpoint.body_mode == "multipart":
                operation["requestBody"] = {
                    "required": True,
                    "content": {
                        "multipart/form-data": {
                            "schema": {
                                "type": "object",
                                "properties": {
                                    "file": {"type": "string", "format": "binary"}
                                },
                            }
                        }
                    },
                }
            elif endpoint.request_model:
                operation["requestBody"] = {
                    "required": True,
                    "content": {
                        "application/json": {
                            "schema": {
                                "$ref": f"#/components/schemas/{endpoint.request_model.__name__}"
                            }
                        }
                    },
                }

        app.openapi_schema = schema
        return app.openapi_schema

    app.openapi = custom_openapi
    return app
