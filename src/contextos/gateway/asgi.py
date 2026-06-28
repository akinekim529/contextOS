"""ASGI entrypoint: ``uvicorn contextos.gateway.asgi:app``.

Settings are read from the environment (CONTEXTOS_*). The app is stateless, so any number of
replicas can serve behind a load balancer.
"""

from __future__ import annotations

from .app import create_app

app = create_app()
