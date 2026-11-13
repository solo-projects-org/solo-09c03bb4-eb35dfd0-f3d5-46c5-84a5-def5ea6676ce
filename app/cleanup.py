"""留存期到期撤下书目的元数据清理编排。

设计要点：
- 清理与采集互不抢锁：清理不获取 harvester 的进程内采集锁，因此补采在
  OAI 源翻页等待（网络 IO，未开事务）时，馆员仍可完成清理。
- 整批提交：在单个 ``BEGIN IMMEDIATE`` 事务内完成 SELECT + UPDATE，任何
  异常整体回滚，不会出现清理一半的状态。
- 不推进、也不读取 harvest_progress：续采进度完全不受影响。
"""
from __future__ import annotations

import logging
from dataclasses import dataclass

from . import db

logger = logging.getLogger("cleanup")


@dataclass
class CleanupSummary:
    cutoff_date: str
    cleared: list[str]
    count: int


def cleanup_withdrawn(cutoff_date: str) -> CleanupSummary:
    """清理 datestamp 不晚于截止日、仍为撤下状态的书目元数据。"""
    conn = db.get_connection()
    try:
        with db.transaction(conn):
            identifiers = db.cleanup_withdrawn_metadata(
                conn, cutoff_date=cutoff_date
            )
            conn.commit()
    except Exception:
        conn.rollback()
        logger.exception("清理失败，本批变更已整体回滚：截止日=%s", cutoff_date)
        raise
    finally:
        conn.close()

    logger.info("元数据清理完成 截止日=%s 实际清理=%d 条：%s",
                cutoff_date, len(identifiers), identifiers)
    return CleanupSummary(
        cutoff_date=cutoff_date,
        cleared=identifiers,
        count=len(identifiers),
    )
