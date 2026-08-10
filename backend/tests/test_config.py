"""配置中心单测."""

from __future__ import annotations

from app.core.config import DEFAULT_CONFIG, ConfigManager, _apply_env_overrides, _deep_merge


def test_defaults():
    cm = ConfigManager(data_dir="__tmp_config_test__")
    cfg = cm.get()
    assert cfg["风控"]["stop_loss_pct"] == 5.0
    assert cfg["仓位"]["strategy"] == "pyramid"
    assert cfg["数据源"]["priority"][0] == "mootdx"


def test_deep_merge_nested():
    base = {"a": {"b": 1, "c": 2}, "x": [1]}
    overlay = {"a": {"b": 9}}
    merged = _deep_merge(base, overlay)
    assert merged["a"]["b"] == 9
    assert merged["a"]["c"] == 2  # 未覆盖的保留
    assert merged["x"] == [1]
    # 不修改原 dict
    assert base["a"]["b"] == 1


def test_update_persist_skipped_without_db():
    cm = ConfigManager(data_dir="__tmp_config_test__")
    cm.update({"风控": {"stop_loss_pct": 6.5}}, persist=True)
    assert cm.get()["风控"]["stop_loss_pct"] == 6.5
    assert cm.get()["趋势"]["ma_short"] == 10  # 其他分组不受影响


def test_get_path():
    cm = ConfigManager(data_dir="__tmp_config_test__")
    assert cm.get_path("风控.stop_loss_pct") == 5.0
    assert cm.get_path("不存在.的.路径", "fallback") == "fallback"


def test_env_overrides(monkeypatch):
    cm = ConfigManager(data_dir="__tmp_config_test__")
    monkeypatch.setenv("LLM_MODEL", "deepseek-chat")
    monkeypatch.setenv("LLM_API_KEY", "sk-test")
    monkeypatch.setenv("EASTMONEY_INTERVAL_SEC", "1.7")
    monkeypatch.setenv("ENABLE_MOOTDX", "0")
    monkeypatch.setenv("PROXY_POOL", "http://p1:1,http://p2:2")
    cfg = cm.get()
    _apply_env_overrides(cfg)
    assert cfg["llm"]["model"] == "deepseek-chat"
    assert cfg["llm"]["api_key"] == "sk-test"
    assert cfg["数据源"]["eastmoney"]["interval_sec"] == 1.7
    assert cfg["数据源"]["enabled"]["mootdx"] is False
    assert cfg["数据源"]["proxy_pool"] == ["http://p1:1", "http://p2:2"]


def test_env_overrides_embedding(monkeypatch):
    """embedding 配置段 env 覆盖: 开关/地址/key/模型 (复盘记忆 RAG 依赖此开关)."""
    cm = ConfigManager(data_dir="__tmp_config_test__")
    monkeypatch.setenv("EMBEDDING_ENABLED", "1")
    monkeypatch.setenv("EMBEDDING_BASE_URL", "https://api.siliconflow.cn/v1")
    monkeypatch.setenv("EMBEDDING_API_KEY", "sk-embed")
    monkeypatch.setenv("EMBEDDING_MODEL", "BAAI/bge-m3")
    cfg = cm.get()
    _apply_env_overrides(cfg)
    emb = cfg["llm"]["embedding"]
    assert emb["enabled"] is True
    assert emb["base_url"] == "https://api.siliconflow.cn/v1"
    assert emb["api_key"] == "sk-embed"
    assert emb["model"] == "BAAI/bge-m3"


def test_embedding_disabled_by_default():
    """无 env 覆盖时 embedding 默认关闭(防误扣费)."""
    cm = ConfigManager(data_dir="__tmp_config_test__")
    assert cm.get()["llm"]["embedding"]["enabled"] is False


def test_env_empty_string_not_override(monkeypatch):
    """env 空字符串视为未设置, 不覆盖已有配置(.env 留空变量不抹掉 DB 的 Key)."""
    cm = ConfigManager(data_dir="__tmp_config_test__")
    cfg = cm.get()
    cfg["llm"]["api_key"] = "sk-db-key"  # 模拟 DB 已保存的页面配置
    monkeypatch.setenv("LLM_API_KEY", "")  # .env 里留空的典型场景
    monkeypatch.setenv("ENABLE_MOOTDX", "")
    _apply_env_overrides(cfg)
    assert cfg["llm"]["api_key"] == "sk-db-key"  # 空值不覆盖
    assert cfg["数据源"]["enabled"]["mootdx"] is True  # 空开关不关闭


def test_env_value_still_overrides(monkeypatch):
    """非空 env 仍然覆盖(env > DB 的优先级不变)."""
    cm = ConfigManager(data_dir="__tmp_config_test__")
    cfg = cm.get()
    cfg["llm"]["api_key"] = "sk-db-key"
    monkeypatch.setenv("LLM_API_KEY", "sk-env-key")
    _apply_env_overrides(cfg)
    assert cfg["llm"]["api_key"] == "sk-env-key"


def test_masked_hides_api_key(monkeypatch):
    cm = ConfigManager(data_dir="__tmp_config_test__")
    monkeypatch.setenv("LLM_API_KEY", "sk-secret")
    cm.load_from_db()  # env 覆盖写入内存配置
    masked = cm.masked()
    assert masked["llm"]["api_key"] == "******"


def test_listener_notified():
    cm = ConfigManager(data_dir="__tmp_config_test__")
    seen = []

    def listener(cfg):
        seen.append(cfg["风控"]["stop_loss_pct"])

    cm.register_listener(listener)
    cm.update({"风控": {"stop_loss_pct": 7.0}})
    assert seen == [7.0]


def test_default_config_structure():
    for group in ("趋势", "动量", "量能", "风控", "仓位", "做T", "评分权重", "数据源", "llm"):
        assert group in DEFAULT_CONFIG


def test_now_is_beijing_time():
    """所有落库时间戳强制东八区(不依赖进程时区): 与 UTC 相差约 8 小时."""
    import datetime as dt

    from app.models.models import _now

    parsed = dt.datetime.strptime(_now(), "%Y-%m-%d %H:%M:%S")
    utc = dt.datetime.now(dt.UTC).replace(tzinfo=None)
    diff_h = (parsed - utc).total_seconds() / 3600
    assert 7.5 <= diff_h <= 8.5, f"_now() 应为东八区, 实际与 UTC 差 {diff_h:.2f}h"
