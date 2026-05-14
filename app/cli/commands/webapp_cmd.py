"""`webapp` subcommand — run the FastAPI server in the foreground."""

from __future__ import annotations

import argparse
import logging

from .base import BaseCommand

logger = logging.getLogger(__name__)


class WebappCommand(BaseCommand):
    @classmethod
    def add_parser(cls, subparsers: argparse._SubParsersAction) -> None:
        p = subparsers.add_parser(
            "webapp",
            help="Run the FastAPI webapp in the foreground (no tray)",
        )
        p.add_argument("--host", default=None, help="Override host")
        p.add_argument("--port", type=int, default=None, help="Override port")

    def execute(self, args: argparse.Namespace) -> int:
        # Lazy imports so a plain `webapp` subcommand doesn't drag pystray in.
        import uvicorn

        from app.webapp.manager import cert_paths
        from src.webapp_config import load_webapp_config

        cfg = load_webapp_config()
        host = args.host or cfg.host
        port = args.port or cfg.port

        certs = cert_paths()
        kwargs = {
            "host": host,
            "port": port,
            "log_level": "info",
        }
        if certs is not None:
            cert, key = certs
            kwargs["ssl_certfile"] = str(cert)
            kwargs["ssl_keyfile"] = str(key)
            scheme = "https"
        else:
            scheme = "http"

        logger.info(
            f"🚀 launcher webapp on {scheme}://{host if host != '0.0.0.0' else '127.0.0.1'}:{port}"
        )
        uvicorn.run("app.webapp.server:app", **kwargs)
        return 0
