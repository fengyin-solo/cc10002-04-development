"""数据建模业务规则：模型字段版本化、与血缘发布共用同一把锁的并发裁决。

模型字段变更只接受基于当前版本的修改；只要生效血缘快照还引用着要被删除的字段，
就整组拒绝这次变更——不能产生看似有效、却指向已不存在字段的图。
"""
from __future__ import annotations

from copy import deepcopy
from datetime import datetime
from typing import Any

from app.services.lineage import GRAPH_LOCK, latest_snapshot
from app.store import store

MODULE = "datamodel"
REQUIRED_FIELDS = ["模型编号", "模型名称", "事实表名"]
STATUS_ORDER = ["草稿", "待审核", "已生效", "已废弃"]
ACTION_RULES = {"提交审核": "待审核", "生效发布": "已生效", "废弃下线": "已废弃"}
NEGATIVE_ACTIONS = []

FIELD_ROLES = ["维度键", "度量", "分区", "普通"]


def _now() -> str:
    return datetime.now().isoformat(timespec="seconds")


def _text(value: Any) -> str:
    return str(value or "").strip()


def _normalize_fields(raw_fields: list[dict[str, Any]]) -> tuple[list[dict[str, str]], str]:
    """校验并规范化字段清单；返回 (字段列表, 错误消息)，错误消息为空表示通过。"""
    normalized: list[dict[str, str]] = []
    seen: set[str] = set()
    for raw in raw_fields:
        name = _text(raw.get("name"))
        if not name:
            return [], "字段名不能为空"
        if name in seen:
            return [], f"字段名「{name}」重复"
        seen.add(name)
        role = _text(raw.get("role")) or "普通"
        if role not in FIELD_ROLES:
            return [], f"字段「{name}」的角色「{role}」不支持，可选：{'、'.join(FIELD_ROLES)}"
        normalized.append({"name": name, "type": _text(raw.get("type")) or "string", "role": role})
    if not normalized:
        return [], "模型至少保留一个字段"
    return normalized, ""


def _lineage_refs_of_fields(model_id: int, field_names: set[str]) -> list[dict[str, Any]]:
    """生效血缘快照中引用了该模型这些字段的关系。"""
    snapshot = latest_snapshot()
    if snapshot is None or not field_names:
        return []
    refs: list[dict[str, Any]] = []
    for rel in snapshot["relations"]:
        for side, field_key, id_key in (("上游", "来源字段", "上游模型ID"), ("下游", "目标字段", "下游模型ID")):
            if rel.get(id_key) == model_id and rel.get(field_key) in field_names:
                refs.append({
                    "type": "血缘引用",
                    "relation": rel.get("关系编号"),
                    "relation_id": rel.get("id"),
                    "side": side,
                    "field": rel.get(field_key),
                    "snapshot_version": snapshot["version"],
                    "message": f"生效快照 v{snapshot['version']} 的关系 {rel.get('关系编号')} 的{side}仍引用字段「{rel.get(field_key)}」",
                })
    return refs


def _lineage_refs_of_model(model_id: int) -> list[dict[str, Any]]:
    snapshot = latest_snapshot()
    if snapshot is None:
        return []
    return [
        {"relation": rel.get("关系编号"), "snapshot_version": snapshot["version"]}
        for rel in snapshot["relations"]
        if rel.get("上游模型ID") == model_id or rel.get("下游模型ID") == model_id
    ]


def _refresh_display_fields(model: dict[str, Any]) -> None:
    """字段清单变化后，同步展示用的度量列定义与分区字段。"""
    fields = model.get("fields") or []
    measures = [f"{field['name']}:{field['type']}" for field in fields if field.get("role") == "度量"]
    if measures:
        model["度量列定义"] = ", ".join(measures)
    partitions = [field["name"] for field in fields if field.get("role") == "分区"]
    if partitions:
        model["分区字段"] = ", ".join(partitions)


class DatamodelService:
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
            rows = [row for row in rows if keyword in str(row.get("模型编号", ""))]
        if status:
            rows = [row for row in rows if row.get("status") == status]
        total = len(rows)
        start = max(page - 1, 0) * size
        return rows[start:start + size], total

    def get_entry(self, entry_id: int) -> dict[str, Any] | None:
        return store.find(MODULE, entry_id)

    def create_entry(self, values: dict[str, Any]) -> tuple[dict[str, Any] | None, list[str]]:
        missing = [field for field in REQUIRED_FIELDS if not str(values.get(field) or "").strip()]
        if missing:
            return None, missing
        rows = store.rows(MODULE)
        entry = {"id": max((int(row.get("id", 0)) for row in rows), default=0) + 1}
        entry.update({field: values.get(field) for field in REQUIRED_FIELDS})
        for field in ["关联维度", "度量列定义", "分区字段", "增量键", "刷新周期"]:
            entry[field] = values.get(field) or ""
        fields, error = _normalize_fields(values.get("fields") or [{"name": "id", "type": "bigint", "role": "普通"}])
        if error:
            return None, [error]
        entry["fields"] = fields
        entry["version"] = 1
        entry["版本历史"] = [{"version": 1, "time": _now(), "remark": "登记模型", "fields": deepcopy(fields)}]
        entry["status"] = STATUS_ORDER[0]
        entry["pending"] = True
        entry["abnormal"] = False
        rows.append(entry)
        return entry, []

    def run_action(self, entry_id: int, action: str) -> tuple[dict[str, Any] | None, str]:
        entry = store.find(MODULE, entry_id)
        if entry is None:
            return None, f"模型定义 {entry_id} 不存在或已归档"
        if action not in ACTION_RULES:
            return None, f"动作「{action}」不属于数据建模可执行范围"
        target = ACTION_RULES[action]
        if target not in STATUS_ORDER:
            return None, f"目标状态「{target}」不在允许的状态序列里"
        if target == "已废弃":
            with GRAPH_LOCK:
                refs = _lineage_refs_of_model(entry_id)
                if refs:
                    codes = "、".join(ref["relation"] for ref in refs)
                    version = refs[0]["snapshot_version"]
                    return None, (
                        f"模型仍被生效血缘快照 v{version} 的 {len(refs)} 条关系引用（{codes}），"
                        "请先在数据血缘中停用这些关系并发布，再废弃模型"
                    )
                entry["status"] = target
                entry["pending"] = False
                entry["abnormal"] = False
                return entry, f"模型定义已{action}"
        entry["status"] = target
        entry["pending"] = target != STATUS_ORDER[-1]
        entry["abnormal"] = action in NEGATIVE_ACTIONS
        return entry, f"模型定义已{action}"

    # ------------------------------------------------------------------
    # 字段版本化变更：基准版本裁决 + 生效血缘引用拦截
    # ------------------------------------------------------------------
    def update_fields(self, entry_id: int, base_version: int, raw_fields: list[dict[str, Any]], remark: str | None) -> dict[str, Any]:
        with GRAPH_LOCK:
            model = store.find(MODULE, entry_id)
            if model is None:
                return {"ok": False, "stage": "不存在", "message": f"模型定义 {entry_id} 不存在或已归档", "conflicts": []}
            current_version = int(model.get("version", 1))
            if base_version != current_version:
                return {
                    "ok": False,
                    "stage": "版本冲突",
                    "message": f"模型「{model.get('模型名称')}」当前版本为 v{current_version}，本次修改基于 v{base_version}，请刷新后重试",
                    "conflicts": [],
                    "current_version": current_version,
                }
            fields, error = _normalize_fields(raw_fields)
            if error:
                return {"ok": False, "stage": "校验失败", "message": error, "conflicts": []}
            old_names = {_text(field.get("name")) for field in model.get("fields") or []}
            new_names = {field["name"] for field in fields}
            removed = old_names - new_names
            refs = _lineage_refs_of_fields(entry_id, removed)
            if refs:
                snapshot = latest_snapshot()
                return {
                    "ok": False,
                    "stage": "血缘引用冲突",
                    "message": (
                        f"字段变更会让生效血缘快照 v{snapshot['version'] if snapshot else '-'} 指向不存在的字段，"
                        "已整组拒绝；请先在数据血缘中修订或停用相关关系并发布"
                    ),
                    "conflicts": refs,
                    "current_version": current_version,
                }
            model["fields"] = fields
            model["version"] = current_version + 1
            model.setdefault("版本历史", []).append({
                "version": model["version"],
                "time": _now(),
                "remark": remark or "字段变更",
                "base_version": current_version,
                "removed": sorted(removed),
                "fields": deepcopy(fields),
            })
            _refresh_display_fields(model)
            return {
                "ok": True,
                "stage": "已生效",
                "message": f"模型「{model.get('模型名称')}」字段已更新到 v{model['version']}",
                "conflicts": [],
                "current_version": model["version"],
                "entry": model,
            }

    def list_versions(self, entry_id: int) -> list[dict[str, Any]] | None:
        model = store.find(MODULE, entry_id)
        if model is None:
            return None
        return list(reversed(model.get("版本历史") or []))
