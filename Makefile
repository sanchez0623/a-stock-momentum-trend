# === 常用命令 ===
PYTHON ?= python
PIP ?= pip

.PHONY: help install install-dev dev-backend dev-frontend test lint build up down logs backup

help: ## 显示帮助
	@grep -E '^[a-zA-Z_-]+:.*?## .*$$' $(MAKEFILE_LIST) | awk 'BEGIN {FS = ":.*?## "}; {printf "  \033[36m%-16s\033[0m %s\n", $$1, $$2}'

install: ## 安装后端核心依赖
	$(PIP) install -r backend/requirements.txt

install-dev: ## 安装后端开发依赖(含 ruff/pytest/mypy)
	$(PIP) install -r backend/requirements-dev.txt

install-optional: ## 安装可选数据源(mootdx/akshare)
	$(PIP) install -e ".[optional]"

dev-backend: ## 本地启动后端(热重载, 8000)
	cd backend && uvicorn app.main:app --reload --host 0.0.0.0 --port 8000

dev-frontend: ## 本地启动前端(Vite HMR, 5173)
	cd frontend && npm run dev

test: ## 后端测试
	cd backend && python -m pytest

lint: ## 后端 lint + 类型检查
	cd backend && ruff check app cli tests && mypy app

build: ## 构建前端产物(输出 frontend/dist)
	cd frontend && npm run build

up: ## 生产模式一键启动(Docker)
	docker compose up -d --build

dev: ## 开发模式双容器(Docker)
	docker compose -f docker-compose.dev.yml up --build

logs: ## 查看容器日志
	docker compose logs -f

backup: ## 备份数据目录
	docker run --rm -v "$(PWD)/data":/data -v "$(PWD)":/backup alpine \
		tar czf /backup/data-$$(date +%F).tar.gz /data
