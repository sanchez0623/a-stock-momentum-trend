"""复盘记忆 RAG(方案: 历史复盘 -> embedding 入库 -> 本次复盘检索注入).

数据源: AiReview(问题/建议) + ConfigChange(采纳/撤销) + 后续复盘 stats(效果归因)。
检索: 全量余弦相似度(numpy), 复盘记录量级小(几十条), 无需引入向量库。
embedding: SiliconFlow /v1/embeddings(OpenAI 兼容), 未配置时静默降级(无记忆注入)。
"""

from __future__ import annotations

import json
import logging
from typing import Any

import httpx
import numpy as np
from sqlmodel import Session, select

from app import db
from app.core.config import config_manager
from app.models.models import AiReview, ConfigChange, ReviewMemory

logger = logging.getLogger(__name__)

DEFAULT_EMBEDDING_BASE = "https://api.siliconflow.cn/v1"
DEFAULT_EMBEDDING_MODEL = "BAAI/bge-m3"
BATCH = 32  # 单次 embedding 请求的文本条数上限


class EmbeddingError(Exception):
    pass


# ---------------------------------------------------------------- 客户端
class EmbeddingClient:
    """SiliconFlow embeddings(OpenAI 兼容 POST {base}/embeddings)."""

    def __init__(self, base_url: str, api_key: str, model: str = DEFAULT_EMBEDDING_MODEL,
                 timeout_sec: float = 30.0) -> None:
        self.base_url = (base_url or DEFAULT_EMBEDDING_BASE).rstrip("/")
        self.api_key = api_key
        self.model = model or DEFAULT_EMBEDDING_MODEL
        self.timeout_sec = timeout_sec

    @property
    def configured(self) -> bool:
        return bool(self.api_key)

    async def embed(self, texts: list[str]) -> list[list[float]]:
        """批量嵌入, 返回与输入同序的向量列表."""
        if not self.configured:
            raise EmbeddingError("未配置 Embedding API Key")
        if not texts:
            return []
        out: list[list[float]] = []
        for i in range(0, len(texts), BATCH):
            # 空串用占位符替换(API 拒绝空 input), 保证返回条数与输入严格对齐
            chunk = [t if t.strip() else "无内容" for t in texts[i:i + BATCH]]
            try:
                async with httpx.AsyncClient(timeout=self.timeout_sec) as client:
                    resp = await client.post(
                        f"{self.base_url}/embeddings",
                        json={"model": self.model, "input": chunk, "encoding_format": "float"},
                        headers={"Authorization": f"Bearer {self.api_key}"},
                    )
                    resp.raise_for_status()
                    data = resp.json()
                items = sorted(data["data"], key=lambda d: d.get("index", 0))
                out.extend([list(it["embedding"]) for it in items])
            except httpx.TimeoutException as exc:
                raise EmbeddingError(f"Embedding 请求超时({self.timeout_sec}s)") from exc
            except httpx.HTTPStatusError as exc:
                raise EmbeddingError(
                    f"Embedding 返回 {exc.response.status_code}: {exc.response.text[:120]}") from exc
            except (KeyError, TypeError, ValueError) as exc:
                raise EmbeddingError(f"Embedding 响应格式异常: {str(data)[:120]}") from exc
            except Exception as exc:  # noqa: BLE001
                raise EmbeddingError(f"Embedding 请求失败: {exc}") from exc
        return out

    async def embed_one(self, text: str) -> list[float]:
        vecs = await self.embed([text])
        return vecs[0] if vecs else []


def build_embedding_client(emb_cfg: dict[str, Any]) -> EmbeddingClient:
    """从配置段构建客户端(与 build_client_from_config 同风格)."""
    return EmbeddingClient(
        base_url=emb_cfg.get("base_url", ""),
        api_key=emb_cfg.get("api_key", ""),
        model=emb_cfg.get("model", ""),
        timeout_sec=float(emb_cfg.get("timeout_sec", 30)),
    )


# ---------------------------------------------------------------- 记忆文本构造
def _issue_brief(issues: list[dict]) -> str:
    """问题摘要: 按标题聚合计数, 如 [high]追高买入×2."""
    if not issues:
        return "无"
    counts: dict[str, dict] = {}
    for it in issues:
        key = f"{it.get('level', '')}:{it.get('title', '')}"
        if key not in counts:
            counts[key] = {"level": it.get("level", ""), "title": it.get("title", ""), "n": 0}
        counts[key]["n"] += 1
    return "; ".join(f"[{v['level']}]{v['title']}×{v['n']}" for v in counts.values())


def _stats_text(stats: dict) -> str:
    """统计摘要: 胜率/盈亏/平仓数."""
    return (f"胜率{float(stats.get('win_rate', 0) or 0)}%, "
            f"盈亏{float(stats.get('total_pnl', 0) or 0):.0f}元, "
            f"平仓{int(stats.get('closed', 0) or 0)}笔")


def _suggestions_brief(suggestions: list[dict], change_status: dict[int, str]) -> str:
    """建议摘要: 文本 + patch 数值 + 采纳状态(采纳/已撤销/已拒绝/未处理)."""
    if not suggestions:
        return "无"
    lines: list[str] = []
    for idx, it in enumerate(suggestions):
        text = str(it.get("text", "")).strip()
        patch = it.get("patch")
        head = text or "(无文字)"
        if isinstance(patch, dict) and patch.get("group") and patch.get("key"):
            frm = patch.get("from")
            to = patch.get("to")
            head += f"[{patch['group']}.{patch['key']} "
            if frm is not None:
                head += f"{frm:g}→{to:g}"
            else:
                head += f"→{to:g}"
            head += "]"
        if it.get("status") == "accepted":
            status = "已撤销" if change_status.get(idx) == "reverted" else "已采纳生效"
        elif it.get("status") == "rejected":
            status = "已拒绝"
        else:
            status = "未处理"
        lines.append(f"{head}({status})")
    return "; ".join(lines)


def build_memory_text(review: AiReview, change_status: dict[int, str],
                      next_stats: dict | None = None) -> str:
    """把一次历史复盘转成记忆条目文本(问题 + 建议/采纳结果 + 采纳后表现).

    next_stats: 该复盘之后最近一次复盘的 stats, 用于效果归因("采纳后表现").
    """
    try:
        result = json.loads(review.rule_result_json or "{}")
    except json.JSONDecodeError:
        result = {}
    try:
        suggestions = json.loads(review.suggestions_json or "[]")
    except json.JSONDecodeError:
        suggestions = []

    parts = [f"复盘({review.time[:10]}): 问题={_issue_brief(result.get('issues') or [])}"]
    parts.append(f"建议: {_suggestions_brief(suggestions, change_status)}")
    prev = result.get("stats") or {}
    if prev and next_stats:
        try:
            prev_wr = float(prev.get("win_rate", 0) or 0)
            next_wr = float(next_stats.get("win_rate", 0) or 0)
            prev_pnl = float(prev.get("total_pnl", 0) or 0)
            next_pnl = float(next_stats.get("total_pnl", 0) or 0)
            parts.append(
                f"采纳后表现(后续复盘): 胜率{prev_wr:.1f}%→{next_wr:.1f}%, "
                f"盈亏{prev_pnl:.0f}→{next_pnl:.0f}元"
            )
        except (TypeError, ValueError):
            pass
    return " | ".join(parts)


def _next_review_stats(reviews: list[AiReview], idx: int) -> dict | None:
    """reviews 按 time 升序; 取 idx 之后最近一条有 stats 的复盘."""
    for r in reviews[idx + 1:]:
        try:
            result = json.loads(r.rule_result_json or "{}")
        except json.JSONDecodeError:
            continue
        stats = result.get("stats")
        if isinstance(stats, dict) and stats:
            return stats
    return None


# ---------------------------------------------------------------- 索引
def _embedding_cfg() -> dict[str, Any]:
    return (config_manager.get().get("llm", {}) or {}).get("embedding", {}) or {}


async def index_review(review: AiReview, session: Session | None = None) -> bool:
    """为一次复盘生成记忆条目并入库(幂等: 同 review 已存在则跳过).

    embedding 未启用/调用失败均只记日志返回 False, 不阻塞复盘主流程。
    """
    emb = _embedding_cfg()
    if not (emb.get("enabled") and emb.get("api_key")):
        return False

    def _do(s: Session) -> bool:
        existing = s.exec(select(ReviewMemory).where(ReviewMemory.review_id == review.id)).first()
        if existing is not None:
            return True
        changes = s.exec(select(ConfigChange).where(ConfigChange.review_id == review.id)).all()
        change_status = {c.suggestion_index: c.status for c in changes if c.suggestion_index is not None}
        reviews = list(s.exec(select(AiReview).order_by(AiReview.time)).all())
        try:
            idx = next(i for i, r in enumerate(reviews) if r.id == review.id)
        except StopIteration:
            idx = len(reviews) - 1
        text = build_memory_text(review, change_status, _next_review_stats(reviews, idx))
        return text

    try:
        if session is not None:
            text = _do(session)
        else:
            with db.session_scope() as s:
                text = _do(s)
    except Exception as exc:  # noqa: BLE001
        logger.warning("复盘记忆文本构造失败(review_id=%s): %s", review.id, exc)
        return False
    if not text:
        return False

    try:
        client = build_embedding_client(emb)
        vec = await client.embed_one(text)
        if not vec:
            return False
        if session is not None:
            session.add(ReviewMemory(review_id=review.id, time=review.time, text=text,
                                     embedding_json=json.dumps(vec), model=emb.get("model", "")))
            session.commit()
        else:
            with db.session_scope() as s:
                s.add(ReviewMemory(review_id=review.id, time=review.time, text=text,
                                   embedding_json=json.dumps(vec), model=emb.get("model", "")))
                s.commit()
        return True
    except Exception as exc:  # noqa: BLE001 - 记忆失败不影响复盘
        logger.warning("复盘记忆索引失败(review_id=%s): %s", review.id, exc)
        return False


async def rebuild_index(session: Session | None = None) -> int:
    """为所有缺失记忆的历史复盘补建索引, 返回新增条数."""
    emb = _embedding_cfg()
    if not (emb.get("enabled") and emb.get("api_key")):
        return 0

    def _collect(s: Session) -> tuple[list[AiReview], set[int], dict[int, dict[int, str]]]:
        reviews = list(s.exec(select(AiReview).order_by(AiReview.time)).all())
        mem_ids = {m.review_id for m in s.exec(select(ReviewMemory.review_id)).all()}
        changes_map: dict[int, dict[int, str]] = {}
        for c in s.exec(select(ConfigChange)).all():
            if c.review_id is not None and c.suggestion_index is not None:
                changes_map.setdefault(c.review_id, {})[c.suggestion_index] = c.status
        return reviews, mem_ids, changes_map

    try:
        if session is not None:
            reviews, mem_ids, changes_map = _collect(session)
        else:
            with db.session_scope() as s:
                reviews, mem_ids, changes_map = _collect(s)
    except Exception as exc:  # noqa: BLE001
        logger.warning("复盘记忆重建索引: 读取历史失败 %s", exc)
        return 0

    pending = [r for r in reviews if r.id not in mem_ids]
    if not pending:
        return 0
    idx_of = {r.id: i for i, r in enumerate(reviews)}
    texts = [build_memory_text(r, changes_map.get(r.id, {}),
                               _next_review_stats(reviews, idx_of.get(r.id, len(reviews) - 1)))
             for r in pending]
    try:
        client = build_embedding_client(emb)
        vecs = await client.embed(texts)
    except Exception as exc:  # noqa: BLE001
        logger.warning("复盘记忆重建索引: embedding 失败 %s", exc)
        return 0

    def _save(s: Session) -> int:
        for r, vec in zip(pending, vecs, strict=True):
            s.add(ReviewMemory(review_id=r.id, time=r.time,
                               text=texts[pending.index(r)],
                               embedding_json=json.dumps(vec), model=emb.get("model", "")))
        s.commit()
        return len(pending)

    try:
        if session is not None:
            return _save(session)
        with db.session_scope() as s:
            return _save(s)
    except Exception as exc:  # noqa: BLE001
        logger.warning("复盘记忆重建索引: 落库失败 %s", exc)
        return 0


# ---------------------------------------------------------------- 检索
def _cosine_scores(query_vec: list[float], rows: list[ReviewMemory]) -> list[tuple[ReviewMemory, float]]:
    q = np.asarray(query_vec, dtype=float)
    scored: list[tuple[ReviewMemory, float]] = []
    for row in rows:
        try:
            v = np.asarray(json.loads(row.embedding_json or "[]"), dtype=float)
        except (json.JSONDecodeError, ValueError):
            continue
        if v.size == 0 or q.size != v.size:
            continue
        qn, vn = np.linalg.norm(q), np.linalg.norm(v)
        if qn == 0 or vn == 0:
            continue
        scored.append((row, float(np.dot(q, v) / (qn * vn))))
    return scored


async def search_memories(query: str, k: int = 3, session: Session | None = None) -> list[dict]:
    """检索与 query 最相似的 k 条复盘记忆. 未启用/失败返回 [].

    返回 [{time, text, score}], 按相似度降序。
    """
    emb = _embedding_cfg()
    if not (emb.get("enabled") and emb.get("api_key")) or not query.strip():
        return []
    try:
        if session is not None:
            rows = list(session.exec(select(ReviewMemory).order_by(ReviewMemory.time.desc())).all())
        else:
            with db.session_scope() as s:
                rows = list(s.exec(select(ReviewMemory).order_by(ReviewMemory.time.desc())).all())
    except Exception as exc:  # noqa: BLE001
        logger.warning("复盘记忆检索: 读取失败 %s", exc)
        return []
    if not rows:
        return []
    try:
        client = build_embedding_client(emb)
        qv = await client.embed_one(query)
        scored = _cosine_scores(qv, rows)
    except Exception as exc:  # noqa: BLE001
        logger.warning("复盘记忆检索失败: %s", exc)
        return []
    scored.sort(key=lambda x: x[1], reverse=True)
    return [{"time": r.time, "text": r.text, "score": round(score, 4)}
            for r, score in scored[:k]]


async def memory_context(query: str, k: int = 3, session: Session | None = None) -> str:
    """供两步链注入的记忆文本片段; 无记忆返回空串."""
    hits = await search_memories(query, k=k, session=session)
    if not hits:
        return ""
    lines = [f"- [{h['time']}] {h['text']} (相似度{h['score']:.2f})" for h in hits]
    return "\n".join(lines)
