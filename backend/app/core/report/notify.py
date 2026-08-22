"""盘后日报 - 推送通道: 站内 Notification 表 + 可选企业微信 webhook.

站内通知必写; webhook 未配置或推送失败均只记日志, 不影响主流程。
"""

from __future__ import annotations

import logging

import httpx
from sqlmodel import Session, select

from app import db
from app.models.models import Notification

logger = logging.getLogger(__name__)


async def push_notification(category: str, title: str, content: str,
                            webhook: str = "", session: Session | None = None,
                            fingerprint: str = "") -> Notification:
    """写站内通知; webhook 非空时同步推送到企业微信机器人. 返回通知记录.

    fingerprint: 去重指纹(如 2026-08-12:300139:SELL_REDUCE), 供 AI 助理等避免重复提醒.
    """
    def _save(s: Session) -> Notification:
        row = Notification(category=category, title=title, content=content,
                           fingerprint=fingerprint)
        s.add(row)
        s.commit()
        s.refresh(row)
        return row

    if session is not None:
        row = _save(session)
    else:
        with db.session_scope() as s:
            row = _save(s)

    if webhook:
        try:
            async with httpx.AsyncClient(timeout=10) as client:
                resp = await client.post(webhook, json={"msgtype": "text", "text": {"content": content}})
                resp.raise_for_status()
        except Exception as exc:  # noqa: BLE001 - webhook 失败不影响站内
            logger.warning("日报 webhook 推送失败: %s", exc)
    return row


def list_notifications(limit: int = 50, unread_only: bool = False,
                       session: Session | None = None) -> list[Notification]:
    stmt = select(Notification).order_by(Notification.time.desc()).limit(limit)
    if unread_only:
        stmt = stmt.where(Notification.read == False)  # noqa: E712
    if session is not None:
        return list(session.exec(stmt).all())
    with db.session_scope() as s:
        return list(s.exec(stmt).all())


def mark_read(notification_id: int, session: Session | None = None) -> Notification | None:
    def _do(s: Session) -> Notification | None:
        row = s.get(Notification, notification_id)
        if row is None:
            return None
        row.read = True
        s.commit()
        s.refresh(row)
        return row

    if session is not None:
        return _do(session)
    with db.session_scope() as s:
        return _do(s)
