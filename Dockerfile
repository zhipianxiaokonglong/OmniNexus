FROM python:3.13-slim AS runtime

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    OMNI_HOST=0.0.0.0 \
    OMNI_PORT=8000

WORKDIR /app

COPY pyproject.toml README.md LICENSE ./
COPY src ./src
COPY config ./config

RUN pip install --upgrade pip \
    && pip install . \
    && useradd --create-home --uid 10001 omninexus \
    && mkdir -p /app/data \
    && chown -R omninexus:omninexus /app

USER omninexus
VOLUME ["/app/data"]
EXPOSE 8000

HEALTHCHECK --interval=30s --timeout=5s --start-period=10s --retries=3 \
    CMD python -c "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8000/v1/healthz', timeout=3)"

CMD ["omninexus", "--host", "0.0.0.0", "--port", "8000"]
