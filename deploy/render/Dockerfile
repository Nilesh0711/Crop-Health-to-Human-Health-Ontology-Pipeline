FROM node:22-alpine AS frontend
WORKDIR /build
COPY frontend/package*.json ./
RUN npm ci
COPY frontend/ ./
RUN npm run build

FROM python:3.12-slim
ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1
WORKDIR /app
COPY deploy/render/requirements.txt /app/requirements.txt
RUN pip install --no-cache-dir -r requirements.txt
COPY backend/ /app/backend/
COPY deploy/render/snapshot/ /app/deploy/render/snapshot/
COPY --from=frontend /build/dist/ /app/frontend/dist/
RUN useradd --create-home viewer && chown -R viewer:viewer /app
USER viewer
EXPOSE 8000
CMD ["sh", "-c", "exec uvicorn backend.viewer.app:app --host 0.0.0.0 --port ${PORT:-8000}"]
