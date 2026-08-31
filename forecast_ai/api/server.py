"""
FastAPI Server for Forecast AI.
"""

import asyncio
import logging
import os
from typing import Optional
import uvicorn
from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from . import routes
from ..pipelines.forecast import ForecastPipeline
from ..config import ForecastConfig
from ..config_store import ConfigStore
from ..proof.publisher import ProofPublisher
from ..services.robinhood_stock_tokens import RobinhoodStockTokenClient

logger = logging.getLogger(__name__)

class ApiServer:
    def __init__(self, config: ForecastConfig, pipeline: ForecastPipeline):
        self.config = config
        self.pipeline = pipeline
        self.app = FastAPI(title="Forecast AI API", version="0.2.0")
        self._server_task: Optional[asyncio.Task] = None
        self.proof_publisher = ProofPublisher(config.robinhood_chain)
        self._proof_resolution_task: Optional[asyncio.Task] = None
        self._init_app()

    def _init_app(self):
        # The website normally calls this API server-side, but direct clients
        # still need a controlled CORS policy when the API is exposed publicly.
        configured_origins = os.getenv("CORS_ALLOW_ORIGINS", "").strip()
        allow_origins = [origin.strip() for origin in configured_origins.split(",") if origin.strip()]
        if not allow_origins:
            allow_origins = ["*"]
        self.app.add_middleware(
            CORSMiddleware,
            allow_origins=allow_origins,
            allow_methods=["*"],
            allow_headers=["*"],
        )

        # Set pipeline reference in routes
        routes._pipeline = self.pipeline
        routes._proof_publisher = self.proof_publisher
        routes._stock_tokens = RobinhoodStockTokenClient(
            base_url=self.config.robinhood_chain.stock_token_api_url,
        )
        self.app.include_router(routes.router)

        @self.app.on_event("startup")
        async def start_proof_publisher():
            await self.proof_publisher.start()
            if self.proof_publisher.configured:
                self._proof_resolution_task = asyncio.create_task(
                    self._run_resolution_loop(), name="forecast-proof-resolver"
                )

        @self.app.on_event("shutdown")
        async def stop_proof_publisher():
            if self._proof_resolution_task:
                self._proof_resolution_task.cancel()
                try:
                    await self._proof_resolution_task
                except asyncio.CancelledError:
                    pass
                self._proof_resolution_task = None
            await self.proof_publisher.stop()

    async def _run_resolution_loop(self) -> None:
        """Queue official market outcomes and due RWA prices without an LLM."""
        while True:
            try:
                await self.pipeline.resolve_due_forecasts()
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                logger.warning("[ProofResolver] Resolution check failed: %s", exc)
            await asyncio.sleep(self.config.robinhood_chain.resolution_interval_seconds)

    async def start(self):
        """
        Starts the API server asynchronously and launches the public feed update loop.
        """
        host = self.config.server.host
        port = self.config.server.port
        logger.info(f"Starting API Server on {host}:{port}...")

        config = uvicorn.Config(
            app=self.app,
            host=host,
            port=port,
            log_level="info",
            loop="asyncio"
        )
        server = uvicorn.Server(config)
        self._server_task = asyncio.create_task(server.serve())

    def stop(self):
        if self._server_task:
            self._server_task.cancel()
            logger.info("API Server stopped.")

# Default top-level ASGI application instance for Uvicorn / Gunicorn / Railway / Render
# IMPORTANT: must use ConfigStore().load_config() — NOT ForecastConfig() directly —
# so that Railway/Render environment variables (OPENAI_API_KEY etc.) are applied.
_default_config = ConfigStore().load_config()
_default_pipeline = ForecastPipeline(_default_config)
_default_server = ApiServer(_default_config, _default_pipeline)
app = _default_server.app
