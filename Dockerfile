# Multi-stage build -> distroless runtime (small attack surface, no shell in the final image).
FROM python:3.11-slim AS builder
WORKDIR /app
ENV PIP_DISABLE_PIP_VERSION_CHECK=1 PIP_NO_CACHE_DIR=1
COPY pyproject.toml README.md ./
COPY src ./src
# Build a wheel and install it plus deps into a relocatable prefix.
RUN pip install --upgrade build && python -m build --wheel \
    && pip install --prefix=/install dist/*.whl

FROM gcr.io/distroless/python3-debian12:nonroot
WORKDIR /app
COPY --from=builder /install /usr/local
ENV PYTHONPATH=/usr/local/lib/python3.11/site-packages
EXPOSE 8080
# Stateless gateway; scale horizontally. uvicorn with uvloop for high-throughput ASGI.
ENTRYPOINT ["python", "-m", "uvicorn", "contextos.gateway.asgi:app", "--host", "0.0.0.0", "--port", "8080"]
