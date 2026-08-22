"""测试: 结构化日志框架(JSON Lines + 幂等初始化)."""

from __future__ import annotations

import io
import json
import logging

from app.core.logger.setup import JsonFormatter, setup_logging


def test_json_formatter_with_extra_and_exc():
    """JSON 行包含 ts/level/logger/message + extra 字段 + 异常堆栈."""
    logger = logging.getLogger("test.json")
    handler = logging.StreamHandler()
    handler.setFormatter(JsonFormatter())
    logger.addHandler(handler)
    logger.setLevel(logging.DEBUG)
    logger.propagate = False
    buf = io.StringIO()
    handler.setStream(buf)
    try:
        try:
            raise ValueError("boom")
        except ValueError:
            logger.warning("事件发生", exc_info=True, extra={"component": "test", "count": 3})
    finally:
        logger.removeHandler(handler)
    line = json.loads(buf.getvalue().strip())
    assert line["level"] == "WARNING"
    assert line["logger"] == "test.json"
    assert line["message"] == "事件发生"
    assert line["component"] == "test"
    assert line["count"] == 3
    assert "boom" in line["exc"]  # 异常堆栈完整保留


def test_setup_logging_creates_file_and_idempotent(tmp_path):
    """setup_logging: 生成 JSON Lines 文件, 重复调用不重复加 handler."""
    import logging.handlers as lh

    root = logging.getLogger()
    before = [h for h in root.handlers if isinstance(h, lh.TimedRotatingFileHandler)]
    try:
        p1 = setup_logging(tmp_path / "logs")
        p2 = setup_logging(tmp_path / "logs")
        assert p1 == p2  # 幂等
        after = [h for h in root.handlers if isinstance(h, lh.TimedRotatingFileHandler)]
        assert len(after) == len(before) + 1
        assert p1.exists()
        # 写一条日志, 文件里是合法 JSON 行
        logging.getLogger("test.setup").info("hello", extra={"k": "v"})
        for handler in after:
            handler.flush()
        lines = p1.read_text(encoding="utf-8").splitlines()
        assert any(json.loads(line)["message"] == "hello" for line in lines)
    finally:
        # 清理本次添加的 handler, 避免污染其它测试
        for h in list(root.handlers):
            if isinstance(h, lh.TimedRotatingFileHandler) and h.baseFilename.startswith(str(tmp_path)):
                root.removeHandler(h)
                h.close()
