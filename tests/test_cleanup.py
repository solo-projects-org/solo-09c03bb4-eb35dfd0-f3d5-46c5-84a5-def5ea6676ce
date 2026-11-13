"""元数据清理与清理标识补采守卫规则的端到端测试。"""
from __future__ import annotations

import threading

from app.oai_client import OAIPage, OAIRecord

REC13 = "oai:sample.lib:rec-013"
REC01 = "oai:sample.lib:rec-001"


def _harvest(client, payload):
    api, _db, _oai = client
    resp = api.post("/api/v1/harvest", json=payload)
    assert resp.status_code == 200, resp.text
    return resp.json()


def _get(api, identifier):
    resp = api.get(f"/api/v1/catalog/{identifier}")
    assert resp.status_code == 200, resp.text
    return resp.json()


def _cleanup(client, until):
    api, _db, _oai = client
    resp = api.post("/api/v1/cleanup", json={"until": until})
    assert resp.status_code == 200, resp.text
    return resp.json()


def test_cleanup_clears_only_eligible_withdrawn_and_keeps_tombstone_fields(client):
    api, db_mod, _oai = client
    _harvest(client, {"from": "2026-10-01", "until": "2026-10-15"})

    before = _get(api, REC13)
    assert before["status"] == "withdrawn"
    assert before["write_guarded"] is False

    result = _cleanup(client, "2026-10-13")

    # 返回本次实际清掉元数据的标识
    assert result["count"] == 1
    assert result["cleared_identifiers"] == [REC13]
    assert result["cutoff_date"] == "2026-10-13"

    row = _get(api, REC13)
    # 保留 OAI 标识、datestamp、撤下状态
    assert row["identifier"] == REC13
    assert row["datestamp"] == before["datestamp"] == "2026-10-13"
    assert row["status"] == "withdrawn"
    # DC 列已清空、清理标记与时间已落库
    assert row["title"] is None
    assert row["creator"] is None
    assert row["date"] is None
    assert row["identifier_dc"] is None
    assert row["write_guarded"] is True
    assert row["cleared_at"]

    conn = db_mod.get_connection()
    try:
        raw = conn.execute(
            "SELECT raw_dc FROM bib_records WHERE identifier = ?", (REC13,)
        ).fetchone()["raw_dc"]
        assert raw == "{}"
        # 续采进度不受影响
        assert db_mod.get_last_until(conn) == "2026-10-15"
    finally:
        conn.close()


def test_cleanup_ignores_active_records_and_newer_datestamps(client):
    api, _db, _oai = client
    _harvest(client, {"from": "2026-10-01", "until": "2026-10-15"})

    # 截止日早于撤下记录的 datestamp：无符合条件记录
    result = _cleanup(client, "2026-10-12")
    assert result["count"] == 0
    assert result["cleared_identifiers"] == []
    assert _get(api, REC13)["write_guarded"] is False

    # 在架记录即使 datestamp 早于截止日也不会被清理
    result = _cleanup(client, "2026-10-20")
    assert result["cleared_identifiers"] == [REC13]
    active = _get(api, REC01)
    assert active["status"] == "active"
    assert active["title"] is not None
    assert active["write_guarded"] is False


def test_cleanup_is_idempotent_and_progress_untouched(client):
    api, db_mod, _oai = client
    _harvest(client, {"from": "2026-10-01", "until": "2026-10-15"})
    first = _cleanup(client, "2026-10-31")
    second = _cleanup(client, "2026-10-31")
    assert first["count"] == 1
    # 已清理过的记录不会再次返回，避免重复清理
    assert second["count"] == 0
    assert second["cleared_identifiers"] == []
    conn = db_mod.get_connection()
    try:
        assert db_mod.get_last_until(conn) == "2026-10-15"
    finally:
        conn.close()


def test_cleanup_invalid_date_rejected(client):
    api, _db, _oai = client
    resp = api.post("/api/v1/cleanup", json={"until": "2026-13-40"})
    assert resp.status_code == 422


# ---------------------------------------------------------------------------
# 清理后补采的守卫规则（借助样例源的版本切换 admin 端点）
# ---------------------------------------------------------------------------

def _cleanup_rec13(client):
    _harvest(client, {"from": "2026-10-01", "until": "2026-10-15"})
    _cleanup(client, "2026-10-13")
    assert _get(client[0], REC13)["write_guarded"] is True


def test_reharvest_same_day_active_conflict_keeps_withdrawn(client):
    api, _db, oai_mod = client
    _cleanup_rec13(client)
    oai_mod.set_override(REC13, "active-same")
    try:
        result = _harvest(client, {"from": "2026-10-01", "until": "2026-10-31"})
        # 同日冲突记录被忽略，计入 skipped
        assert result["skipped"] == 1
        row = _get(api, REC13)
        assert row["status"] == "withdrawn"
        assert row["title"] is None
        assert row["write_guarded"] is True
    finally:
        oai_mod.set_override(REC13, None)


def test_reharvest_older_active_version_is_ignored(client):
    api, _db, oai_mod = client
    _cleanup_rec13(client)
    oai_mod.set_override(REC13, "active-old")
    try:
        result = _harvest(client, {"from": "2026-10-01", "until": "2026-10-31"})
        assert result["skipped"] == 1
        row = _get(api, REC13)
        assert row["status"] == "withdrawn"
        assert row["title"] is None
        # 库内 datestamp 不被更早记录覆盖
        assert row["datestamp"] == "2026-10-13"
        assert row["write_guarded"] is True
    finally:
        oai_mod.set_override(REC13, None)


def test_reharvest_newer_active_version_reactivates_and_guard_persists(client):
    api, _db, oai_mod = client
    _cleanup_rec13(client)
    oai_mod.set_override(REC13, "active-new")
    try:
        result = _harvest(client, {"from": "2026-10-01", "until": "2026-11-30"})
        assert result["skipped"] == 0
        assert result["updated"] >= 1
        row = _get(api, REC13)
        # 较新在架版本重新上架，写回完整 DC
        assert row["status"] == "active"
        assert row["title"] is not None and "新版" in row["title"]
        assert row["datestamp"] == "2026-11-12"
        # 守卫标记保留：重新上架后规则仍然生效
        assert row["write_guarded"] is True

        # 再来一条更早的旧版：即便记录已在架，仍按库内 datestamp 忽略
        oai_mod.set_override(REC13, "active-old")
        again = _harvest(client, {"from": "2026-10-01", "until": "2026-11-30"})
        assert again["skipped"] == 1
        row = _get(api, REC13)
        assert row["status"] == "active"
        assert row["datestamp"] == "2026-11-12"
        assert "新版" in (row["title"] or "")
    finally:
        oai_mod.set_override(REC13, None)


def test_reharvest_newer_deleted_keeps_withdrawn_and_updates_datestamp(client):
    api, _db, oai_mod = client
    _cleanup_rec13(client)
    oai_mod.set_override(REC13, "deleted-new")
    try:
        result = _harvest(client, {"from": "2026-10-01", "until": "2026-11-30"})
        assert result["withdrawn"] >= 1
        row = _get(api, REC13)
        assert row["status"] == "withdrawn"
        assert row["datestamp"] == "2026-11-12"
        # 删除标记不恢复元数据
        assert row["title"] is None
        assert row["write_guarded"] is True
    finally:
        oai_mod.set_override(REC13, None)


def test_same_day_deleted_record_is_skipped_for_guarded_id(client, monkeypatch):
    """同日到来的删除标记同样跳过（不刷新 updated_at/datestamp）。"""
    api, db_mod, _oai = client
    _cleanup_rec13(client)
    before = _get(api, REC13)

    fake = _FakeClient([
        OAIRecord(identifier=REC13, datestamp="2026-10-13", deleted=True, dc={})
    ])
    from app import main as main_mod
    monkeypatch.setattr(main_mod, "OAIClient", lambda *a, **k: fake)

    result = _harvest(client, {"from": "2026-10-13", "until": "2026-10-13"})
    assert result["skipped"] == 1
    after = _get(api, REC13)
    assert after["status"] == "withdrawn"
    assert after["datestamp"] == before["datestamp"]
    assert after["updated_at"] == before["updated_at"]


def test_unguarded_records_are_not_skipped(client):
    api, _db, _oai = client
    # 全量采集，不做清理：普通记录不受守卫规则影响，无 skipped
    result = _harvest(client, {"from": "2026-10-01", "until": "2026-10-27"})
    assert result["skipped"] == 0
    run = api.get("/api/v1/runs").json()[0]
    assert run["skipped"] == 0


def test_guard_marker_survives_restart(client):
    """write_guarded 持久化在 SQLite，重新 init_db（模拟重启）后规则仍在。"""
    api, db_mod, oai_mod = client
    _cleanup_rec13(client)
    # 模拟服务重启：重新执行建表/迁移不应清掉守卫标记
    db_mod.init_db()
    oai_mod.set_override(REC13, "active-same")
    try:
        result = _harvest(client, {"from": "2026-10-01", "until": "2026-10-31"})
        assert result["skipped"] == 1
        assert _get(api, REC13)["status"] == "withdrawn"
    finally:
        oai_mod.set_override(REC13, None)


# ---------------------------------------------------------------------------
# 整批原子性与并发
# ---------------------------------------------------------------------------

def test_cleanup_batch_rolls_back_on_failure(client, monkeypatch):
    api, db_mod, _oai = client
    _harvest(client, {"from": "2026-10-01", "until": "2026-10-15"})
    real_fn = db_mod.cleanup_withdrawn_metadata
    state = {"armed": True}

    def boom(conn, *, cutoff_date):
        if not state["armed"]:
            return real_fn(conn, cutoff_date=cutoff_date)
        state["armed"] = False
        # 在同一事务内先做一次真实写、再抛错，验证异常会整体回滚不留半批
        conn.execute(
            "UPDATE bib_records SET write_guarded = 1 WHERE identifier = ?",
            (REC13,),
        )
        raise RuntimeError("injected failure")

    monkeypatch.setattr(db_mod, "cleanup_withdrawn_metadata", boom)
    resp = api.post("/api/v1/cleanup", json={"until": "2026-10-31"})
    assert resp.status_code == 500
    # 只还原被注入的函数，避免 undo 撤销夹具设置的 DB_PATH
    monkeypatch.setattr(db_mod, "cleanup_withdrawn_metadata", real_fn)

    row = _get(api, REC13)
    # 回滚后状态保持清理前：撤下、无守卫标记（注入的写入被一并回滚）
    assert row["status"] == "withdrawn"
    assert row["write_guarded"] is False
    assert row["cleared_at"] is None
    conn = db_mod.get_connection()
    try:
        guarded = conn.execute(
            "SELECT write_guarded, cleared_at FROM bib_records "
            "WHERE identifier = ?", (REC13,)
        ).fetchone()
        assert guarded["write_guarded"] == 0
        assert guarded["cleared_at"] is None
    finally:
        conn.close()


def test_cleanup_runs_while_harvest_waits_on_pagination(client, monkeypatch):
    """补采卡在源馆翻页（未开写事务）时，清理仍能整批完成。"""
    api, db_mod, _oai = client
    _harvest(client, {"from": "2026-10-01", "until": "2026-10-15"})

    entered = threading.Event()
    release = threading.Event()

    class BlockingClient:
        def __init__(self, *args, **kwargs):
            pass

        def __enter__(self):
            return self

        def __exit__(self, *exc):
            return False

        def list_records_first(self, *, from_date, until_date):
            return OAIPage(
                records=[OAIRecord(
                    identifier=REC01, datestamp="2026-10-16",
                    deleted=False, dc={"title": ["翻页等待中的记录"]},
                )],
                resumption_token="blocking-token",
                complete_list_size=2, cursor=0,
            )

        def list_records_resume(self, token):
            entered.set()
            release.wait(timeout=10)
            return OAIPage(
                records=[OAIRecord(
                    identifier="oai:sample.lib:rec-002",
                    datestamp="2026-10-16", deleted=False, dc={},
                )],
                resumption_token=None, complete_list_size=2, cursor=1,
            )

        def close(self):
            pass

    from app import main as main_mod
    monkeypatch.setattr(main_mod, "OAIClient", BlockingClient)

    harvest_done = threading.Event()
    harvest_result: dict = {}

    def run_harvest():
        resp = api.post(
            "/api/v1/harvest", json={"until": "2026-10-16"}
        )
        harvest_result["status_code"] = resp.status_code
        harvest_result["body"] = resp.json()
        harvest_done.set()

    thread = threading.Thread(target=run_harvest)
    thread.start()
    assert entered.wait(timeout=5), "采集未如预期卡在翻页"

    # 翻页等待期间发起清理：不与采集锁冲突，应立即成功
    cleanup_resp = api.post(
        "/api/v1/cleanup", json={"until": "2026-10-13"}
    )
    assert cleanup_resp.status_code == 200
    assert cleanup_resp.json()["cleared_identifiers"] == [REC13]
    assert _get(api, REC13)["write_guarded"] is True

    release.set()
    thread.join(timeout=10)
    assert harvest_done.is_set()
    assert harvest_result["status_code"] == 200
    assert harvest_result["body"]["fetched"] == 2


class _FakeClient:
    def __init__(self, records):
        self._records = records

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def list_records_first(self, *, from_date, until_date):
        return OAIPage(records=list(self._records),
                       resumption_token=None,
                       complete_list_size=len(self._records), cursor=0)

    def list_records_resume(self, token):  # pragma: no cover - 单页不触发
        raise AssertionError("unexpected resume")

    def close(self):
        pass
