"""核心业务逻辑：登记、校验（时序/可用性/重叠/来源环）、溯源计算、标签签发。

溯源模型
--------
- 原料批次：当前有效声明（最新版本）给出过敏原集合与待确认状态。
- 加工产出：继承全部投入批次的过敏原/待确认，并继承同一设备上一次加工的残留；
  若两次加工之间存在“已验证且未撤销”的清洗，则残留被清除。
- 来源路径：从声明起点到目标批次的链路，逐步标注经由哪个加工、
  是“投入(input)”还是“设备残留(equipment_residue)”。
"""
import json
import sqlite3
from datetime import datetime, timezone

from .db import write_tx


class NotFound(Exception):
    pass


class Conflict(Exception):
    pass


class BadRequest(Exception):
    pass


# ---------------------------------------------------------------- 时间工具

def now_utc() -> datetime:
    return datetime.now(timezone.utc)


def to_store(dt: datetime) -> str:
    """归一化为 UTC ISO 字符串入库（格式一致，可按字符串比较）。"""
    return dt.astimezone(timezone.utc).isoformat()


def from_store(s: str) -> datetime:
    return datetime.fromisoformat(s)


def iso_z(s: str) -> str:
    """展示用：+00:00 -> Z"""
    return s.replace("+00:00", "Z")


# ---------------------------------------------------------------- 批次查询

def _material(conn, batch_id: str):
    return conn.execute(
        "SELECT * FROM material_batches WHERE batch_id = ?", (batch_id,)
    ).fetchone()


def _output_of(conn, batch_id: str):
    return conn.execute(
        "SELECT * FROM processing_outputs WHERE batch_id = ?", (batch_id,)
    ).fetchone()


def batch_kind(conn, batch_id: str):
    """返回 ('material', row) / ('product', processing_row) / (None, None)。"""
    m = _material(conn, batch_id)
    if m:
        return "material", m
    out = _output_of(conn, batch_id)
    if out:
        proc = conn.execute(
            "SELECT * FROM processings WHERE processing_id = ?",
            (out["processing_id"],),
        ).fetchone()
        return "product", proc
    return None, None


def _available_at(conn, batch_id: str) -> datetime:
    """批次可用时间：原料=首次登记时间；加工产出=产出加工结束时间。"""
    kind, row = batch_kind(conn, batch_id)
    if kind == "material":
        return from_store(row["created_at"])
    if kind == "product":
        return from_store(row["end_utc"])
    raise NotFound(f"未知批次: {batch_id}")


def _current_declaration(conn, batch_id: str):
    return conn.execute(
        "SELECT * FROM material_declarations WHERE batch_id = ? "
        "ORDER BY version DESC LIMIT 1",
        (batch_id,),
    ).fetchone()


# ---------------------------------------------------------------- 原料登记

def create_material(conn: sqlite3.Connection, data) -> dict:
    declared_at = to_store(data.declared_at) if data.declared_at else to_store(now_utc())
    with write_tx(conn):
        kind, _ = batch_kind(conn, data.batch_id)
        if kind:
            raise Conflict(f"批次 {data.batch_id} 已存在（{kind}），不能重复登记")
        conn.execute(
            "INSERT INTO material_batches (batch_id, created_at) VALUES (?, ?)",
            (data.batch_id, declared_at),
        )
        conn.execute(
            "INSERT INTO material_declarations (batch_id, version, allergens, pending, declared_at)"
            " VALUES (?, 1, ?, ?, ?)",
            (data.batch_id, json.dumps(data.allergens), int(data.pending), declared_at),
        )
    return get_material(conn, data.batch_id)


def add_declaration(conn: sqlite3.Connection, batch_id: str, data) -> dict:
    declared_at = to_store(data.declared_at) if data.declared_at else to_store(now_utc())
    with write_tx(conn):
        if not _material(conn, batch_id):
            raise NotFound(f"未知原料批次: {batch_id}")
        cur = _current_declaration(conn, batch_id)
        conn.execute(
            "INSERT INTO material_declarations (batch_id, version, allergens, pending, declared_at)"
            " VALUES (?, ?, ?, ?, ?)",
            (batch_id, cur["version"] + 1, json.dumps(data.allergens),
             int(data.pending), declared_at),
        )
    return get_material(conn, batch_id)


def get_material(conn: sqlite3.Connection, batch_id: str) -> dict:
    m = _material(conn, batch_id)
    if not m:
        raise NotFound(f"未知原料批次: {batch_id}")
    decl = _current_declaration(conn, batch_id)
    history = conn.execute(
        "SELECT COUNT(*) AS n FROM material_declarations WHERE batch_id = ?", (batch_id,)
    ).fetchone()["n"]
    return {
        "batch_id": batch_id,
        "kind": "material",
        "allergens": json.loads(decl["allergens"]),
        "pending": bool(decl["pending"]),
        "version": decl["version"],
        "declared_at": iso_z(decl["declared_at"]),
        "declaration_versions": history,
    }


def list_materials(conn: sqlite3.Connection) -> list[dict]:
    rows = conn.execute("SELECT batch_id FROM material_batches ORDER BY batch_id").fetchall()
    return [get_material(conn, r["batch_id"]) for r in rows]


# ---------------------------------------------------------------- 加工登记

def create_processing(conn: sqlite3.Connection, data) -> dict:
    start = to_store(data.start_utc)
    end = to_store(data.end_utc)
    with write_tx(conn):
        # 1) 时序有效
        if not data.start_utc < data.end_utc:
            raise BadRequest("无效时序：end_utc 必须晚于 start_utc")
        if conn.execute(
            "SELECT 1 FROM processings WHERE processing_id = ?", (data.processing_id,)
        ).fetchone():
            raise Conflict(f"加工批次 {data.processing_id} 已存在")

        # 2) 来源环：产出不得同时是投入（自环）；并做通用可达性检查
        loop = set(data.inputs) & set(data.outputs)
        if loop:
            raise Conflict(f"批次来源成环：{sorted(loop)} 既是投入又是产出")
        _check_no_cycle(conn, data.inputs, data.outputs)

        # 3) 产出批次必须是全新批次（一个批次只能有一个来源）
        for out_id in data.outputs:
            kind, _ = batch_kind(conn, out_id)
            if kind:
                raise Conflict(f"产出批次 {out_id} 已存在（{kind}），来源冲突")

        # 4) 投入必须存在且在加工开始前可用
        for in_id in data.inputs:
            try:
                avail = _available_at(conn, in_id)
            except NotFound:
                raise BadRequest(f"投入批次 {in_id} 不存在")
            if avail > data.start_utc:
                raise BadRequest(
                    f"投入批次 {in_id} 在加工开始前不可用"
                    f"（可用时间 {iso_z(to_store(avail))} 晚于开始时间 {iso_z(start)}）"
                )

        # 5) 同设备加工不得重叠
        for row in conn.execute(
            "SELECT processing_id, start_utc, end_utc FROM processings WHERE equipment_id = ?",
            (data.equipment_id,),
        ):
            if start < row["end_utc"] and row["start_utc"] < end:
                raise Conflict(
                    f"设备 {data.equipment_id} 在 [{iso_z(row['start_utc'])}, {iso_z(row['end_utc'])}) "
                    f"已有加工 {row['processing_id']}，时间重叠"
                )

        conn.execute(
            "INSERT INTO processings (processing_id, equipment_id, start_utc, end_utc)"
            " VALUES (?, ?, ?, ?)",
            (data.processing_id, data.equipment_id, start, end),
        )
        for in_id in data.inputs:
            conn.execute(
                "INSERT INTO processing_inputs (processing_id, batch_id) VALUES (?, ?)",
                (data.processing_id, in_id),
            )
        for out_id in data.outputs:
            conn.execute(
                "INSERT INTO processing_outputs (processing_id, batch_id) VALUES (?, ?)",
                (data.processing_id, out_id),
            )
    return get_processing(conn, data.processing_id)


def _check_no_cycle(conn, inputs: list[str], outputs: list[str]) -> None:
    """通用成环检查：在现有“投入->产出”批次图上，若任一新产出可到达任一投入，
    则新增边会成环，拒绝。（产出为全新批次时该检查恒通过，作为防御保留。）"""
    adj: dict[str, list[str]] = {}
    for p in conn.execute("SELECT processing_id FROM processings"):
        pid = p["processing_id"]
        ins = [r["batch_id"] for r in conn.execute(
            "SELECT batch_id FROM processing_inputs WHERE processing_id = ?", (pid,))]
        outs = [r["batch_id"] for r in conn.execute(
            "SELECT batch_id FROM processing_outputs WHERE processing_id = ?", (pid,))]
        for i in ins:
            adj.setdefault(i, []).extend(outs)
    targets = set(inputs)
    for o in outputs:
        stack, seen = [o], set()
        while stack:
            node = stack.pop()
            if node in targets:
                raise Conflict(f"批次来源成环：产出 {o} 是投入 {node} 的上游")
            if node in seen:
                continue
            seen.add(node)
            stack.extend(adj.get(node, []))


def get_processing(conn: sqlite3.Connection, processing_id: str) -> dict:
    proc = conn.execute(
        "SELECT * FROM processings WHERE processing_id = ?", (processing_id,)
    ).fetchone()
    if not proc:
        raise NotFound(f"未知加工批次: {processing_id}")
    ins = [r["batch_id"] for r in conn.execute(
        "SELECT batch_id FROM processing_inputs WHERE processing_id = ? ORDER BY batch_id",
        (processing_id,))]
    outs = [r["batch_id"] for r in conn.execute(
        "SELECT batch_id FROM processing_outputs WHERE processing_id = ? ORDER BY batch_id",
        (processing_id,))]
    return {
        "processing_id": proc["processing_id"],
        "equipment_id": proc["equipment_id"],
        "start_utc": iso_z(proc["start_utc"]),
        "end_utc": iso_z(proc["end_utc"]),
        "inputs": ins,
        "outputs": outs,
    }


def list_processings(conn: sqlite3.Connection) -> list[dict]:
    rows = conn.execute("SELECT processing_id FROM processings ORDER BY start_utc").fetchall()
    return [get_processing(conn, r["processing_id"]) for r in rows]


# ---------------------------------------------------------------- 清洗

def create_cleaning(conn: sqlite3.Connection, data) -> dict:
    with write_tx(conn):
        cur = conn.execute(
            "INSERT INTO cleanings (equipment_id, cleaned_at, verified, revoked)"
            " VALUES (?, ?, ?, 0)",
            (data.equipment_id, to_store(data.cleaned_at), int(data.verified)),
        )
        cid = cur.lastrowid
    return get_cleaning(conn, cid)


def get_cleaning(conn: sqlite3.Connection, cleaning_id: int) -> dict:
    row = conn.execute("SELECT * FROM cleanings WHERE id = ?", (cleaning_id,)).fetchone()
    if not row:
        raise NotFound(f"未知清洗记录: {cleaning_id}")
    return {
        "id": row["id"],
        "equipment_id": row["equipment_id"],
        "cleaned_at": iso_z(row["cleaned_at"]),
        "verified": bool(row["verified"]),
        "revoked": bool(row["revoked"]),
        "effective": bool(row["verified"] and not row["revoked"]),
    }


def list_cleanings(conn: sqlite3.Connection, equipment_id: str | None = None) -> list[dict]:
    if equipment_id:
        rows = conn.execute(
            "SELECT id FROM cleanings WHERE equipment_id = ? ORDER BY cleaned_at",
            (equipment_id,)).fetchall()
    else:
        rows = conn.execute("SELECT id FROM cleanings ORDER BY cleaned_at").fetchall()
    return [get_cleaning(conn, r["id"]) for r in rows]


def revoke_cleaning(conn: sqlite3.Connection, cleaning_id: int) -> dict:
    with write_tx(conn):
        row = conn.execute("SELECT * FROM cleanings WHERE id = ?", (cleaning_id,)).fetchone()
        if not row:
            raise NotFound(f"未知清洗记录: {cleaning_id}")
        if row["revoked"]:
            raise Conflict(f"清洗记录 {cleaning_id} 已撤销")
        conn.execute("UPDATE cleanings SET revoked = 1 WHERE id = ?", (cleaning_id,))
    return get_cleaning(conn, cleaning_id)


# ---------------------------------------------------------------- 溯源计算

def compute_trace(conn: sqlite3.Connection, batch_id: str):
    """按当前有效记录实时计算批次结论。

    返回 (result, evidence)：
    - result: {"allergens": {过敏原: [路径...]}, "pending_paths": [路径...]}
    - evidence: 计算依据（命中的原料声明版本、赖以清除残留的清洗记录）
    路径 = 步骤列表，步骤形如：
      {"batch_id": "RM-1", "via": "declaration"}
      {"batch_id": "OUT-1", "processing_id": "P1", "via": "input"}
      {"batch_id": "OUT-2", "processing_id": "P2", "via": "equipment_residue", "equipment_id": "EQ-1"}
    """
    evidence = {"materials": {}, "cleanings": {}}
    memo: dict[str, dict] = {}

    def trace(bid: str) -> dict:
        if bid in memo:
            return memo[bid]
        if _material(conn, bid):
            decl = _current_declaration(conn, bid)
            allergens = json.loads(decl["allergens"])
            evidence["materials"][bid] = {
                "batch_id": bid,
                "version": decl["version"],
                "allergens": allergens,
                "pending": bool(decl["pending"]),
                "declared_at": iso_z(decl["declared_at"]),
            }
            step = {"batch_id": bid, "via": "declaration"}
            res = {
                "allergens": {a: [[step]] for a in allergens},
                "pending_paths": [[step]] if decl["pending"] else [],
            }
        else:
            out = _output_of(conn, bid)
            if not out:
                raise NotFound(f"未知批次: {bid}")
            pid = out["processing_id"]
            proc = conn.execute(
                "SELECT * FROM processings WHERE processing_id = ?", (pid,)
            ).fetchone()
            res = {"allergens": {}, "pending_paths": []}

            def absorb(sub: dict, step: dict):
                for a, paths in sub["allergens"].items():
                    res["allergens"].setdefault(a, []).extend(
                        [p + [step] for p in paths])
                res["pending_paths"].extend([p + [step] for p in sub["pending_paths"]])

            # 1) 投入继承
            for r in conn.execute(
                "SELECT batch_id FROM processing_inputs WHERE processing_id = ?", (pid,)
            ):
                absorb(trace(r["batch_id"]),
                       {"batch_id": bid, "processing_id": pid, "via": "input"})

            # 2) 设备残留：上一次同设备加工，除非两次加工间存在有效清洗
            prev = conn.execute(
                "SELECT * FROM processings WHERE equipment_id = ? AND end_utc <= ?"
                " ORDER BY end_utc DESC LIMIT 1",
                (proc["equipment_id"], proc["start_utc"]),
            ).fetchone()
            if prev:
                cleaning = conn.execute(
                    "SELECT * FROM cleanings WHERE equipment_id = ?"
                    " AND verified = 1 AND revoked = 0"
                    " AND cleaned_at >= ? AND cleaned_at <= ?"
                    " ORDER BY cleaned_at LIMIT 1",
                    (proc["equipment_id"], prev["end_utc"], proc["start_utc"]),
                ).fetchone()
                if cleaning:
                    evidence["cleanings"][cleaning["id"]] = {
                        "id": cleaning["id"],
                        "equipment_id": cleaning["equipment_id"],
                        "cleaned_at": iso_z(cleaning["cleaned_at"]),
                        "verified": True,
                    }
                else:
                    for r in conn.execute(
                        "SELECT batch_id FROM processing_outputs WHERE processing_id = ?",
                        (prev["processing_id"],),
                    ):
                        absorb(trace(r["batch_id"]), {
                            "batch_id": bid,
                            "processing_id": pid,
                            "via": "equipment_residue",
                            "equipment_id": proc["equipment_id"],
                        })
        memo[bid] = res
        return res

    result = trace(batch_id)
    return result, evidence


def trace_response(conn: sqlite3.Connection, batch_id: str) -> dict:
    kind, _ = batch_kind(conn, batch_id)
    if not kind:
        raise NotFound(f"未知批次: {batch_id}")
    result, evidence = compute_trace(conn, batch_id)
    return {
        "batch_id": batch_id,
        "kind": kind,
        "pending": bool(result["pending_paths"]),
        "allergens": sorted(result["allergens"].keys()),
        "allergen_paths": {a: result["allergens"][a] for a in sorted(result["allergens"])},
        "pending_paths": result["pending_paths"],
        "basis": {
            "material_declarations": sorted(
                evidence["materials"].values(), key=lambda d: d["batch_id"]),
            "cleanings": sorted(
                evidence["cleanings"].values(), key=lambda d: d["id"]),
        },
    }


# ---------------------------------------------------------------- 标签签发

def issue_label(conn: sqlite3.Connection, batch_id: str, supersedes_id: int | None) -> dict:
    with write_tx(conn):
        kind, _ = batch_kind(conn, batch_id)
        if not kind:
            raise NotFound(f"未知批次: {batch_id}")
        if supersedes_id is not None:
            old = conn.execute(
                "SELECT * FROM labels WHERE id = ?", (supersedes_id,)).fetchone()
            if not old:
                raise NotFound(f"被关联的旧标签不存在: {supersedes_id}")
            if old["batch_id"] != batch_id:
                raise BadRequest(
                    f"旧标签 {supersedes_id} 属于批次 {old['batch_id']}，"
                    f"不能关联到批次 {batch_id} 的新标签")
        # 签发时刻冻结结论与依据
        result, evidence = compute_trace(conn, batch_id)
        snapshot = {
            "batch_id": batch_id,
            "conclusion": {
                "pending": bool(result["pending_paths"]),
                "allergens": sorted(result["allergens"].keys()),
                "allergen_paths": {a: result["allergens"][a]
                                   for a in sorted(result["allergens"])},
                "pending_paths": result["pending_paths"],
            },
            "basis": {
                "material_declarations": sorted(
                    evidence["materials"].values(), key=lambda d: d["batch_id"]),
                "cleanings": sorted(
                    evidence["cleanings"].values(), key=lambda d: d["id"]),
            },
        }
        cur = conn.execute(
            "INSERT INTO labels (batch_id, issued_at, supersedes_id, snapshot)"
            " VALUES (?, ?, ?, ?)",
            (batch_id, to_store(now_utc()), supersedes_id, json.dumps(snapshot)),
        )
        label_id = cur.lastrowid
    return get_label(conn, label_id)


def get_label(conn: sqlite3.Connection, label_id: int) -> dict:
    row = conn.execute("SELECT * FROM labels WHERE id = ?", (label_id,)).fetchone()
    if not row:
        raise NotFound(f"未知标签: {label_id}")
    snap = json.loads(row["snapshot"])
    return {
        "label_id": row["id"],
        "batch_id": row["batch_id"],
        "issued_at": iso_z(row["issued_at"]),
        "supersedes_id": row["supersedes_id"],
        "conclusion": snap["conclusion"],
        "basis": snap["basis"],
    }


def list_labels(conn: sqlite3.Connection, batch_id: str) -> list[dict]:
    kind, _ = batch_kind(conn, batch_id)
    if not kind:
        raise NotFound(f"未知批次: {batch_id}")
    rows = conn.execute(
        "SELECT id FROM labels WHERE batch_id = ? ORDER BY id", (batch_id,)).fetchall()
    return [get_label(conn, r["id"]) for r in rows]
