"""端到端测试：登记校验、残留继承、清洗与撤销、声明更正、标签冻结。"""
import os
import tempfile

import pytest
from fastapi.testclient import TestClient

# 每个测试会话使用独立数据库文件
_tmp = tempfile.mkdtemp(prefix="allergen-test-")
os.environ["DB_PATH"] = os.path.join(_tmp, "test.db")

from app import db  # noqa: E402
from app.main import app  # noqa: E402


@pytest.fixture()
def client():
    # 重置数据库
    db.init_db()
    conn = db.connect()
    for t in ["material_declarations", "material_batches", "processing_inputs",
              "processing_outputs", "processings", "cleanings", "labels"]:
        conn.execute(f"DELETE FROM {t}")
    conn.close()
    with TestClient(app) as c:
        yield c


def material(c, batch_id, allergens=None, pending=False, declared_at="2026-01-01T00:00:00Z"):
    return c.post("/materials", json={
        "batch_id": batch_id, "allergens": allergens or [],
        "pending": pending, "declared_at": declared_at})


def processing(c, pid, eq, start, end, inputs, outputs):
    return c.post("/processings", json={
        "processing_id": pid, "equipment_id": eq,
        "start_utc": start, "end_utc": end,
        "inputs": inputs, "outputs": outputs})


# ---------------------------------------------------------------- 基础登记与校验

def test_material_and_trace(client):
    r = material(client, "RM-1", allergens=["milk", "peanut"])
    assert r.status_code == 201, r.text
    t = client.get("/batches/RM-1/trace").json()
    assert t["allergens"] == ["milk", "peanut"]
    assert t["pending"] is False
    assert t["allergen_paths"]["milk"][0][0]["via"] == "declaration"


def test_pending_material(client):
    material(client, "RM-P", pending=True)
    t = client.get("/batches/RM-P/trace").json()
    assert t["pending"] is True
    assert t["pending_paths"][0][0]["batch_id"] == "RM-P"


def test_duplicate_material_rejected(client):
    assert material(client, "RM-1").status_code == 201
    r = material(client, "RM-1")
    assert r.status_code == 409


def test_invalid_time_range_rejected(client):
    material(client, "RM-1")
    r = processing(client, "P1", "EQ-1",
                   "2026-01-02T10:00:00Z", "2026-01-02T09:00:00Z",
                   ["RM-1"], ["OUT-1"])
    assert r.status_code == 400
    assert "无效时序" in r.json()["detail"]
    # 原状态保持：无加工记录
    assert client.get("/processings").json() == []


def test_naive_datetime_rejected(client):
    material(client, "RM-1")
    r = processing(client, "P1", "EQ-1",
                   "2026-01-02T08:00:00", "2026-01-02T09:00:00",
                   ["RM-1"], ["OUT-1"])
    assert r.status_code == 422  # 必须带时区


def test_unknown_input_rejected(client):
    r = processing(client, "P1", "EQ-1",
                   "2026-01-02T08:00:00Z", "2026-01-02T09:00:00Z",
                   ["GHOST"], ["OUT-1"])
    assert r.status_code == 400


def test_input_not_available_before_start(client):
    # 原料 1 月 2 日 10 点才登记，加工 9 点开始 -> 不可用
    material(client, "RM-LATE", declared_at="2026-01-02T10:00:00Z")
    r = processing(client, "P1", "EQ-1",
                   "2026-01-02T09:00:00Z", "2026-01-02T11:00:00Z",
                   ["RM-LATE"], ["OUT-1"])
    assert r.status_code == 400
    assert "不可用" in r.json()["detail"]
    # 上游产出未结束就投入也不可用
    material(client, "RM-2", declared_at="2026-01-01T00:00:00Z")
    assert processing(client, "P2", "EQ-1",
                      "2026-01-02T11:00:00Z", "2026-01-02T12:00:00Z",
                      ["RM-2"], ["MID-1"]).status_code == 201
    r = processing(client, "P3", "EQ-2",
                   "2026-01-02T11:30:00Z", "2026-01-02T12:30:00Z",
                   ["MID-1"], ["OUT-3"])
    assert r.status_code == 400  # MID-1 12:00 才可用


def test_equipment_overlap_rejected(client):
    material(client, "RM-1")
    material(client, "RM-2")
    assert processing(client, "P1", "EQ-1",
                      "2026-01-02T08:00:00Z", "2026-01-02T10:00:00Z",
                      ["RM-1"], ["OUT-1"]).status_code == 201
    # 部分重叠
    r = processing(client, "P2", "EQ-1",
                   "2026-01-02T09:00:00Z", "2026-01-02T11:00:00Z",
                   ["RM-2"], ["OUT-2"])
    assert r.status_code == 409
    assert "重叠" in r.json()["detail"]
    # 相邻不重叠（前序结束即开始）允许
    assert processing(client, "P3", "EQ-1",
                      "2026-01-02T10:00:00Z", "2026-01-02T11:00:00Z",
                      ["RM-2"], ["OUT-3"]).status_code == 201
    # 不同设备同时间段允许
    assert processing(client, "P4", "EQ-2",
                      "2026-01-02T08:30:00Z", "2026-01-02T09:30:00Z",
                      ["RM-2"], ["OUT-4"]).status_code == 201


def test_cycle_and_output_conflict_rejected(client):
    material(client, "RM-1")
    # 自环：同一批次既是投入又是产出
    r = processing(client, "P1", "EQ-1",
                   "2026-01-02T08:00:00Z", "2026-01-02T09:00:00Z",
                   ["RM-1"], ["RM-1"])
    assert r.status_code == 409
    assert "成环" in r.json()["detail"]
    # 产出批次号已被占用
    assert processing(client, "P2", "EQ-1",
                      "2026-01-02T08:00:00Z", "2026-01-02T09:00:00Z",
                      ["RM-1"], ["OUT-1"]).status_code == 201
    r = processing(client, "P3", "EQ-1",
                   "2026-01-02T10:00:00Z", "2026-01-02T11:00:00Z",
                   ["RM-1"], ["OUT-1"])
    assert r.status_code == 409
    assert "冲突" in r.json()["detail"]
    # 把已登记的原料当产出也是来源冲突
    r = processing(client, "P4", "EQ-1",
                   "2026-01-02T10:00:00Z", "2026-01-02T11:00:00Z",
                   ["OUT-1"], ["RM-1"])
    assert r.status_code == 409


# ---------------------------------------------------------------- 继承与残留

def _build_line(client):
    """EQ-1: P1(RM-MILK -> OUT-1) 08-09, P2(RM-OAT -> OUT-2) 10-11"""
    material(client, "RM-MILK", allergens=["milk"])
    material(client, "RM-OAT", allergens=["gluten"])
    assert processing(client, "P1", "EQ-1",
                      "2026-01-02T08:00:00Z", "2026-01-02T09:00:00Z",
                      ["RM-MILK"], ["OUT-1"]).status_code == 201
    assert processing(client, "P2", "EQ-1",
                      "2026-01-02T10:00:00Z", "2026-01-02T11:00:00Z",
                      ["RM-OAT"], ["OUT-2"]).status_code == 201


def test_residue_inherited_without_cleaning(client):
    _build_line(client)
    t = client.get("/batches/OUT-2/trace").json()
    # 投入继承 gluten + 设备残留 milk
    assert t["allergens"] == ["gluten", "milk"]
    residue_paths = [p for p in t["allergen_paths"]["milk"]
                     if p[-1]["via"] == "equipment_residue"]
    assert residue_paths, "milk 应来自设备残留"
    p = residue_paths[0]
    assert [s.get("batch_id") for s in p] == ["RM-MILK", "OUT-1", "OUT-2"]
    assert p[-1]["equipment_id"] == "EQ-1"


def test_verified_cleaning_clears_residue(client):
    _build_line(client)
    r = client.post("/cleanings", json={
        "equipment_id": "EQ-1", "cleaned_at": "2026-01-02T09:30:00Z",
        "verified": True})
    assert r.status_code == 201
    t = client.get("/batches/OUT-2/trace").json()
    assert t["allergens"] == ["gluten"]
    assert t["basis"]["cleanings"][0]["id"] == r.json()["id"]


def test_unverified_cleaning_does_not_clear(client):
    _build_line(client)
    client.post("/cleanings", json={
        "equipment_id": "EQ-1", "cleaned_at": "2026-01-02T09:30:00Z",
        "verified": False})
    t = client.get("/batches/OUT-2/trace").json()
    assert "milk" in t["allergens"]


def test_revoked_cleaning_restores_residue(client):
    _build_line(client)
    cid = client.post("/cleanings", json={
        "equipment_id": "EQ-1", "cleaned_at": "2026-01-02T09:30:00Z",
        "verified": True}).json()["id"]
    assert client.get("/batches/OUT-2/trace").json()["allergens"] == ["gluten"]
    # 撤销清洗 -> 未签发结论按当前有效记录重算，残留恢复
    r = client.post(f"/cleanings/{cid}/revoke")
    assert r.status_code == 200
    assert r.json()["revoked"] is True
    t = client.get("/batches/OUT-2/trace").json()
    assert "milk" in t["allergens"]
    # 重复撤销拒绝
    assert client.post(f"/cleanings/{cid}/revoke").status_code == 409


def test_cleaning_outside_window_does_not_clear(client):
    _build_line(client)
    # 清洗发生在 P2 开始之后，不在两次加工之间
    client.post("/cleanings", json={
        "equipment_id": "EQ-1", "cleaned_at": "2026-01-02T10:30:00Z",
        "verified": True})
    t = client.get("/batches/OUT-2/trace").json()
    assert "milk" in t["allergens"]


def test_pending_propagates_through_processing_and_residue(client):
    material(client, "RM-PEND", pending=True)
    material(client, "RM-OK", allergens=["soy"])
    assert processing(client, "P1", "EQ-1",
                      "2026-01-02T08:00:00Z", "2026-01-02T09:00:00Z",
                      ["RM-PEND"], ["OUT-1"]).status_code == 201
    assert processing(client, "P2", "EQ-1",
                      "2026-01-02T10:00:00Z", "2026-01-02T11:00:00Z",
                      ["RM-OK"], ["OUT-2"]).status_code == 201
    t = client.get("/batches/OUT-2/trace").json()
    assert t["pending"] is True  # 残留带入待确认
    assert t["pending_paths"][0][-1]["via"] == "equipment_residue"


# ---------------------------------------------------------------- 声明更正与标签

def test_declaration_correction_recomputes_live(client):
    _build_line(client)
    # 更正：RM-OAT 实际含 sesame
    r = client.post("/materials/RM-OAT/declarations",
                    json={"allergens": ["gluten", "sesame"], "pending": False})
    assert r.status_code == 201
    assert r.json()["version"] == 2
    t = client.get("/batches/OUT-2/trace").json()
    assert t["allergens"] == ["gluten", "milk", "sesame"]
    # 依据记录为第 2 版声明
    versions = {d["batch_id"]: d["version"]
                for d in t["basis"]["material_declarations"]}
    assert versions["RM-OAT"] == 2


def test_label_frozen_and_superseded(client):
    _build_line(client)
    # 签发标签
    r = client.post("/batches/OUT-2/labels", json={})
    assert r.status_code == 201
    label1 = r.json()
    assert label1["conclusion"]["allergens"] == ["gluten", "milk"]
    # 声明更正 + 清洗撤销等后续变更
    client.post("/materials/RM-OAT/declarations",
                json={"allergens": ["gluten", "sesame"]})
    cid = client.post("/cleanings", json={
        "equipment_id": "EQ-1", "cleaned_at": "2026-01-02T09:30:00Z",
        "verified": True}).json()["id"]
    # 旧标签不变
    old = client.get(f"/labels/{label1['label_id']}").json()
    assert old["conclusion"]["allergens"] == ["gluten", "milk"]
    # 实时结论已变（清洗生效，milk 消失；sesame 出现）
    live = client.get("/batches/OUT-2/trace").json()
    assert live["allergens"] == ["gluten", "sesame"]
    # 签发关联旧版的新标签
    r = client.post("/batches/OUT-2/labels",
                    json={"supersedes_id": label1["label_id"]})
    assert r.status_code == 201
    label2 = r.json()
    assert label2["supersedes_id"] == label1["label_id"]
    assert label2["conclusion"]["allergens"] == ["gluten", "sesame"]
    # 撤销清洗后，两张已签发标签仍不变
    client.post(f"/cleanings/{cid}/revoke")
    assert client.get(f"/labels/{label2['label_id']}").json()[
        "conclusion"]["allergens"] == ["gluten", "sesame"]
    # 新实时结论再次包含 milk
    assert "milk" in client.get("/batches/OUT-2/trace").json()["allergens"]
    # 标签历史
    labels = client.get("/batches/OUT-2/labels").json()
    assert [l["label_id"] for l in labels] == [label1["label_id"], label2["label_id"]]


def test_label_supersedes_validation(client):
    _build_line(client)
    material(client, "RM-X")
    l1 = client.post("/batches/OUT-2/labels", json={}).json()
    # 旧标签属于别的批次 -> 拒绝
    r = client.post("/batches/RM-X/labels",
                    json={"supersedes_id": l1["label_id"]})
    assert r.status_code == 400
    # 旧标签不存在
    assert client.post("/batches/RM-X/labels",
                       json={"supersedes_id": 999}).status_code == 404


def test_unknown_batch_404(client):
    assert client.get("/batches/NOPE/trace").status_code == 404
    assert client.post("/batches/NOPE/labels", json={}).status_code == 404


# ---------------------------------------------------------------- 预演与凭据提交

def _two_step_line(client, verified_cleaning=True):
    """EQ-1: P1(RM-MILK -> OUT-1) 08-09, P2(RM-OAT -> OUT-2) 10-11，
    两次加工之间登记已验证清洗（milk 残留被清除）。"""
    material(client, "RM-MILK", allergens=["milk"])
    material(client, "RM-OAT", allergens=["gluten"])
    assert processing(client, "P1", "EQ-1",
                      "2026-01-02T08:00:00Z", "2026-01-02T09:00:00Z",
                      ["RM-MILK"], ["OUT-1"]).status_code == 201
    assert processing(client, "P2", "EQ-1",
                      "2026-01-02T10:00:00Z", "2026-01-02T11:00:00Z",
                      ["RM-OAT"], ["OUT-2"]).status_code == 201
    if verified_cleaning:
        cid = client.post("/cleanings", json={
            "equipment_id": "EQ-1", "cleaned_at": "2026-01-02T09:30:00Z",
            "verified": True}).json()["id"]
        return cid


def test_preview_does_not_persist(client):
    cid = _two_step_line(client)
    r = client.post(f"/previews/cleanings/{cid}/revocation")
    assert r.status_code == 200
    pv = r.json()
    assert pv["applied"] is False
    assert pv["credential"]["token"]
    assert pv["credential"]["expires_at"]
    # 预演不落库：清洗仍有效、实时结论无 milk
    assert client.get(f"/cleanings/{cid}").json()["revoked"] is False
    assert client.get("/batches/OUT-2/trace").json()["allergens"] == ["gluten"]
    # 预演结果显示撤销后 milk 经设备残留恢复
    entry = pv["affected_batches"][0]
    assert entry["batch_id"] == "OUT-2"
    assert entry["changes"]["allergens"] == {"added": ["milk"], "removed": []}
    milk_paths = entry["after"]["allergen_paths"]["milk"]
    assert milk_paths[0][-1]["via"] == "equipment_residue"
    assert entry["changes"]["basis"]["cleanings"] == {"added": [], "removed": [cid]}


def test_preview_shows_issued_label_differences(client):
    cid = _two_step_line(client)
    label = client.post("/batches/OUT-2/labels", json={}).json()  # 签发于 milk 已清除
    pv = client.post(f"/previews/cleanings/{cid}/revocation").json()
    entry = [b for b in pv["affected_batches"] if b["batch_id"] == "OUT-2"][0]
    # 变更前与标签一致；变更后标签相对实时结论出现 milk 差异
    vs_before = entry["issued_labels"]["vs_before"]["labels"][0]
    vs_after = entry["issued_labels"]["vs_after"]["labels"][0]
    assert vs_before["label_id"] == label["label_id"]
    assert vs_before["identical"] is True
    assert vs_after["identical"] is False
    assert vs_after["differences"]["allergens"] == {
        "added_realtime": ["milk"], "removed_realtime": []}
    assert "milk" in vs_after["differences"]["allergen_paths_changed"]


def test_preview_commit_applies_atomically_and_returns_actual_impact(client):
    cid = _two_step_line(client)
    pv = client.post(f"/previews/cleanings/{cid}/revocation").json()
    r = client.post("/previews/commit", json={"credential": pv["credential"]["token"]})
    assert r.status_code == 200
    body = r.json()
    assert body["applied"] is True
    assert body["credential"] is None
    assert body["change"]["current"]["revoked"] is True
    assert body["affected_count"] == 1
    assert "milk" in client.get("/batches/OUT-2/trace").json()["allergens"]


def test_unrelated_updates_do_not_invalidate_credential(client):
    cid = _two_step_line(client)
    pv = client.post(f"/previews/cleanings/{cid}/revocation").json()
    # 无关设备上的加工/清洗、无关原料声明、对受影响批次新签发标签均不使凭据失效
    material(client, "RM-X", allergens=["soy"])
    assert processing(client, "PX", "EQ-9",
                      "2026-01-02T08:00:00Z", "2026-01-02T09:00:00Z",
                      ["RM-X"], ["OUT-X"]).status_code == 201
    client.post("/cleanings", json={
        "equipment_id": "EQ-9", "cleaned_at": "2026-01-02T07:30:00Z",
        "verified": False})
    client.post("/batches/OUT-2/labels", json={})
    r = client.post("/previews/commit", json={"credential": pv["credential"]["token"]})
    assert r.status_code == 200
    assert r.json()["affected_batches"][0]["batch_id"] == "OUT-2"


def test_changed_relevant_record_rejects_commit(client):
    cid = _two_step_line(client)
    pv = client.post(f"/previews/cleanings/{cid}/revocation").json()
    # 通过既有接口先行撤销 → 影响本次差异的记录已变化
    assert client.post(f"/cleanings/{cid}/revoke").status_code == 200
    r = client.post("/previews/commit", json={"credential": pv["credential"]["token"]})
    assert r.status_code == 409


def test_new_affected_downstream_after_preview_rejects_commit(client):
    cid = _two_step_line(client)
    # 预演时只有 OUT-2 受影响
    pv = client.post(f"/previews/cleanings/{cid}/revocation").json()
    assert [b["batch_id"] for b in pv["affected_batches"]] == ["OUT-2"]
    # 在预演后、提交前插入一个新的受影响下游 P3（撤销后 milk 沿残留链继续传播）
    assert processing(client, "P3", "EQ-1",
                      "2026-01-02T12:00:00Z", "2026-01-02T13:00:00Z",
                      ["RM-OAT"], ["OUT-3"]).status_code == 201
    r = client.post("/previews/commit", json={"credential": pv["credential"]["token"]})
    assert r.status_code == 409
    assert "重新预演" in r.json()["detail"]
    # 被拒绝后原状态保持
    assert client.get(f"/cleanings/{cid}").json()["revoked"] is False


def test_declaration_preview_allergen_and_pending_paths(client):
    material(client, "RM-P", allergens=["milk"])
    assert processing(client, "P1", "EQ-1",
                      "2026-01-02T08:00:00Z", "2026-01-02T09:00:00Z",
                      ["RM-P"], ["OUT-1"]).status_code == 201
    r = client.post("/previews/material-declarations", json={
        "batch_id": "RM-P", "allergens": ["milk", "peanut"], "pending": True})
    assert r.status_code == 200
    pv = r.json()
    assert pv["change"]["current"]["version"] == 1
    assert pv["change"]["proposed"]["version"] == 2
    affected = {b["batch_id"]: b for b in pv["affected_batches"]}
    assert set(affected) == {"RM-P", "OUT-1"}
    ch = affected["OUT-1"]["changes"]
    assert ch["allergens"] == {"added": ["peanut"], "removed": []}
    assert ch["pending"] == {"before": False, "after": True}
    assert ch["pending_paths"]["added_paths"][0][-1]["via"] == "input"
    assert ch["basis"]["material_declarations"]["RM-P"]["after"]["version"] == 2
    # 提交后新版本落库、实时结论更新
    r = client.post("/previews/commit", json={"credential": pv["credential"]["token"]})
    assert r.status_code == 200
    assert r.json()["change"]["proposed"]["version"] == 2
    t = client.get("/batches/OUT-1/trace").json()
    assert t["allergens"] == ["milk", "peanut"]
    assert t["pending"] is True
    assert client.get("/materials/RM-P").json()["version"] == 2


def test_declaration_preview_residue_propagation_and_label_freeze(client):
    _two_step_line(client, verified_cleaning=False)
    label = client.post("/batches/OUT-2/labels", json={}).json()  # 冻结含 milk 的结论
    pv = client.post("/previews/material-declarations", json={
        "batch_id": "RM-MILK", "allergens": []}).json()  # 更正为不含 milk
    entry = [b for b in pv["affected_batches"] if b["batch_id"] == "OUT-2"][0]
    # 实时预演中 milk（含残留路径）消失
    assert entry["changes"]["allergens"] == {"added": [], "removed": ["milk"]}
    assert entry["changes"]["allergen_paths"]["milk"]["removed_paths"][0][-1][
        "via"] == "equipment_residue"
    # 已签发标签相对“变更后实时结论”显示 milk 差异，但标签本身不改写
    lab_diff = entry["issued_labels"]["vs_after"]["labels"][0]
    assert lab_diff["identical"] is False
    assert lab_diff["differences"]["allergens"] == {
        "added_realtime": [], "removed_realtime": ["milk"]}
    assert client.post(
        "/previews/commit", json={"credential": pv["credential"]["token"]}
    ).status_code == 200
    assert client.get("/batches/OUT-2/trace").json()["allergens"] == ["gluten"]
    frozen = client.get(f"/labels/{label['label_id']}").json()
    assert frozen["conclusion"]["allergens"] == ["gluten", "milk"]


def test_preview_errors_follow_existing_semantics(client):
    # 无效目标
    assert client.post("/previews/material-declarations",
                       json={"batch_id": "GHOST"}).status_code == 404
    assert client.post("/previews/cleanings/999/revocation").status_code == 404
    # 请求格式错误（缺 batch_id）
    assert client.post("/previews/material-declarations",
                       json={"allergens": ["x"]}).status_code == 422
    # 重复撤销沿用 409
    cid = client.post("/cleanings", json={
        "equipment_id": "EQ-1", "cleaned_at": "2026-01-02T09:30:00Z",
        "verified": True}).json()["id"]
    client.post(f"/cleanings/{cid}/revoke")
    assert client.post(f"/previews/cleanings/{cid}/revocation").status_code == 409
    # 凭据非法
    assert client.post("/previews/commit", json={"credential": "garbage"}
                       ).status_code == 400


def test_preview_credential_tamper_rejected(client):
    material(client, "RM-1", allergens=["milk"])
    pv = client.post("/previews/material-declarations",
                     json={"batch_id": "RM-1", "allergens": ["egg"]}).json()
    body, sig = pv["credential"]["token"].split(".", 1)
    import base64
    import json as _json
    payload = _json.loads(base64.urlsafe_b64decode(body))
    payload["change"]["allergens"] = ["peanut"]  # 篡改拟变更内容
    forged = base64.urlsafe_b64encode(
        _json.dumps(payload, sort_keys=True, separators=(",", ":")).encode()
    ).decode()
    r = client.post("/previews/commit", json={"credential": f"{forged}.{sig}"})
    assert r.status_code == 400


def test_existing_correction_revoke_and_label_apis_still_compatible(client):
    cid = _two_step_line(client)
    # 既有声明更正接口照常追加版本
    r = client.post("/materials/RM-OAT/declarations",
                    json={"allergens": ["gluten", "sesame"]})
    assert r.status_code == 201
    # 既有清洗撤销接口照常工作
    assert client.post(f"/cleanings/{cid}/revoke").status_code == 200
    # 既有 trace / 标签接口照常
    t = client.get("/batches/OUT-2/trace").json()
    assert set(t["allergens"]) == {"gluten", "milk", "sesame"}
    assert client.get("/batches/OUT-2/labels").status_code == 200

