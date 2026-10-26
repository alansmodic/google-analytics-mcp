# Remote (Railway) image: upstream analytics_mcp + multi-account/auth wrapper.
FROM python:3.13-slim
WORKDIR /app

# Install upstream's dependencies first for layer caching.
COPY pyproject.toml README.md ./
COPY analytics_mcp analytics_mcp
RUN pip install --no-cache-dir . uvicorn

COPY remote-server/server.py remote-server/server.py

ENV PYTHONUNBUFFERED=1
CMD ["python", "remote-server/server.py"]
