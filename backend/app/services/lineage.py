"""数据血缘业务规则：草稿-发布快照、整组校验、并发裁决、影响分析与历史自由文本兼容。

核心约定：
- 血缘关系是草稿态数据，可任意修订；只有「发布」才会把当前整组关系固化成生效快照。
- 发布按整组结果校验：自环、跨多条关系形成的环、缺失模型字段都会让整组失败，
  失败时旧快照继续生效，不产生中间态。
- 发布与模型字段变更都携带基准版本，只接受基于当前生效版本的修改，
  裁决结果里明确写出各自依据的版本。
- 历史自由文本节点（未绑定模型）照常展示、按名字精确串联，但不参与字段级影响分析，
  无法解析的旧关系明确标出，绝不伪造到模型的连接。
"""
from __future__ import annotations

import threading
from collections import deque
from copy import deepcopy
from datetime import datetime
from typing import Any

from app.store import store

MODULE = "lineage"
MODEL_MODULE = "datamodel"
SNAPSHOT_TABLE = "lineage_snapshots"
IMPACT_TABLE = "lineage_impacts"

REQUIRED_FIELDS = ["关系编号", "上游节点", "下游节点"]
STATUS_ORDER = ["草稿", "待校验", "已启用", "已停用"]
ACTION_RULES = {"提交校验": "待校验", "启用关系": "已启用", "停用关系": "已停用"}
NEGATIVE_ACTIONS = ["停用关系"]

# 参与「是否被修订」比对的关系内容字段
CONTENT_FIELDS = ["上游节点", "下游节点", "来源字段", "目标字段", "转换函数", "上游模型ID", "下游模型ID"]

# 影响分析的路径数上限：菱形依赖会组合出大量路径，计数封顶避免膨胀
PATH_CAP = 999
# 每个受影响节点最多保留的示例路径条数
SAMPLE_CAP = 3

# 发布与模型字段变更共用同一把锁：两类写操作必须串行，裁决才有意义
GRAPH_LOCK = threading.RLock()


def _now() -> str:
    return datetime.now().isoformat(timespec="seconds")


def _text(value: Any) -> str:
    return str(value or "").strip()


def model_by_id(model_id: Any) -> dict[str, Any] | None:
    try:
        mid = int(model_id)
    except (TypeError, ValueError):
        return None
    return store.find(MODEL_MODULE, mid)


def endpoint_of(row: dict[str, Any], side: str) -> dict[str, Any]:
    """解析关系的一端：绑定了模型就是模型端点，否则按自由文本端点处理。"""
    field = _text(row.get("来源字段" if side == "上游" else "目标字段"))
    model_id = row.get(f"{side}模型ID")
    if model_id not in (None, "", 0):
        model = model_by_id(model_id)
        mid = int(model_id)
        return {
            "kind": "model",
            "key": f"model:{mid}",
            "model_id": mid,
            "label": _text(model.get("模型名称")) if model else f"模型{mid}",
            "field": field,
            "model": model,
        }
    name = _text(row.get(f"{side}节点"))
    return {
        "kind": "text",
        "key": f"text:{name}",
        "model_id": None,
        "label": name or "未命名节点",
        "field": field,
        "model": None,
    }


def _model_field_names(model: dict[str, Any]) -> list[str]:
    return [_text(field.get("name")) for field in model.get("fields") or []]


def relation_problems(row: dict[str, Any]) -> list[dict[str, Any]]:
    """单条关系的结构问题：自环、模型缺失/已废弃、模型字段缺失。"""
    problems: list[dict[str, Any]] = []
    up = endpoint_of(row, "上游")
    down = endpoint_of(row, "下游")
    code = _text(row.get("关系编号"))
    if up["key"] == down["key"]:
        problems.append({
            "type": "自环",
            "message": f"关系 {code} 的上游与下游指向同一节点「{up['label']}」，构成自环",
            "node_keys": [up["key"]],
        })
    for side, ep in (("上游", up), ("下游", down)):
        if ep["kind"] != "model":
            continue
        model = ep["model"]
        if model is None:
            problems.append({
                "type": "模型缺失",
                "message": f"关系 {code} 的{side}绑定了不存在的模型（ID={ep['model_id']}）",
                "node_keys": [ep["key"]],
            })
            continue
        if model.get("status") == "已废弃":
            problems.append({
                "type": "模型已废弃",
                "message": f"关系 {code} 的{side}绑定的模型「{ep['label']}」已废弃",
                "node_keys": [ep["key"]],
            })
            continue
        if not ep["field"]:
            problems.append({
                "type": "字段缺失",
                "message": f"关系 {code} 的{side}未填写字段（模型「{ep['label']}」）",
                "node_keys": [ep["key"]],
            })
        elif ep["field"] not in _model_field_names(model):
            problems.append({
                "type": "字段缺失",
                "message": f"关系 {code} 的{side}字段「{ep['field']}」在模型「{ep['label']}」v{model.get('version', 1)} 中不存在",
                "node_keys": [ep["key"]],
            })
    return problems


def find_cycle(rows: list[dict[str, Any]]) -> dict[str, Any] | None:
    """在节点级有向图上找环：跨多条关系形成的环不允许发布。返回环上的节点与关系。"""
    adjacency: dict[str, list[tuple[str, str]]] = {}
    labels: dict[str, str] = {}
    for row in rows:
        up = endpoint_of(row, "上游")
        down = endpoint_of(row, "下游")
        labels[up["key"]] = up["label"]
        labels[down["key"]] = down["label"]
        adjacency.setdefault(up["key"], []).append((down["key"], _text(row.get("关系编号"))))

    color: dict[str, int] = {}  # 0=未访问 1=在栈上 2=已完成
    stack_nodes: list[str] = []
    stack_edges: list[str] = []

    def dfs(node: str) -> dict[str, Any] | None:
        color[node] = 1
        stack_nodes.append(node)
        for nxt, code in adjacency.get(node, []):
            if color.get(nxt, 0) == 0:
                stack_edges.append(code)
                found = dfs(nxt)
                if found:
                    return found
                stack_edges.pop()
            elif color.get(nxt) == 1:
                idx = stack_nodes.index(nxt)
                cycle_nodes = stack_nodes[idx:] + [nxt]
                # stack_edges[i] 是 stack_nodes[i] → stack_nodes[i+1] 的关系编号，
                # 再加上当前这条回边 code 就闭合成环
                cycle_edges = stack_edges[idx:] + [code]
                return {
                    "nodes": [labels.get(key, key) for key in cycle_nodes],
                    "node_keys": cycle_nodes,
                    "relations": cycle_edges,
                }
        stack_nodes.pop()
        color[node] = 2
        return None

    for key in list(adjacency):
        if color.get(key, 0) == 0:
            found = dfs(key)
            if found:
                return found
    return None


def validate_relation_set(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """整组校验：对最终关系集合整体判断，与关系的提交顺序无关。"""
    conflicts: list[dict[str, Any]] = []
    for row in rows:
        for problem in relation_problems(row):
            conflicts.append({
                **problem,
                "relation": _text(row.get("关系编号")),
                "relation_id": row.get("id"),
            })
    cycle = find_cycle(rows)
    if cycle:
        conflicts.append({
            "type": "环路",
            "message": f"关系 {'、'.join(cycle['relations'])} 跨多条关系形成环：{' → '.join(cycle['nodes'])}",
            "relation": None,
            "relation_id": None,
            "path": cycle["nodes"],
            "node_keys": cycle["node_keys"],
            "relations": cycle["relations"],
        })
    return conflicts


def _snapshot_rows() -> list[dict[str, Any]]:
    return store.aux(SNAPSHOT_TABLE)


def latest_snapshot() -> dict[str, Any] | None:
    snapshots = _snapshot_rows()
    return snapshots[-1] if snapshots else None


def _snapshot_relation(row: dict[str, Any]) -> dict[str, Any]:
    """发布时固化的关系内容：带上解析结果，之后模型改名也不影响当时快照。"""
    up = endpoint_of(row, "上游")
    down = endpoint_of(row, "下游")
    return {
        "id": row.get("id"),
        "关系编号": _text(row.get("关系编号")),
        "上游节点": up["label"],
        "下游节点": down["label"],
        "来源字段": up["field"],
        "目标字段": down["field"],
        "转换函数": _text(row.get("转换函数")),
        "上游模型ID": up["model_id"],
        "下游模型ID": down["model_id"],
        "上游key": up["key"],
        "下游key": down["key"],
        "绑定形态": _binding_form(up, down),
    }


def _binding_form(up: dict[str, Any], down: dict[str, Any]) -> str:
    kinds = {up["kind"], down["kind"]}
    if kinds == {"model"}:
        return "已绑定模型"
    if kinds == {"text"}:
        return "自由文本"
    return "半绑定"


def _included_rows() -> list[dict[str, Any]]:
    """纳入下一次发布的关系：未停用的全部关系，整组参与校验。"""
    return [row for row in store.rows(MODULE) if row.get("status") != "已停用"]


def _referenced_model_ids(rows: list[dict[str, Any]]) -> set[int]:
    ids: set[int] = set()
    for row in rows:
        for side in ("上游", "下游"):
            ep = endpoint_of(row, side)
            if ep["kind"] == "model" and ep["model_id"] is not None:
                ids.add(ep["model_id"])
    return ids


def _effective_status(row: dict[str, Any], snapshot: dict[str, Any] | None) -> str:
    if snapshot is None:
        return "待发布"
    in_snapshot = None
    for rel in snapshot["relations"]:
        if rel["id"] == row.get("id"):
            in_snapshot = rel
            break
    if row.get("status") == "已停用":
        return "停用待生效" if in_snapshot else "已停用"
    if in_snapshot is None:
        return "待发布"
    current = _snapshot_relation(row)
    same = all(current.get(field) == in_snapshot.get(field) for field in
               ["上游节点", "下游节点", "来源字段", "目标字段", "转换函数", "上游模型ID", "下游模型ID"])
    return "生效中" if same else "已修订待生效"


def enrich_row(row: dict[str, Any], snapshot: dict[str, Any] | None = None) -> dict[str, Any]:
    if snapshot is None:
        snapshot = latest_snapshot()
    enriched = dict(row)
    up = endpoint_of(row, "上游")
    down = endpoint_of(row, "下游")
    enriched["绑定状态"] = _binding_form(up, down)
    enriched["生效状态"] = _effective_status(row, snapshot)
    enriched["问题"] = [p["type"] for p in relation_problems(row)] if row.get("status") != "已停用" else []
    enriched["上游节点key"] = up["key"]
    enriched["下游节点key"] = down["key"]
    return enriched


class LineageService:
    def __init__(self) -> None:
        self._bootstrap()

    # ------------------------------------------------------------------
    # 初始化：给模型补版本、给关系补绑定位，并把初始关系固化为快照 v1
    # ------------------------------------------------------------------
    def _bootstrap(self) -> None:
        for model in store.rows(MODEL_MODULE):
            model.setdefault("fields", [])
            model.setdefault("version", 1)
            history = model.setdefault("版本历史", [])
            if not history:
                history.append({
                    "version": model["version"],
                    "time": "2026-09-01 10:00:00",
                    "remark": "初始版本",
                    "fields": deepcopy(model["fields"]),
                })
        for row in store.rows(MODULE):
            row.setdefault("上游模型ID", None)
            row.setdefault("下游模型ID", None)
        if not _snapshot_rows():
            included = _included_rows()
            conflicts = validate_relation_set(included)
            if conflicts:
                raise RuntimeError(f"种子数据无法通过血缘整组校验：{conflicts}")
            _snapshot_rows().append({
                "version": 1,
                "created_at": _now(),
                "remark": "初始生效快照",
                "base_version": 0,
                "model_versions": self._current_model_versions(_referenced_model_ids(included)),
                "relations": [_snapshot_relation(row) for row in included],
            })

    @staticmethod
    def _current_model_versions(model_ids: set[int]) -> dict[str, int]:
        versions: dict[str, int] = {}
        for mid in sorted(model_ids):
            model = model_by_id(mid)
            if model is not None:
                versions[str(mid)] = int(model.get("version", 1))
        return versions

    # ------------------------------------------------------------------
    # 列表 / 明细 / 登记 / 修订 / 动作
    # ------------------------------------------------------------------
    def list_entries(
        self,
        *,
        keyword: str | None = None,
        status: str | None = None,
        page: int = 1,
        size: int = 20,
    ) -> tuple[list[dict[str, Any]], int]:
        rows = store.rows(MODULE)
        if keyword:
            rows = [row for row in rows if keyword in _text(row.get("关系编号"))]
        if status:
            rows = [row for row in rows if row.get("status") == status]
        total = len(rows)
        start = max(page - 1, 0) * size
        snapshot = latest_snapshot()
        return [enrich_row(row, snapshot) for row in rows[start:start + size]], total

    def get_entry(self, entry_id: int) -> dict[str, Any] | None:
        row = store.find(MODULE, entry_id)
        return enrich_row(row) if row else None

    def _next_code(self) -> str:
        codes = [_text(row.get("关系编号")) for row in store.rows(MODULE)]
        serial = 0
        for code in codes:
            if code.startswith("LINE-"):
                try:
                    serial = max(serial, int(code.split("-", 1)[1]))
                except ValueError:
                    continue
        return f"LINE-{serial + 1:04d}"

    def _apply_values(self, row: dict[str, Any], values: dict[str, Any]) -> None:
        for field in ["关系编号", "来源字段", "目标字段", "转换函数", "采集批次", "关系版本"]:
            if field in values:
                row[field] = _text(values.get(field))
        for side in ("上游", "下游"):
            model_key = f"{side}模型ID"
            node_key = f"{side}节点"
            if model_key in values:
                raw = values.get(model_key)
                if raw in (None, "", 0):
                    row[model_key] = None
                    if node_key in values:
                        row[node_key] = _text(values.get(node_key))
                else:
                    model = model_by_id(raw)
                    row[model_key] = int(raw)
                    # 绑定模型后，节点展示名跟随模型，不再使用自由文本
                    row[node_key] = _text(model.get("模型名称")) if model else _text(values.get(node_key))
            elif node_key in values:
                row[node_key] = _text(values.get(node_key))

    def _missing_required(self, merged: dict[str, Any]) -> list[str]:
        missing = [field for field in REQUIRED_FIELDS if not _text(merged.get(field))]
        # 绑定了模型的端点允许节点名为空：展示名由模型补齐
        for side in ("上游", "下游"):
            if merged.get(f"{side}模型ID") not in (None, "", 0):
                missing = [field for field in missing if field != f"{side}节点"]
        return missing

    def create_entry(self, values: dict[str, Any]) -> tuple[dict[str, Any] | None, list[str]]:
        merged = dict(values)
        if not _text(merged.get("关系编号")):
            merged["关系编号"] = self._next_code()
        missing = self._missing_required(merged)
        if missing:
            return None, missing
        rows = store.rows(MODULE)
        entry: dict[str, Any] = {"id": max((int(row.get("id", 0)) for row in rows), default=0) + 1}
        entry["上游模型ID"] = None
        entry["下游模型ID"] = None
        self._apply_values(entry, merged)
        entry["status"] = STATUS_ORDER[0]
        entry["pending"] = True
        entry["abnormal"] = False
        rows.append(entry)
        return enrich_row(entry), []

    def revise_entry(self, entry_id: int, values: dict[str, Any]) -> tuple[dict[str, Any] | None, str]:
        row = store.find(MODULE, entry_id)
        if row is None:
            return None, f"血缘关系 {entry_id} 不存在或已归档"
        self._apply_values(row, values)
        if row.get("status") == "已启用":
            row["status"] = "待校验"
            row["pending"] = True
        return enrich_row(row), f"血缘关系 {row.get('关系编号')} 已修订，发布后生效"

    def apply_revisions(self, items: list[dict[str, Any]]) -> tuple[bool, str, list[dict[str, Any]]]:
        """整组替换：一批新增/修订/停用一次性落为草稿，发布时按整组结果校验。

        先全部校验再统一落库，批内顺序不影响结果，也不会留下应用了一半的中间态。
        """
        with GRAPH_LOCK:
            staged: list[tuple[dict[str, Any] | None, dict[str, Any], bool]] = []
            for item in items:
                entry_id = item.get("id")
                values = item.get("values") or {}
                disable = bool(item.get("disable"))
                if entry_id is not None:
                    row = store.find(MODULE, int(entry_id))
                    if row is None:
                        return False, f"整组替换失败：血缘关系 {entry_id} 不存在，未做任何修改", []
                    staged.append((row, values, disable))
                else:
                    merged = dict(values)
                    if not _text(merged.get("关系编号")):
                        merged["关系编号"] = self._next_code()
                    missing = self._missing_required(merged)
                    if missing:
                        return False, f"整组替换失败：缺少必填字段 {'、'.join(missing)}，未做任何修改", []
                    staged.append((None, merged, disable))
            applied: list[dict[str, Any]] = []
            for row, values, disable in staged:
                if row is None:
                    entry, missing = self.create_entry(values)
                    if entry is None or missing:
                        return False, "整组替换失败：新增关系写入异常，请重试", []
                    row = store.find(MODULE, int(entry["id"]))
                else:
                    self._apply_values(row, values)
                if disable and row is not None:
                    row["status"] = "已停用"
                    row["pending"] = False
                if row is not None:
                    applied.append(enrich_row(row))
            return True, f"整组替换已写入草稿 {len(applied)} 条，发布后生效", applied

    def delete_entry(self, entry_id: int) -> tuple[bool, str]:
        row = store.find(MODULE, entry_id)
        if row is None:
            return False, f"血缘关系 {entry_id} 不存在或已归档"
        snapshot = latest_snapshot()
        if snapshot and any(rel["id"] == entry_id for rel in snapshot["relations"]):
            return False, "该关系仍在生效快照中，请先停用并发布，再删除草稿"
        store.rows(MODULE).remove(row)
        return True, f"血缘关系 {row.get('关系编号')} 已删除"

    def run_action(self, entry_id: int, action: str) -> tuple[dict[str, Any] | None, str]:
        entry = store.find(MODULE, entry_id)
        if entry is None:
            return None, f"血缘关系 {entry_id} 不存在或已归档"
        if action not in ACTION_RULES:
            return None, f"动作「{action}」不属于数据血缘可执行范围"
        target = ACTION_RULES[action]
        if target not in STATUS_ORDER:
            return None, f"目标状态「{target}」不在允许的状态序列里"
        entry["status"] = target
        entry["pending"] = target != STATUS_ORDER[-1]
        entry["abnormal"] = action in NEGATIVE_ACTIONS
        suffix = "，发布后生效图才会更新" if action in ("启用关系", "停用关系") else ""
        return enrich_row(entry), f"血缘关系已{action}{suffix}"

    # ------------------------------------------------------------------
    # 发布：整组校验 + 基准版本裁决 + 原子换快照
    # ------------------------------------------------------------------
    def publish(self, base_snapshot_version: int, base_model_versions: dict[str, Any], remark: str | None) -> dict[str, Any]:
        with GRAPH_LOCK:
            snapshot = latest_snapshot()
            current_version = int(snapshot["version"]) if snapshot else 0
            if base_snapshot_version != current_version:
                return {
                    "ok": False,
                    "stage": "版本冲突",
                    "message": f"当前生效快照为 v{current_version}，本次发布基于 v{base_snapshot_version}，请刷新草稿后重试",
                    "conflicts": [],
                    "current": {"snapshot_version": current_version},
                }
            included = _included_rows()
            referenced = _referenced_model_ids(included)
            model_conflicts: list[dict[str, Any]] = []
            for mid in sorted(referenced):
                model = model_by_id(mid)
                if model is None:
                    continue
                current_model_version = int(model.get("version", 1))
                raw_base = base_model_versions.get(str(mid))
                if raw_base is None:
                    model_conflicts.append({
                        "type": "模型版本未知",
                        "message": f"草稿未记录模型「{model.get('模型名称')}」的基准版本，当前为 v{current_model_version}",
                        "model_id": mid,
                        "current_version": current_model_version,
                        "base_version": None,
                    })
                elif int(raw_base) != current_model_version:
                    model_conflicts.append({
                        "type": "模型版本冲突",
                        "message": f"模型「{model.get('模型名称')}」当前为 v{current_model_version}，草稿基于 v{raw_base}",
                        "model_id": mid,
                        "current_version": current_model_version,
                        "base_version": int(raw_base),
                    })
            if model_conflicts:
                return {
                    "ok": False,
                    "stage": "模型版本冲突",
                    "message": "血缘草稿基于的模型版本已过期，请以当前生效版本为基准重新确认",
                    "conflicts": model_conflicts,
                    "current": {
                        "snapshot_version": current_version,
                        "model_versions": self._current_model_versions(referenced),
                    },
                }
            conflicts = validate_relation_set(included)
            if conflicts:
                return {
                    "ok": False,
                    "stage": "校验失败",
                    "message": f"整组校验未通过（{len(conflicts)} 处冲突），生效图保持 v{current_version} 不变",
                    "conflicts": conflicts,
                    "current": {"snapshot_version": current_version},
                }
            new_snapshot = {
                "version": current_version + 1,
                "created_at": _now(),
                "remark": remark or "",
                "base_version": current_version,
                "model_versions": self._current_model_versions(referenced),
                "relations": [_snapshot_relation(row) for row in included],
            }
            _snapshot_rows().append(new_snapshot)
            for row in included:
                row["status"] = "已启用"
                row["pending"] = False
                row["abnormal"] = False
            return {
                "ok": True,
                "stage": "已发布",
                "message": f"快照 v{new_snapshot['version']} 已生效，纳入关系 {len(included)} 条",
                "conflicts": [],
                "snapshot": {key: new_snapshot[key] for key in ("version", "created_at", "remark", "model_versions")},
                "current": {"snapshot_version": new_snapshot["version"]},
            }

    # ------------------------------------------------------------------
    # 快照查询
    # ------------------------------------------------------------------
    def list_snapshots(self) -> list[dict[str, Any]]:
        return [
            {
                "version": snap["version"],
                "created_at": snap["created_at"],
                "remark": snap.get("remark", ""),
                "base_version": snap.get("base_version", 0),
                "relation_count": len(snap["relations"]),
                "model_versions": snap.get("model_versions", {}),
            }
            for snap in reversed(_snapshot_rows())
        ]

    def get_snapshot(self, version: int) -> dict[str, Any] | None:
        for snap in _snapshot_rows():
            if int(snap["version"]) == version:
                return snap
        return None

    # ------------------------------------------------------------------
    # 图构建：生效快照 / 草稿（含校验问题）
    # ------------------------------------------------------------------
    def build_graph(self, scope: str) -> dict[str, Any]:
        snapshot = latest_snapshot()
        if scope == "effective" and snapshot is not None:
            relations = [dict(rel) for rel in snapshot["relations"]]
            for rel in relations:
                rel["status"] = "已启用"
                rel["problems"] = []
            conflicts: list[dict[str, Any]] = []
            graph_version: int | None = snapshot["version"]
        else:
            scope = "draft"
            rows = store.rows(MODULE)
            relations = []
            for row in rows:
                rel = _snapshot_relation(row)
                rel["status"] = row.get("status", "草稿")
                rel["problems"] = [p["type"] for p in relation_problems(row)] if row.get("status") != "已停用" else []
                relations.append(rel)
            conflicts = validate_relation_set(_included_rows())
            graph_version = None

        nodes: dict[str, dict[str, Any]] = {}
        for model in store.rows(MODEL_MODULE):
            if model.get("status") == "已废弃":
                continue
            key = f"model:{model['id']}"
            nodes[key] = {
                "key": key,
                "kind": "model",
                "label": _text(model.get("模型名称")),
                "sub": _text(model.get("事实表名")),
                "code": _text(model.get("模型编号")),
                "model_id": model.get("id"),
                "version": int(model.get("version", 1)),
                "dimensions": _text(model.get("关联维度")),
                "fields": [
                    {"name": _text(field.get("name")), "role": _text(field.get("role")), "type": _text(field.get("type"))}
                    for field in model.get("fields") or []
                ],
            }
        edges: list[dict[str, Any]] = []
        for rel in relations:
            up_key, down_key = rel["上游key"], rel["下游key"]
            for key, label in ((up_key, rel["上游节点"]), (down_key, rel["下游节点"])):
                if key not in nodes:
                    nodes[key] = {
                        "key": key,
                        "kind": "text",
                        "label": label,
                        "sub": "自由文本节点",
                        "code": "",
                        "model_id": None,
                        "version": None,
                        "dimensions": "",
                        "fields": [],
                    }
            edges.append({
                "id": f"e{rel['id']}",
                "relation_id": rel["id"],
                "code": rel["关系编号"],
                "from": up_key,
                "to": down_key,
                "source_field": rel["来源字段"],
                "target_field": rel["目标字段"],
                "transform": rel.get("转换函数", ""),
                "binding": rel["绑定形态"],
                "status": rel["status"],
                "problems": rel.get("problems", []),
            })
        return {
            "scope": scope,
            "snapshot_version": graph_version,
            "current_snapshot_version": int(snapshot["version"]) if snapshot else 0,
            "nodes": list(nodes.values()),
            "edges": edges,
            "conflicts": conflicts,
        }

    # ------------------------------------------------------------------
    # 影响分析：沿字段链遍历生效图，区分直接 / 间接 / 多路径重复
    # ------------------------------------------------------------------
    def analyze_impact(self, model_id: int, field: str, direction: str) -> tuple[dict[str, Any] | None, str]:
        snapshot = latest_snapshot()
        if snapshot is None:
            return None, "当前没有生效快照，请先发布血缘关系"
        model = model_by_id(model_id)
        if model is None:
            return None, f"模型 {model_id} 不存在"
        field = _text(field)
        if field not in _model_field_names(model):
            return None, f"字段「{field}」在模型「{model.get('模型名称')}」v{model.get('version', 1)} 中不存在"
        if direction not in ("downstream", "upstream"):
            return None, f"方向「{direction}」不支持，只能是 downstream 或 upstream"

        start_key = f"model:{int(model_id)}"
        # 只沿绑定了模型的关系展开；自由文本关系无法确认字段，明确列出但不参与
        adjacency: dict[tuple[str, str], list[tuple[tuple[str, str], dict[str, Any]]]] = {}
        legacy_skipped: list[dict[str, Any]] = []
        for rel in snapshot["relations"]:
            edge = {
                "code": rel["关系编号"],
                "from_label": rel["上游节点"],
                "to_label": rel["下游节点"],
                "source_field": rel["来源字段"],
                "target_field": rel["目标字段"],
            }
            if rel["绑定形态"] != "已绑定模型":
                legacy_skipped.append({
                    "关系编号": rel["关系编号"],
                    "上游节点": rel["上游节点"],
                    "下游节点": rel["下游节点"],
                    "原因": "自由文本节点未绑定模型，不参与字段级影响分析",
                })
                continue
            src = (rel["上游key"], rel["来源字段"])
            dst = (rel["下游key"], rel["目标字段"])
            if direction == "downstream":
                adjacency.setdefault(src, []).append((dst, edge))
            else:
                adjacency.setdefault(dst, []).append((src, edge))

        start = (start_key, field)
        dist: dict[tuple[str, str], int] = {start: 0}
        path_count: dict[tuple[str, str], int] = {start: 1}
        samples: dict[tuple[str, str], list[list[dict[str, Any]]]] = {start: [[]]}
        queue: deque[tuple[str, str]] = deque([start])
        while queue:
            vertex = queue.popleft()
            for nxt, edge in adjacency.get(vertex, []):
                if nxt not in dist:
                    dist[nxt] = dist[vertex] + 1
                    queue.append(nxt)
                if dist[nxt] == dist[vertex] + 1:
                    path_count[nxt] = min(PATH_CAP, path_count.get(nxt, 0) + path_count[vertex])
                    merged = samples.setdefault(nxt, [])
                    for sample in samples.get(vertex, []):
                        if len(merged) >= SAMPLE_CAP:
                            break
                        merged.append(sample + [edge])

        model_cache: dict[str, dict[str, Any] | None] = {}

        def model_of(node_key: str) -> dict[str, Any] | None:
            if node_key not in model_cache:
                mid = node_key.split(":", 1)[1] if node_key.startswith("model:") else None
                model_cache[node_key] = model_by_id(mid) if mid else None
            return model_cache[node_key]

        impacts: list[dict[str, Any]] = []
        for vertex, depth in sorted(dist.items(), key=lambda item: (item[1], item[0])):
            if vertex == start:
                continue
            node_key, vertex_field = vertex
            vertex_model = model_of(node_key)
            count = path_count.get(vertex, 1)
            impacts.append({
                "node_key": node_key,
                "model_id": vertex_model.get("id") if vertex_model else None,
                "label": _text(vertex_model.get("模型名称")) if vertex_model else node_key,
                "table": _text(vertex_model.get("事实表名")) if vertex_model else "",
                "dimensions": _text(vertex_model.get("关联维度")) if vertex_model else "",
                "field": vertex_field,
                "depth": depth,
                "kind": "直接" if depth == 1 else "间接",
                "multi": count >= 2,
                "paths": count,
                "samples": [self._format_path(sample) for sample in samples.get(vertex, [])[:SAMPLE_CAP]],
            })

        return {
            "root": {
                "node_key": start_key,
                "model_id": int(model_id),
                "label": _text(model.get("模型名称")),
                "table": _text(model.get("事实表名")),
                "field": field,
            },
            "direction": direction,
            "snapshot_version": snapshot["version"],
            "model_versions": snapshot.get("model_versions", {}),
            "impacts": impacts,
            "legacy_skipped": legacy_skipped,
            "summary": {
                "直接": sum(1 for item in impacts if item["kind"] == "直接"),
                "间接": sum(1 for item in impacts if item["kind"] == "间接"),
                "多路径": sum(1 for item in impacts if item["multi"]),
            },
        }, ""

    @staticmethod
    def _format_path(edges: list[dict[str, Any]]) -> str:
        if not edges:
            return ""
        parts = [f"{edges[0]['from_label']}.{edges[0]['source_field']}"]
        for edge in edges:
            parts.append(f"—{edge['code']}→ {edge['to_label']}.{edge['target_field']}")
        return " ".join(parts)

    # ------------------------------------------------------------------
    # 影响结论的保存与回看：保存时冻结当时的快照版本与路径
    # ------------------------------------------------------------------
    def save_impact(self, analysis: dict[str, Any], title: str | None) -> dict[str, Any]:
        impacts_table = store.aux(IMPACT_TABLE)
        entry_id = max((int(row.get("id", 0)) for row in impacts_table), default=0) + 1
        record = {
            "id": entry_id,
            "title": title or f"{analysis['root']['label']}.{analysis['root']['field']} 影响分析",
            "created_at": _now(),
            **deepcopy(analysis),
        }
        impacts_table.append(record)
        return record

    def list_impacts(self) -> list[dict[str, Any]]:
        current = latest_snapshot()
        current_version = int(current["version"]) if current else 0
        return [
            {
                "id": row["id"],
                "title": row["title"],
                "created_at": row["created_at"],
                "root": row["root"],
                "direction": row["direction"],
                "snapshot_version": row["snapshot_version"],
                "current_snapshot_version": current_version,
                "stale": row["snapshot_version"] != current_version,
                "summary": row.get("summary", {}),
            }
            for row in reversed(store.aux(IMPACT_TABLE))
        ]

    def get_impact(self, impact_id: int) -> dict[str, Any] | None:
        for row in store.aux(IMPACT_TABLE):
            if int(row.get("id", 0)) == impact_id:
                current = latest_snapshot()
                record = deepcopy(row)
                record["current_snapshot_version"] = int(current["version"]) if current else 0
                record["stale"] = record["snapshot_version"] != record["current_snapshot_version"]
                return record
        return None
