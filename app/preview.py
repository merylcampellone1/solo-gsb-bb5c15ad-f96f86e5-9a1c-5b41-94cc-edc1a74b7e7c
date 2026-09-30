"""原料声明更正 / 清洗撤销的“预演 + 凭据提交”。

预演不落库：在内存中叠加拟变更（compute_trace 的 decl_overrides /
revoked_cleaning_ids），找出变更前后实时结论**确有差异**的下游批次，
并对比这些批次已签发标签与变更前后实时结论的差异；设备残留传播自动计入。

提交凭据是无状态 HMAC 签名令牌：内含拟变更内容，以及预演时
“变更前后确有差异”批次集合的结论签名。提交时在同一写事务内
（BEGIN IMMEDIATE）按当前数据重新预演并重新计算该集合：
- 差异批次集合或其变更前后签名与凭据不一致（影响本次差异的记录已变化，
  包括插入了会产生新差异的加工/清洗/声明）→ 409，要求重新预演；
- 无关记录更新（其它设备、不影响差异的加工/清洗/声明、新签发标签）
  不改变该集合，凭据不失效；
- 一致则原子应用变更，并按落库后的真实状态返回实际影响。
"""
import base64
import hashlib
import hmac
import json
import os
import secrets
from datetime import timedelta

from .db import write_tx
from .service import (
    BadRequest,
    Conflict,
    NotFound,
    from_store,
    iso_z,
    now_utc,
    to_store,
    trace_view,
    batch_kind,
)

CRED_TTL = timedelta(seconds=int(os.environ.get("PREVIEW_TTL_SECONDS", "3600")))
_PROCESS_SECRET = secrets.token_hex(32)


class PreviewStale(Conflict):
    """凭据与当前数据不一致：影响本次差异的记录已变化，须重新预演。"""


class InvalidCredential(BadRequest):
    """凭据格式非法或签名不匹配。"""


# ---------------------------------------------------------------- 签名与凭据

def _secret() -> bytes:
    # 优先使用环境变量固定密钥（多进程/重启后旧凭据仍可用）；
    # 未配置时按进程生成（重启后旧凭据自然失效）。
    return os.environ.get("PREVIEW_SECRET", _PROCESS_SECRET).encode()


def _canon(obj) -> bytes:
    return json.dumps(obj, ensure_ascii=False, sort_keys=True,
                      separators=(",", ":")).encode()


def _sha(obj) -> str:
    return hashlib.sha256(_canon(obj)).hexdigest()


def issue_credential(payload: dict) -> dict:
    body = base64.urlsafe_b64encode(_canon(payload)).decode()
    sig = hmac.new(_secret(), body.encode(), hashlib.sha256).hexdigest()
    return {"token": f"{body}.{sig}",
            "expires_at": iso_z(to_store(from_store(payload["exp"])))}


def parse_credential(token: str) -> dict:
    try:
        body, sig = token.split(".", 1)
    except (AttributeError, ValueError):
        raise InvalidCredential("预演凭据无效：格式错误，请重新预演")
    expected = hmac.new(_secret(), body.encode(), hashlib.sha256).hexdigest()
    if not hmac.compare_digest(expected, sig):
        raise InvalidCredential("预演凭据无效：签名不匹配，请重新预演")
    try:
        payload = json.loads(base64.urlsafe_b64decode(body.encode()))
    except Exception:
        raise InvalidCredential("预演凭据无效：内容无法解析，请重新预演")
    if now_utc().isoformat() > payload["exp"]:
        raise PreviewStale("预演凭据已过期，请重新预演")
    return payload


# ---------------------------------------------------------------- 批次结构图

def _load_graph(conn) -> dict:
    """加载加工图：投入边 + 同设备“上一次加工 -> 下一次加工”的残留结构边。"""
    procs = {}
    inputs: dict[str, list[str]] = {}
    outputs: dict[str, list[str]] = {}
    for p in conn.execute("SELECT * FROM processings"):
        procs[p["processing_id"]] = {
            "processing_id": p["processing_id"],
            "equipment_id": p["equipment_id"],
            "start_utc": p["start_utc"],
            "end_utc": p["end_utc"],
        }
        inputs[p["processing_id"]] = [
            r["batch_id"] for r in conn.execute(
                "SELECT batch_id FROM processing_inputs WHERE processing_id = ?",
                (p["processing_id"],))]
        outputs[p["processing_id"]] = [
            r["batch_id"] for r in conn.execute(
                "SELECT batch_id FROM processing_outputs WHERE processing_id = ?",
                (p["processing_id"],))]

    by_equipment: dict[str, list[str]] = {}
    for pid, p in procs.items():
        by_equipment.setdefault(p["equipment_id"], []).append(pid)
    # 每台设备按开始时间排序，构造“上一次加工”的后继（结构边，不论是否有清洗）
    chain_next: dict[str, str | None] = {}
    for eq, pids in by_equipment.items():
        ordered = sorted(pids, key=lambda x: (procs[x]["start_utc"], x))
        for i, pid in enumerate(ordered):
            cur = procs[pid]
            nxt = next((cand for cand in ordered[i + 1:]
                        if procs[cand]["start_utc"] >= cur["end_utc"]), None)
            chain_next[pid] = nxt

    input_to_pids: dict[str, list[str]] = {}
    for pid, bids in inputs.items():
        for b in bids:
            input_to_pids.setdefault(b, []).append(pid)
    out_to_pid = {b: pid for pid, bids in outputs.items() for b in bids}
    return {"procs": procs, "inputs": inputs, "outputs": outputs,
            "chain_next": chain_next, "input_to_pids": input_to_pids,
            "out_to_pid": out_to_pid}


def _downstream(graph: dict, seeds: set[str]) -> set[str]:
    """从种子批次出发，沿投入边与设备残留结构边前向闭包。"""
    seen = set(seeds)
    stack = list(seeds)
    while stack:
        b = stack.pop()
        for pid in graph["input_to_pids"].get(b, ()):
            for o in graph["outputs"][pid]:
                if o not in seen:
                    seen.add(o)
                    stack.append(o)
        pid = graph["out_to_pid"].get(b)
        if pid:
            nxt = graph["chain_next"].get(pid)
            if nxt:
                for o in graph["outputs"][nxt]:
                    if o not in seen:
                        seen.add(o)
                        stack.append(o)
    return seen


def _cleaning_next_processing(graph: dict, equipment_id: str, cleaned_at: str):
    """清洗之后设备上的首个加工（且其上次加工在清洗之前结束）；
    撤销该清洗只会影响这个加工的产出闭包。无则 None。"""
    pids = [pid for pid, p in graph["procs"].items()
            if p["equipment_id"] == equipment_id and p["start_utc"] >= cleaned_at]
    if not pids:
        return None
    pid = min(pids, key=lambda x: (graph["procs"][x]["start_utc"], x))
    cur = graph["procs"][pid]
    prevs = [q for q, p in graph["procs"].items()
             if p["equipment_id"] == equipment_id
             and p["end_utc"] <= cur["start_utc"]]
    if not prevs:
        return None
    prev = max(prevs, key=lambda x: (graph["procs"][x]["end_utc"], x))
    if graph["procs"][prev]["end_utc"] > cleaned_at:
        return None  # 清洗不在两次加工之间（上次加工尚未结束）
    return pid


# ---------------------------------------------------------------- 差异计算

def _strip_proposed(node):
    if isinstance(node, dict):
        return {k: _strip_proposed(v) for k, v in node.items() if k != "proposed"}
    if isinstance(node, list):
        return [_strip_proposed(v) for v in node]
    return node


def _view_signature(view: dict | None) -> str | None:
    if view is None:
        return None
    return _sha({
        "pending": view["pending"],
        "allergens": view["allergens"],
        "allergen_paths": view["allergen_paths"],
        "pending_paths": view["pending_paths"],
        "basis": _strip_proposed(view["basis"]),
    })


def _path_set(paths: list) -> set[str]:
    return {_sha(p) for p in paths}


def _paths_delta(before: list, after: list):
    bmap = {_sha(p): p for p in before}
    amap = {_sha(p): p for p in after}
    removed = [bmap[h] for h in sorted(set(bmap) - set(amap))]
    added = [amap[h] for h in sorted(set(amap) - set(bmap))]
    return removed, added


def _basis_changes(before_view: dict, after_view: dict) -> dict:
    changes = {}
    b_mat = {d["batch_id"]: _strip_proposed(d)
             for d in before_view["basis"]["material_declarations"]}
    a_mat = {d["batch_id"]: _strip_proposed(d)
             for d in after_view["basis"]["material_declarations"]}
    mat = {}
    for bid in sorted(set(b_mat) | set(a_mat)):
        if b_mat.get(bid) != a_mat.get(bid):
            mat[bid] = {"before": b_mat.get(bid), "after": a_mat.get(bid)}
    if mat:
        changes["material_declarations"] = mat
    b_cl = {d["id"] for d in before_view["basis"]["cleanings"]}
    a_cl = {d["id"] for d in after_view["basis"]["cleanings"]}
    if b_cl != a_cl:
        changes["cleanings"] = {"added": sorted(a_cl - b_cl),
                                "removed": sorted(b_cl - a_cl)}
    return changes


def _view_changes(before_view: dict, after_view: dict) -> dict:
    changes = {}
    b_set, a_set = set(before_view["allergens"]), set(after_view["allergens"])
    if b_set != a_set:
        changes["allergens"] = {"added": sorted(a_set - b_set),
                                "removed": sorted(b_set - a_set)}
    if before_view["pending"] != after_view["pending"]:
        changes["pending"] = {"before": before_view["pending"],
                              "after": after_view["pending"]}
    path_changes = {}
    for a in sorted(b_set | a_set):
        removed, added = _paths_delta(
            before_view["allergen_paths"].get(a, []),
            after_view["allergen_paths"].get(a, []))
        if removed or added:
            path_changes[a] = {"removed_paths": removed, "added_paths": added}
    if path_changes:
        changes["allergen_paths"] = path_changes
    removed, added = _paths_delta(before_view["pending_paths"],
                                  after_view["pending_paths"])
    if removed or added:
        changes["pending_paths"] = {"removed_paths": removed,
                                    "added_paths": added}
    basis = _basis_changes(before_view, after_view)
    if basis:
        changes["basis"] = basis
    return changes


def _label_differences(conn, batch_id: str, view: dict) -> dict:
    """已签发标签（永不变更）相对某实时结论的差异。"""
    rows = conn.execute(
        "SELECT id, issued_at, supersedes_id, snapshot FROM labels"
        " WHERE batch_id = ? ORDER BY id", (batch_id,)).fetchall()
    labels = []
    divergence = 0
    live_paths = {a: _path_set(v) for a, v in view["allergen_paths"].items()}
    for r in rows:
        snap = json.loads(r["snapshot"])
        concl = snap["conclusion"]
        diff = {}
        l_set, v_set = set(concl["allergens"]), set(view["allergens"])
        if l_set != v_set:
            diff["allergens"] = {"added_realtime": sorted(v_set - l_set),
                                 "removed_realtime": sorted(l_set - v_set)}
        if bool(concl["pending"]) != bool(view["pending"]):
            diff["pending"] = {"label": bool(concl["pending"]),
                               "realtime": bool(view["pending"])}
        changed = [a for a in sorted(l_set | v_set)
                   if _path_set(concl["allergen_paths"].get(a, []))
                   != live_paths.get(a, set())]
        if changed:
            diff["allergen_paths_changed"] = changed
        if _path_set(concl.get("pending_paths", [])) != _path_set(view["pending_paths"]):
            diff["pending_paths_changed"] = True
        if diff:
            divergence += 1
        labels.append({
            "label_id": r["id"],
            "issued_at": iso_z(r["issued_at"]),
            "supersedes_id": r["supersedes_id"],
            "identical": not diff,
            "differences": diff,
        })
    return {"labels": labels, "conclusion_divergence_count": divergence}


def _evaluate(conn, batches: set[str], decl_overrides=None,
              revoked_cleaning_ids=None) -> dict:
    """对候选批次逐个计算实时（或叠加预演后的）溯源视图。"""
    views = {}
    for bid in sorted(batches):
        kind, _ = batch_kind(conn, bid)
        if not kind:
            continue
        views[bid] = trace_view(
            conn, bid, decl_overrides=decl_overrides,
            revoked_cleaning_ids=revoked_cleaning_ids)
    return views


def _affected_batches(before: dict, after: dict) -> list[str]:
    return sorted(bid for bid in set(before) | set(after)
                  if _view_signature(before.get(bid))
                  != _view_signature(after.get(bid)))


def _affected_entries(conn, before: dict, after: dict) -> list[dict]:
    entries = []
    for bid in _affected_batches(before, after):
        b, a = before.get(bid), after.get(bid)
        entries.append({
            "batch_id": bid,
            "kind": (a or b)["kind"],
            "before": b,
            "after": a,
            "changes": _view_changes(b, a) if b and a else
                       {"batch_newly_in_scope": True},
            "issued_labels": {
                "vs_before": (_label_differences(conn, bid, b) if b
                              else {"labels": [], "conclusion_divergence_count": 0}),
                "vs_after": (_label_differences(conn, bid, a) if a
                             else {"labels": [], "conclusion_divergence_count": 0}),
            },
        })
    return entries


def _affected_maps(before: dict, after: dict) -> tuple[dict, dict]:
    """凭据载荷：仅记录“确有差异”批次变更前后的结论签名。"""
    mb, ma = {}, {}
    for bid in _affected_batches(before, after):
        mb[bid] = _view_signature(before.get(bid))
        ma[bid] = _view_signature(after.get(bid))
    return mb, ma


def _envelope(conn, *, target_change, before, after,
              applied, credential_payload) -> dict:
    return {
        "applied": applied,
        "change": target_change,
        "credential": (issue_credential(credential_payload)
                       if credential_payload else None),
        "affected_count": len(_affected_batches(before, after)),
        "affected_batches": _affected_entries(conn, before, after),
    }


def _new_payload(change_type: str, target: dict, change: dict,
                 before: dict, after: dict) -> dict:
    mb, ma = _affected_maps(before, after)
    now = now_utc()
    return {
        "type": change_type,
        "target": target,
        "change": change,
        "affected_before": mb,
        "affected_after": ma,
        "iat": now.isoformat(),
        "exp": (now + CRED_TTL).isoformat(),
    }


# ---------------------------------------------------------------- 预演：声明更正

def preview_declaration(conn, data) -> dict:
    declared_at = to_store(data.declared_at) if data.declared_at \
        else to_store(now_utc())
    batch_id = data.batch_id
    cur = conn.execute(
        "SELECT * FROM material_declarations WHERE batch_id = ?"
        " ORDER BY version DESC LIMIT 1", (batch_id,)).fetchone()
    if not cur:
        if not conn.execute(
                "SELECT 1 FROM material_batches WHERE batch_id = ?",
                (batch_id,)).fetchone():
            raise NotFound(f"未知原料批次: {batch_id}")
    proposed = (data.allergens, data.pending, declared_at)
    graph = _load_graph(conn)
    batches = _downstream(graph, {batch_id})
    before = _evaluate(conn, batches)
    after = _evaluate(conn, batches, decl_overrides={batch_id: proposed})
    payload = _new_payload(
        "material_declaration_correction", {"batch_id": batch_id},
        {"allergens": proposed[0], "pending": bool(proposed[1]),
         "declared_at": proposed[2]}, before, after)
    return _declaration_envelope(conn, payload, before, after, applied=False,
                                 current_row=cur, proposed=proposed)


def _declaration_summary(row) -> dict:
    return {"version": row["version"], "allergens": json.loads(row["allergens"]),
            "pending": bool(row["pending"]), "declared_at": iso_z(row["declared_at"])}


def _declaration_envelope(conn, payload, before, after, *, applied,
                          current_row, proposed) -> dict:
    batch_id = payload["target"]["batch_id"]
    proposed_view = {
        "version": current_row["version"] if applied
                   else current_row["version"] + 1,
        "allergens": proposed[0],
        "pending": bool(proposed[1]),
        "declared_at": iso_z(proposed[2]),
    }
    target_change = {
        "type": "material_declaration_correction",
        "batch_id": batch_id,
        "current": _declaration_summary(current_row),
        "proposed": proposed_view,
    }
    return _envelope(conn, target_change=target_change, before=before,
                     after=after, applied=applied,
                     credential_payload=None if applied else payload)


# ---------------------------------------------------------------- 预演：清洗撤销

def preview_cleaning_revocation(conn, cleaning_id: int) -> dict:
    row = conn.execute("SELECT * FROM cleanings WHERE id = ?",
                       (cleaning_id,)).fetchone()
    if not row:
        raise NotFound(f"未知清洗记录: {cleaning_id}")
    if row["revoked"]:
        raise Conflict(f"清洗记录 {cleaning_id} 已撤销")
    before, after = _revocation_views(conn, row)
    payload = _new_payload(
        "cleaning_revocation", {"cleaning_id": cleaning_id}, {}, before, after)
    return _revocation_envelope(conn, payload, before, after, applied=False,
                                cleaning_row=row)


def _revocation_views(conn, row) -> tuple[dict, dict]:
    graph = _load_graph(conn)
    nxt = _cleaning_next_processing(graph, row["equipment_id"], row["cleaned_at"])
    seeds = set(graph["outputs"][nxt]) if nxt else set()
    batches = _downstream(graph, seeds) if seeds else set()
    before = _evaluate(conn, batches)
    after = _evaluate(conn, batches, revoked_cleaning_ids={row["id"]})
    return before, after


def _cleaning_view(row) -> dict:
    return {"id": row["id"], "equipment_id": row["equipment_id"],
            "cleaned_at": iso_z(row["cleaned_at"]),
            "verified": bool(row["verified"]), "revoked": bool(row["revoked"]),
            "effective": bool(row["verified"] and not row["revoked"])}


def _revocation_envelope(conn, payload, before, after, *, applied,
                         cleaning_row) -> dict:
    target_change = {
        "type": "cleaning_revocation",
        "cleaning_id": cleaning_row["id"],
        "current": _cleaning_view(cleaning_row),
        "proposed": {**_cleaning_view(cleaning_row),
                     "revoked": True, "effective": False},
    }
    return _envelope(conn, target_change=target_change, before=before,
                     after=after, applied=applied,
                     credential_payload=None if applied else payload)


# ---------------------------------------------------------------- 提交

def commit_change(conn, token: str) -> dict:
    payload = parse_credential(token)
    with write_tx(conn):
        if payload["type"] == "material_declaration_correction":
            return _commit_declaration(conn, payload)
        if payload["type"] == "cleaning_revocation":
            return _commit_revocation(conn, payload)
        raise InvalidCredential("预演凭据无效：未知变更类型")


def _guard(payload, before, after):
    mb, ma = _affected_maps(before, after)
    if mb != payload["affected_before"] or ma != payload["affected_after"]:
        raise PreviewStale("影响本次差异的记录已变化，请重新预演后再提交")


def _commit_declaration(conn, payload: dict) -> dict:
    batch_id = payload["target"]["batch_id"]
    if not conn.execute(
            "SELECT 1 FROM material_batches WHERE batch_id = ?",
            (batch_id,)).fetchone():
        raise NotFound(f"未知原料批次: {batch_id}")
    cur = conn.execute(
        "SELECT * FROM material_declarations WHERE batch_id = ?"
        " ORDER BY version DESC LIMIT 1", (batch_id,)).fetchone()
    allergens = payload["change"]["allergens"]
    pending = payload["change"]["pending"]
    declared_at = payload["change"]["declared_at"]
    proposed = (allergens, pending, declared_at)

    # 事务内按当前数据重新预演，凭据签名保证拟变更内容未被篡改
    graph = _load_graph(conn)
    batches = _downstream(graph, {batch_id})
    before = _evaluate(conn, batches)
    after_sim = _evaluate(conn, batches, decl_overrides={batch_id: proposed})
    _guard(payload, before, after_sim)

    # 原子应用：追加声明新版本（与既有 /declarations 接口一致）
    conn.execute(
        "INSERT INTO material_declarations (batch_id, version, allergens, pending, declared_at)"
        " VALUES (?, ?, ?, ?, ?)",
        (batch_id, cur["version"] + 1, json.dumps(allergens),
         int(pending), declared_at))
    new_cur = conn.execute(
        "SELECT * FROM material_declarations WHERE batch_id = ?"
        " ORDER BY version DESC LIMIT 1", (batch_id,)).fetchone()
    after = _evaluate(conn, batches)
    return _declaration_envelope(conn, payload, before, after, applied=True,
                                 current_row=new_cur, proposed=proposed)


def _commit_revocation(conn, payload: dict) -> dict:
    cleaning_id = payload["target"]["cleaning_id"]
    row = conn.execute("SELECT * FROM cleanings WHERE id = ?",
                       (cleaning_id,)).fetchone()
    if not row:
        raise NotFound(f"未知清洗记录: {cleaning_id}")
    if row["revoked"]:
        raise Conflict(f"清洗记录 {cleaning_id} 已撤销")

    before, after_sim = _revocation_views(conn, row)
    _guard(payload, before, after_sim)

    conn.execute("UPDATE cleanings SET revoked = 1 WHERE id = ?", (cleaning_id,))
    new_row = conn.execute("SELECT * FROM cleanings WHERE id = ?",
                           (cleaning_id,)).fetchone()
    _, after = _revocation_views(conn, new_row)
    return _revocation_envelope(conn, payload, before, after, applied=True,
                                cleaning_row=new_row)
