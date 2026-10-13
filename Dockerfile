# --- build the map (React + MapLibre) ---------------------------------------------------
FROM node:20-alpine AS web
WORKDIR /web
COPY web/package.json ./
RUN npm install --no-audit --no-fund
COPY web/ ./
RUN npm run build

# --- python service --------------------------------------------------------------------
FROM python:3.12-slim
ENV PYTHONUNBUFFERED=1 PIP_NO_CACHE_DIR=1 OMP_NUM_THREADS=1 TRACKMYFERRY_WEB_DIST=/app/web/dist TRACKMYFERRY_MODEL_DIR=/app/models
WORKDIR /app
COPY pyproject.toml README.md ./
COPY trackmyferry ./trackmyferry
RUN pip install ".[kafka,db,redis]"
COPY models ./models
COPY --from=web /web/dist ./web/dist
RUN useradd --create-home ferry && chown -R ferry /app
USER ferry
EXPOSE 8000 9100
HEALTHCHECK --interval=30s --timeout=5s CMD python -c "import urllib.request,sys; urllib.request.urlopen('http://localhost:8000/api/health', timeout=4)" || exit 1
CMD ["trackmyferry", "run"]
