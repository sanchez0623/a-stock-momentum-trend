# ============================================================
# 多阶段构建: node 构建前端 -> python 运行时(单容器, 端口 8000)
# ============================================================

# ---------- 阶段1: 构建前端 ----------
FROM node:22-alpine AS frontend-build
WORKDIR /build/frontend
COPY frontend/package*.json ./
RUN npm ci
COPY frontend/ ./
RUN npm run build

# ---------- 阶段2: 运行时 ----------
FROM python:3.11-slim AS runtime
ENV TZ=Asia/Shanghai \
    LANG=C.UTF-8 \
    PYTHONUNBUFFERED=1 \
    DATA_DIR=/app/data \
    PIP_NO_CACHE_DIR=1
WORKDIR /app/backend

# 先装依赖(利用缓存层)
COPY backend/requirements.txt ./requirements.txt
RUN pip install -r requirements.txt

# 再拷代码(落在 /app/backend, uvicorn 从此目录 import app)
COPY backend/ ./
COPY --from=frontend-build /build/frontend/dist/ /app/frontend/dist/

VOLUME ["/app/data"]
EXPOSE 8000

HEALTHCHECK --interval=30s --timeout=5s --start-period=15s --retries=3 \
  CMD ["python", "-c", "import urllib.request;urllib.request.urlopen('http://127.0.0.1:8000/api/health', timeout=4)"]

CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000"]
