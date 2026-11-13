"""样例 OAI 源 admin 版本切换端点的协议测试。"""
from __future__ import annotations

import json
import urllib.request
import urllib.error
import xml.etree.ElementTree as ET
from urllib.parse import urlencode

NS = "{http://www.openarchives.org/OAI/2.0/}"
DC_TITLE = "{http://purl.org/dc/elements/1.1/}title"
REC13 = "oai:sample.lib:rec-013"


def _base(oai_mod):
    return oai_mod.BASE_URL.rsplit("/oai", 1)[0]


def _records(oai_mod, from_date=None, until_date=None):
    # 直接对过滤后的生效记录走 XML 渲染，规避 PAGE_SIZE 分页（本测试关注版本内容）
    matched = oai_mod._filter_records(from_date, until_date)
    root = ET.Element(f"{{{oai_mod.OAI_NS}}}ListRecords")
    for record in matched:
        oai_mod._record_element(root, record)
    out = []
    for rec in root.findall(f"{NS}record"):
        header = rec.find(f"{NS}header")
        title_el = rec.find(f".//{DC_TITLE}")
        out.append({
            "identifier": header.find(f"{NS}identifier").text,
            "datestamp": header.find(f"{NS}datestamp").text,
            "deleted": header.get("status") == "deleted",
            "title": title_el.text if title_el is not None else None,
        })
    return out


def _find(records, identifier):
    return next(r for r in records if r["identifier"] == identifier)


def _admin(oai_mod, identifier, *, payload=None, query=None):
    base = oai_mod.BASE_URL.rsplit("/oai", 1)[0]
    url = f"{base}/admin/records/{identifier}"
    if query:
        url = f"{url}?{urlencode(query)}"
    data = json.dumps(payload).encode() if payload is not None else None
    req = urllib.request.Request(url, data=data, method="POST")
    if data is not None:
        req.add_header("Content-Type", "application/json")
    try:
        with urllib.request.urlopen(req, timeout=5) as resp:
            return resp.status, json.loads(resp.read().decode())
    except urllib.error.HTTPError as exc:
        return exc.code, json.loads(exc.read().decode())


def test_default_rec13_is_deleted(client):
    _api, _db, oai_mod = client
    rec = _find(_records(oai_mod, until_date="2026-10-31"), REC13)
    assert rec["deleted"] is True
    assert rec["datestamp"] == "2026-10-13"
    assert rec["title"] is None


def test_override_active_old_is_earlier_active(client):
    _api, _db, oai_mod = client
    oai_mod.set_override(REC13, "active-old")
    try:
        rec = _find(_records(oai_mod, until_date="2026-10-31"), REC13)
        assert rec["deleted"] is False
        assert rec["datestamp"] == "2026-10-12"
        assert "旧版" in rec["title"]
    finally:
        oai_mod.set_override(REC13, None)


def test_override_active_same_keeps_day_and_becomes_active(client):
    _api, _db, oai_mod = client
    oai_mod.set_override(REC13, "active-same")
    try:
        rec = _find(_records(oai_mod, until_date="2026-10-31"), REC13)
        assert rec["deleted"] is False
        assert rec["datestamp"] == "2026-10-13"
        assert "同日在架版" in rec["title"]
    finally:
        oai_mod.set_override(REC13, None)


def test_override_active_new_is_later_and_filterable_by_date(client):
    _api, _db, oai_mod = client
    oai_mod.set_override(REC13, "active-new")
    try:
        # 新版 datestamp 在 11 月，10 月窗口采不到
        october = _records(oai_mod, from_date="2026-10-01", until_date="2026-10-31")
        assert all(r["identifier"] != REC13 for r in october)

        rec = _find(
            _records(oai_mod, from_date="2026-10-01", until_date="2026-11-30"),
            REC13,
        )
        assert rec["deleted"] is False
        assert rec["datestamp"] == "2026-11-12"
        assert "新版" in rec["title"]
    finally:
        oai_mod.set_override(REC13, None)


def test_override_deleted_new_keeps_deleted_status(client):
    _api, _db, oai_mod = client
    oai_mod.set_override(REC13, "deleted-new")
    try:
        rec = _find(
            _records(oai_mod, from_date="2026-10-01", until_date="2026-11-30"),
            REC13,
        )
        assert rec["deleted"] is True
        assert rec["datestamp"] == "2026-11-12"
        assert rec["title"] is None
    finally:
        oai_mod.set_override(REC13, None)


def test_override_reset_restores_builtin_version(client):
    _api, _db, oai_mod = client
    oai_mod.set_override(REC13, "active-new")
    assert oai_mod.overrides_snapshot() == {REC13: "active-new"}
    oai_mod.set_override(REC13, None)
    assert oai_mod.overrides_snapshot() == {}
    rec = _find(_records(oai_mod, until_date="2026-10-31"), REC13)
    assert rec["deleted"] is True
    assert rec["datestamp"] == "2026-10-13"


def test_admin_http_endpoint_json_body_and_validation(client):
    _api, _db, oai_mod = client
    try:
        # JSON 请求体切换版本
        status, body = _admin(oai_mod, REC13, payload={"version": "active-same"})
        assert status == 200 and body["ok"] is True
        assert oai_mod.overrides_snapshot()[REC13] == "active-same"

        # 非法版本返回 400 且不改变覆盖
        status, _ = _admin(oai_mod, REC13, payload={"version": "bogus"})
        assert status == 400
        assert oai_mod.overrides_snapshot()[REC13] == "active-same"

        # 未知标识返回 404
        status, _ = _admin(oai_mod, "oai:sample.lib:rec-999",
                           query={"version": "active-new"})
        assert status == 404

        # query 参数方式 + default 清除覆盖
        status, body = _admin(oai_mod, REC13, query={"version": "default"})
        assert status == 200 and body["version"] == "default"
        assert REC13 not in oai_mod.overrides_snapshot()
    finally:
        oai_mod.set_override(REC13, None)


def test_admin_state_reports_overrides(client):
    _api, _db, oai_mod = client
    oai_mod.set_override(REC13, "active-new")
    try:
        with urllib.request.urlopen(
            f"{_base(oai_mod)}/admin/state", timeout=5
        ) as resp:
            state = json.loads(resp.read().decode())
        assert state["overrides"] == {REC13: "active-new"}
    finally:
        oai_mod.set_override(REC13, None)
