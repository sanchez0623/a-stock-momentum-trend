"""结构化日志框架: JSON Lines 文件(按天轮转) + 控制台.

用法:
    from app.core.logger.setup import setup_logging
    setup_logging()                                   # 启动时调用一次(幂等)
    logger.info("事件", extra={"component": "x"})      # extra 键会进入 JSON 行

日志行示例:
    {"ts": "2026-08-10T21:30:00+0800", "level": "WARNING", "logger": "app.core.report.service",
     "message": "日报 LLM 生成失败, 降级规则模板", "component": "daily_report",
     "llm_model": "deepseek-v4-flash", "exc": "Traceback..."}

环境变量:
    LOG_DIR    日志目录(默认 backend/logs)
    LOG_LEVEL  根级别(默认 INFO)
"""

from __future__ import annotations

import json
import logging
import os
from logging.handlers import TimedRotatingFileHandler
from pathlib import Path

# 标准 LogRecord 属性(extra 中其余键会进入 JSON 行)
_STD_ATTRS = frozenset({
    "name", "msg", "args", "levelname", "levelno", "pathname", "filename",
    "module", "exc_info", "exc_text", "stack_info", "lineno", "funcName",
    "created", "msecs", "relativeCreated", "thread", "threadName",
    "processName", "process", "message", "taskName",
})


class JsonFormatter(logging.Formatter):
    """每行一个 JSON 对象: {ts, level, logger, message, [exc], **extra}."""

    def format(self, record: logging.LogRecord) -> str:
        payload: dict = {
            "ts": self.formatTime(record, "%Y-%m-%dT%H:%M:%S%z"),
            "level": record.levelname,
            "logger": record.name,
            "message": record.getMessage(),
        }
        if record.exc_info:
            payload["exc"] = self.formatException(record.exc_info)
        for k, v in record.__dict__.items():
            if k not in _STD_ATTRS and not k.startswith("_"):
                payload[k] = v
        return json.dumps(payload, ensure_ascii=False)


def setup_logging(log_dir: str | Path | None = None,
                  level: str | None = None) -> Path:
    """配置根日志: 控制台(人类可读) + 文件(JSON Lines 按天轮转, 保留 14 天).

    幂等: 已有文件 handler 时直接返回, 不重复添加(uvicorn/CLI 均可安全调用).
    """
    root = logging.getLogger()
    for h in root.handlers:
        if isinstance(h, TimedRotatingFileHandler):
            return Path(h.baseFilename)
    log_dir = Path(log_dir or os.getenv("LOG_DIR") or
                   (Path(__file__).resolve().parents[3] / "logs"))
    log_dir.mkdir(parents=True, exist_ok=True)
    lvl = (level or os.getenv("LOG_LEVEL") or "INFO").upper()
    root.setLevel(getattr(logging, lvl, logging.INFO))

    # 文件: JSON Lines, 按天轮转
    fpath = log_dir / "app.log"
    fh = TimedRotatingFileHandler(fpath, when="midnight", backupCount=14, encoding="utf-8")
    fh.setFormatter(JsonFormatter())
    root.addHandler(fh)

    # 控制台(人类可读); uvicorn 已配控制台时不再重复添加
    has_console = any(isinstance(h, logging.StreamHandler) for h in root.handlers)
    if not has_console:
        console = logging.StreamHandler()
        console.setFormatter(logging.Formatter("%(asctime)s %(levelname)s [%(name)s] %(message)s"))
        root.addHandler(console)

    logging.getLogger(__name__).info("日志初始化: %s (level=%s)", fpath, lvl)
    return fpath
