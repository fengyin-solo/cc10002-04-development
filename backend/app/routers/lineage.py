"""数据血缘接口：关系维护、整组发布、影响图、影响分析与快照回溯。"""
from __future__ import annotations

from typing import Any

from fastapi import APIRouter, HTTPException, Query
from pydantic import BaseModel, Field

from app.schemas import ActionResult, EntryPayload, PageResult
from app.services.lineage import LineageService

router = APIRouter(prefix="/api/lineage", tags=["数据血缘"])

service = LineageService()

LIST_FIELDS = ["关系编号", "上游节点", "下游节点", "来源字段", "目标字段", "转换函数", "采集批次", "关系版本"]
STATUSES = ["草稿", "待校验", "已启用", "已停用"]


class PublishPayload(BaseModel):
    """整组发布：基准版本用于并发裁决，只接受基于当前生效版本的发布。"""

    base_snapshot_version: int = 0
    base_model_versions: dict[str, int] = Field(default_factory=dict)
    remark: str | None = None


class ImpactPayload(BaseModel):
    model_id: int
    field: str
    direction: str = "downstream"
    save: bool = False
    title: str | None = None


class RevisionsPayload(BaseModel):
    """整组替换：一批新增/修订/停用一次性落为草稿。"""

    items: list[dict[str, Any]] = Field(default_factory=list)


# ------------------------------------------------------------------
# 图 / 发布 / 快照 / 影响分析：字面量路由必须先于 /{entry_id}
# ------------------------------------------------------------------
@router.get("/graph")
def get_graph(scope: str = Query(default="effective", description="effective=生效快照，draft=草稿")) -> dict[str, Any]:
    """影响图：生效快照或草稿（草稿附带整组校验冲突，用于图上定位）。"""
    if scope not in ("effective", "draft"):
        raise HTTPException(status_code=400, detail="scope 只能是 effective 或 draft")
    return service.build_graph(scope)


@router.post("/publish")
def publish(payload: PublishPayload) -> dict[str, Any]:
    """整组发布：自环、跨关系环、缺失模型字段都会让整组失败，失败时旧图继续生效。"""
    return service.publish(payload.base_snapshot_version, payload.base_model_versions, payload.remark)


@router.get("/snapshots")
def list_snapshots() -> dict[str, Any]:
    """发布快照历史：每一次生效版本都可回溯。"""
    return {"items": service.list_snapshots()}


@router.get("/snapshots/{version}")
def get_snapshot(version: int) -> dict[str, Any]:
    snapshot = service.get_snapshot(version)
    if snapshot is None:
        raise HTTPException(status_code=404, detail=f"血缘快照 v{version} 不存在")
    return snapshot


@router.post("/impact")
def run_impact(payload: ImpactPayload) -> dict[str, Any]:
    """从模型字段出发沿字段链展开影响路径；save=true 时把结论与当时版本一起保存。"""
    analysis, error = service.analyze_impact(payload.model_id, payload.field, payload.direction)
    if analysis is None:
        raise HTTPException(status_code=400, detail=error)
    if payload.save:
        record = service.save_impact(analysis, payload.title)
        return {"ok": True, "saved_id": record["id"], "analysis": record}
    return {"ok": True, "saved_id": None, "analysis": analysis}


@router.get("/impacts")
def list_impacts() -> dict[str, Any]:
    """已保存的影响结论清单，标注各自基于的快照版本。"""
    return {"items": service.list_impacts()}


@router.get("/impacts/{impact_id}")
def get_impact(impact_id: int) -> dict[str, Any]:
    record = service.get_impact(impact_id)
    if record is None:
        raise HTTPException(status_code=404, detail=f"影响分析结论 {impact_id} 不存在")
    return record


@router.post("/revisions")
def apply_revisions(payload: RevisionsPayload) -> dict[str, Any]:
    """整组替换一批关系（新增/修订/停用），一次落为草稿，发布时按整组结果校验。"""
    if not payload.items:
        return {"ok": False, "message": "整组替换内容为空", "items": []}
    ok, message, items = service.apply_revisions(payload.items)
    return {"ok": ok, "message": message, "items": items}


@router.get("/export")
def export_entries() -> dict[str, Any]:
    """导出数据血缘清单：返回当前过滤条件下的全量数据。"""
    items, total = service.list_entries(page=1, size=10000)
    return {"module": "lineage", "total": total, "items": items}


# ------------------------------------------------------------------
# 关系列表 / 明细 / 登记 / 修订 / 动作
# ------------------------------------------------------------------
@router.get("", response_model=PageResult[dict])
def list_entries(
    keyword: str | None = Query(default=None, description="按关系编号检索"),
    status: str | None = Query(default=None, description="草稿、待校验、已启用、已停用"),
    page: int = 1,
    size: int = 20,
) -> PageResult[dict]:
    """按关系编号与状态过滤数据血缘列表；每行附带绑定状态、生效状态与结构问题。"""
    if size > 200:
        raise HTTPException(status_code=400, detail="每页最多 200 条，请缩小分页范围")
    items, total = service.list_entries(keyword=keyword, status=status, page=page, size=size)
    return PageResult(items=items, total=total, page=page, size=size)


@router.get("/{entry_id}", response_model=dict)
def get_entry(entry_id: int) -> dict:
    """读取单条血缘关系明细；不存在时给出可读的错误说明。"""
    entry = service.get_entry(entry_id)
    if entry is None:
        raise HTTPException(status_code=404, detail=f"血缘关系 {entry_id} 不存在或已归档")
    return entry


@router.post("", response_model=ActionResult)
def create_entry(payload: EntryPayload) -> ActionResult:
    """登记一条血缘关系（可绑定模型或保留自由文本），缺字段时说明原因而不是静默丢弃。"""
    entry, missing = service.create_entry(payload.values)
    if missing:
        return ActionResult(ok=False, message=f"缺少必填字段：{'、'.join(missing)}")
    return ActionResult(ok=True, message="血缘关系已登记为草稿，发布后生效", entry=entry)


@router.put("/{entry_id}", response_model=ActionResult)
def revise_entry(entry_id: int, payload: EntryPayload) -> ActionResult:
    """修订一条关系：改绑定、改字段都只改草稿，发布后才进入生效图。"""
    entry, message = service.revise_entry(entry_id, payload.values)
    if entry is None:
        return ActionResult(ok=False, message=message)
    return ActionResult(ok=True, message=message, entry=entry)


@router.delete("/{entry_id}", response_model=ActionResult)
def delete_entry(entry_id: int) -> ActionResult:
    """删除草稿关系；仍在生效快照中的关系必须先停用并发布。"""
    ok, message = service.delete_entry(entry_id)
    return ActionResult(ok=ok, message=message)


@router.post("/{entry_id}/actions", response_model=ActionResult)
def run_action(entry_id: int, payload: EntryPayload) -> ActionResult:
    """对单条血缘关系执行提交校验、启用关系、停用关系；启用/停用要发布后才影响生效图。"""
    action = str(payload.values.get("action") or "").strip()
    entry, message = service.run_action(entry_id, action)
    if entry is None:
        return ActionResult(ok=False, message=message)
    return ActionResult(ok=True, message=message, entry=entry)
