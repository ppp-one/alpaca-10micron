import logging
from contextlib import asynccontextmanager

from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse, PlainTextResponse
from starlette.exceptions import HTTPException as StarletteHTTPException

from alpaca_10micron.alpaca import next_server_transaction_id
from alpaca_10micron.api import common, management, telescope
from alpaca_10micron.config import AppConfig
from alpaca_10micron.device import TelescopeDevice
from alpaca_10micron.discovery import start_discovery
from alpaca_10micron.errors import AlpacaError, BadRequest
from alpaca_10micron.mount import Mount

logger = logging.getLogger(__name__)

API_PREFIX = "/api/v1"


def _client_transaction_id(request: Request) -> int:
    # Set by read_request(); 0 if the request failed before its parameters were read.
    alpaca = getattr(request.state, "alpaca", None)
    return alpaca.client_transaction_id if alpaca else 0


def create_app(config: AppConfig) -> FastAPI:
    @asynccontextmanager
    async def lifespan(app: FastAPI):
        discovery = None
        if config.server.discovery:
            try:
                discovery = await start_discovery(config.server.port, config.server.discovery_port)
            except OSError as exc:
                logger.warning("Alpaca discovery is off: %s", exc)
        try:
            yield
        finally:
            await app.state.telescope.shutdown()
            if discovery is not None:
                discovery.close()

    app = FastAPI(
        lifespan=lifespan,
        title="10Micron Alpaca driver",
        description="ASCOM Alpaca telescope driver for 10Micron mounts.",
    )
    app.state.config = config
    app.state.telescope = TelescopeDevice(Mount(config.mount), config.device)

    @app.exception_handler(AlpacaError)
    async def alpaca_error_handler(request: Request, exc: AlpacaError):
        # ASCOM errors are HTTP 200 with ErrorNumber and ErrorMessage.
        return JSONResponse(
            status_code=200,
            content={
                "ClientTransactionID": _client_transaction_id(request),
                "ServerTransactionID": next_server_transaction_id(),
                "ErrorNumber": exc.code,
                "ErrorMessage": exc.message,
            },
        )

    @app.exception_handler(BadRequest)
    async def bad_request_handler(request: Request, exc: BadRequest):
        return PlainTextResponse(str(exc), status_code=400)

    @app.exception_handler(RequestValidationError)
    async def validation_error_handler(request: Request, exc: RequestValidationError):
        return PlainTextResponse(f"Invalid request: {exc.errors()}", status_code=400)

    @app.exception_handler(StarletteHTTPException)
    async def http_error_handler(request: Request, exc: StarletteHTTPException):
        # An unknown device type or member is a malformed Alpaca request: 400, not 404.
        if exc.status_code in (404, 405) and request.url.path.startswith(API_PREFIX):
            return PlainTextResponse(
                f"Unknown device type, device number or member: {request.url.path}",
                status_code=400,
            )
        return PlainTextResponse(str(exc.detail), status_code=exc.status_code)

    @app.exception_handler(Exception)
    async def unexpected_error_handler(request: Request, exc: Exception):
        logger.exception("Unexpected error in %s %s", request.method, request.url.path)
        return PlainTextResponse(f"Internal driver error: {exc}", status_code=500)

    app.include_router(common.router, prefix=API_PREFIX, tags=["Common"])
    app.include_router(telescope.router, prefix=API_PREFIX, tags=["Telescope"])
    app.include_router(management.router, tags=["Management"])
    return app
