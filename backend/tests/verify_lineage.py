"""数据血缘 × 数据建模联动特性的端到端验证脚本。

覆盖：菱形依赖的多路径影响、自环/跨关系环/缺字段的整组发布拦截、整组替换的顺序无关性、
版本裁决（快照版本与模型版本）、模型删字段被血缘拦截、停用后新图生效、
历史结论可追溯、自由文本节点兼容且不伪造绑定。

用法：先起后端（127.0.0.1:8000），再执行 `.venv/bin/python tests/verify_lineage.py`。
脚本会改动数据，验证完后重启后端即可回到种子状态。
"""
from __future__ import annotations

import json
import sys
import urllib.request
import urllib.error

BASE = "http://127.0.0.1:8000"
FAILURES: list[str] = []
CHECKS = [0]


def call(method: str, path: str, body: dict | None = None) -> tuple[int, dict]:
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(BASE + path, data=data, method=method,
                                 headers={"Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req) as resp:
            return resp.status, json.loads(resp.read().decode())
    except urllib.error.HTTPError as exc:
        return exc.code, json.loads(exc.read().decode())


def check(name: str, cond: bool, detail: str = "") -> None:
    CHECKS[0] += 1
    if cond:
        print(f"  ✓ {name}")
    else:
        FAILURES.append(name)
        print(f"  ✗ {name}  {detail}")


def current_snapshot_version() -> int:
    _, payload = call("GET", "/api/lineage/snapshots")
    return payload["items"][0]["version"]


def model_versions() -> dict[str, int]:
    _, payload = call("GET", "/api/datamodel?size=200")
    return {str(row["id"]): row["version"] for row in payload["items"]}


def publish(expect_ok: bool, remark: str = "") -> dict:
    body = {"base_snapshot_version": current_snapshot_version(),
            "base_model_versions": model_versions(), "remark": remark}
    _, payload = call("POST", "/api/lineage/publish", body)
    if expect_ok:
        check(f"发布成功（{remark}）", payload.get("ok") is True, json.dumps(payload, ensure_ascii=False))
    return payload


def main() -> int:
    # ---------------- 阶段 1：初始快照与菱形影响 ----------------
    print("阶段 1：初始快照与菱形依赖影响分析")
    _, graph = call("GET", "/api/lineage/graph?scope=effective")
    check("生效图为快照 v1", graph["snapshot_version"] == 1)
    model_nodes = [n for n in graph["nodes"] if n["kind"] == "model"]
    text_nodes = [n for n in graph["nodes"] if n["kind"] == "text"]
    check("模型节点全部入图（含无血缘的 M5）", len(model_nodes) == 5, str(len(model_nodes)))
    check("自由文本节点共存", len(text_nodes) >= 3, str([n["label"] for n in text_nodes]))
    check("生效边 9 条（6 绑定 + 3 自由文本）", len(graph["edges"]) == 9, str(len(graph["edges"])))

    _, resp = call("POST", "/api/lineage/impact", {"model_id": 1, "field": "order_amount", "direction": "downstream"})
    impacts = {i["label"] + "." + i["field"]: i for i in resp["analysis"]["impacts"]}
    check("菱形：用户日汇总.gmv 直接影响", impacts.get("用户日汇总模型.gmv", {}).get("kind") == "直接")
    check("菱形：商品日汇总.gmv 直接影响", impacts.get("商品日汇总模型.gmv", {}).get("kind") == "直接")
    wide = impacts.get("用户GMV宽表模型.gmv_7d", {})
    check("菱形：宽表.gmv_7d 间接影响", wide.get("kind") == "间接")
    check("菱形：宽表.gmv_7d 多路径重复（2 条路径）", wide.get("multi") is True and wide.get("paths") == 2,
          json.dumps(wide, ensure_ascii=False))
    check("影响结果带快照版本", resp["analysis"]["snapshot_version"] == 1)
    check("自由文本关系被明确排除", len(resp["analysis"]["legacy_skipped"]) == 3)

    _, resp = call("POST", "/api/lineage/impact", {"model_id": 4, "field": "gmv_7d", "direction": "upstream", "save": True, "title": "gmv_7d 上游溯源"})
    check("上游溯源可保存", resp.get("ok") is True and resp.get("saved_id") == 1)
    up_impacts = {i["label"] + "." + i["field"]: i for i in resp["analysis"]["impacts"]}
    check("上游：订单事实.order_amount 间接+多路径",
          up_impacts.get("订单交易事实模型.order_amount", {}).get("multi") is True)

    _, resp = call("POST", "/api/lineage/impact", {"model_id": 1, "field": "order_amount", "direction": "downstream", "save": True})
    check("下游影响结论已保存 id=2", resp.get("saved_id") == 2)

    # ---------------- 阶段 2：自环拦截 ----------------
    print("阶段 2：自环不能发布，旧图保持")
    _, resp = call("POST", "/api/lineage", {"values": {"上游模型ID": 1, "下游模型ID": 1, "来源字段": "user_id", "目标字段": "order_amount"}})
    self_loop_id = resp["entry"]["id"]
    _, draft_graph = call("GET", "/api/lineage/graph?scope=draft")
    check("草稿图校验标出自环", any(c["type"] == "自环" for c in draft_graph["conflicts"]))
    result = publish(False, "尝试发布自环")
    check("自环整组拒绝", result.get("stage") == "校验失败" and any(c["type"] == "自环" for c in result["conflicts"]))
    check("自环失败后旧图仍 v1", current_snapshot_version() == 1)
    _, resp = call("DELETE", f"/api/lineage/{self_loop_id}")
    check("自环草稿可删除", resp.get("ok") is True)

    # ---------------- 阶段 3：跨多条关系形成的环 ----------------
    print("阶段 3：跨关系环不能发布，停用后发布成功")
    _, resp = call("POST", "/api/lineage", {"values": {"上游模型ID": 4, "下游模型ID": 2, "来源字段": "gmv_7d", "目标字段": "gmv"}})
    cycle_id = resp["entry"]["id"]
    result = publish(False, "尝试发布成环草稿")
    cycle_conflicts = [c for c in result["conflicts"] if c["type"] == "环路"]
    check("跨关系环被识别", len(cycle_conflicts) == 1, json.dumps(result, ensure_ascii=False))
    check("环路径包含 LINE-0005", cycle_conflicts and "LINE-0005" in cycle_conflicts[0].get("relations", []))
    check("成环失败后旧图仍 v1", current_snapshot_version() == 1)
    call("POST", f"/api/lineage/{cycle_id}/actions", {"values": {"action": "停用关系"}})
    result = publish(True, "停用成环关系后发布")
    check("发布后快照 v2", result["snapshot"]["version"] == 2)

    # ---------------- 阶段 4：整组替换不按中间顺序误判 ----------------
    print("阶段 4：整组替换（新增反向边 + 停用原边）按最终集合判断")
    batch = {"items": [
        {"values": {"关系编号": "LINE-9001", "上游模型ID": 4, "下游模型ID": 2, "来源字段": "gmv_7d", "目标字段": "gmv"}},
        {"id": 5, "disable": True},
    ]}
    _, resp = call("POST", "/api/lineage/revisions", batch)
    check("整组替换写入草稿", resp.get("ok") is True, json.dumps(resp, ensure_ascii=False))
    result = publish(True, "整组替换发布")
    check("整组替换发布为 v3（逐条校验会误判成环）", result["snapshot"]["version"] == 3)
    _, resp = call("POST", "/api/lineage/impact", {"model_id": 4, "field": "gmv_7d", "direction": "downstream"})
    labels = [i["label"] for i in resp["analysis"]["impacts"]]
    check("新图生效：gmv_7d 直接影响用户日汇总", "用户日汇总模型" in labels, str(labels))
    # 还原菱形：停用 LINE-9001，重新启用 LINE-0005
    _, rows = call("GET", "/api/lineage?keyword=LINE-9001")
    line9001 = rows["items"][0]["id"]
    call("POST", f"/api/lineage/{line9001}/actions", {"values": {"action": "停用关系"}})
    call("POST", "/api/lineage/5/actions", {"values": {"action": "启用关系"}})
    result = publish(True, "还原菱形")
    check("还原后快照 v4", result["snapshot"]["version"] == 4)

    # ---------------- 阶段 5：缺失模型字段不能发布 ----------------
    print("阶段 5：缺失模型字段不能发布")
    _, resp = call("POST", "/api/lineage", {"values": {"上游模型ID": 1, "下游模型ID": 2, "来源字段": "no_such_field", "目标字段": "gmv"}})
    bad_id = resp["entry"]["id"]
    result = publish(False, "尝试发布缺字段关系")
    check("缺字段整组拒绝", any(c["type"] == "字段缺失" for c in result["conflicts"]))
    check("失败后旧图仍 v4", current_snapshot_version() == 4)
    call("DELETE", f"/api/lineage/{bad_id}")

    # ---------------- 阶段 6：并发版本裁决 ----------------
    print("阶段 6：模型变更与血缘发布的版本裁决")
    stale = {"base_snapshot_version": 1, "base_model_versions": model_versions()}
    _, resp = call("POST", "/api/lineage/publish", stale)
    check("过期快照基准被拒绝并给出双方版本", resp.get("stage") == "版本冲突" and "v4" in resp.get("message", ""))
    _, resp = call("PUT", "/api/datamodel/5/fields", {"base_version": 1, "fields": [
        {"name": "user_id", "type": "bigint", "role": "维度键"},
        {"name": "feature_score", "type": "double", "role": "度量"},
        {"name": "exp_group", "type": "string", "role": "普通"},
        {"name": "dt", "type": "string", "role": "分区"}]})
    check("模型字段基于当前版本修改成功 → v2", resp.get("ok") is True and resp.get("current_version") == 2)
    _, resp = call("PUT", "/api/datamodel/5/fields", {"base_version": 1, "fields": [{"name": "user_id", "role": "维度键"}]})
    check("过期模型基准被拒绝", resp.get("stage") == "版本冲突" and resp.get("current_version") == 2)
    # 模型 M1 加字段（不删），血缘草稿的模型基准随即过期
    _, m1 = call("GET", "/api/datamodel/1")
    new_fields = m1["fields"] + [{"name": "buyer_cnt", "type": "bigint", "role": "度量"}]
    _, resp = call("PUT", "/api/datamodel/1/fields", {"base_version": m1["version"], "fields": new_fields, "remark": "加 buyer_cnt"})
    check("M1 加字段成功 → v2", resp.get("ok") is True)
    stale_models = model_versions() | {"1": 1}
    _, resp = call("POST", "/api/lineage/publish", {"base_snapshot_version": current_snapshot_version(), "base_model_versions": stale_models})
    check("血缘发布发现模型基准过期", resp.get("stage") == "模型版本冲突"
          and any(c.get("model_id") == 1 and c.get("base_version") == 1 and c.get("current_version") == 2 for c in resp["conflicts"]))
    result = publish(True, "以当前模型版本为基准重新发布")
    check("按当前版本裁决后发布 v5", result["snapshot"]["version"] == 5)

    # ---------------- 阶段 7：模型删字段被生效血缘拦截 ----------------
    print("阶段 7：模型删除被引用字段 → 拦截 → 血缘修订发布 → 放行")
    _, m4 = call("GET", "/api/datamodel/4")
    kept = [f for f in m4["fields"] if f["name"] != "gmv_7d"]
    _, resp = call("PUT", "/api/datamodel/4/fields", {"base_version": m4["version"], "fields": kept})
    ref_relations = {c.get("relation") for c in resp.get("conflicts", [])}
    check("删 gmv_7d 被血缘引用拦截", resp.get("stage") == "血缘引用冲突"
          and {"LINE-0005", "LINE-0006"} <= ref_relations, json.dumps(resp, ensure_ascii=False))
    check("拦截后模型版本未变", model_versions()["4"] == m4["version"])
    call("POST", "/api/lineage/6/actions", {"values": {"action": "停用关系"}})
    publish(True, "停用 LINE-0006")
    _, resp = call("PUT", "/api/datamodel/4/fields", {"base_version": m4["version"], "fields": kept})
    check("仍被 LINE-0005 拦截", resp.get("stage") == "血缘引用冲突"
          and {c.get("relation") for c in resp["conflicts"]} == {"LINE-0005"})
    call("POST", "/api/lineage/5/actions", {"values": {"action": "停用关系"}})
    publish(True, "停用 LINE-0005")
    _, resp = call("PUT", "/api/datamodel/4/fields", {"base_version": m4["version"], "fields": kept, "remark": "下线 gmv_7d"})
    check("血缘清理后删字段成功 → M4 v2", resp.get("ok") is True and resp.get("current_version") == 2)

    # ---------------- 阶段 8：停用后新分析采用新图，历史结论仍可追溯 ----------------
    print("阶段 8：新分析用新图，已保存结论保留当时路径与版本")
    _, resp = call("POST", "/api/lineage/impact", {"model_id": 1, "field": "order_amount", "direction": "downstream"})
    check("新图下宽表不再受影响", all(i["label"] != "用户GMV宽表模型" for i in resp["analysis"]["impacts"]))
    check("新图下无多路径", resp["analysis"]["summary"]["多路径"] == 0)
    _, saved = call("GET", "/api/lineage/impacts/2")
    check("历史结论仍指向快照 v1", saved["snapshot_version"] == 1 and saved["stale"] is True)
    saved_wide = [i for i in saved["impacts"] if i["label"] == "用户GMV宽表模型"]
    check("历史结论保留当时 2 条路径", saved_wide and saved_wide[0]["paths"] == 2 and len(saved_wide[0]["samples"]) == 2)
    _, snap1 = call("GET", "/api/lineage/snapshots/1")
    check("快照 v1 仍可回溯（9 条关系）", len(snap1["relations"]) == 9)
    _, versions = call("GET", "/api/datamodel/4/versions")
    check("M4 版本历史记录删字段", any(v.get("removed") == ["gmv_7d"] for v in versions["items"]))

    # ---------------- 阶段 9：自由文本节点兼容，不伪造绑定 ----------------
    print("阶段 9：自由文本节点标出共存，启用成环旧关系被拦截")
    call("POST", "/api/lineage/9/actions", {"values": {"action": "启用关系"}})
    result = publish(False, "启用旧环关系")
    check("自由文本跨关系环被拦截", any(c["type"] == "环路" for c in result["conflicts"]))
    call("POST", "/api/lineage/9/actions", {"values": {"action": "停用关系"}})
    call("POST", "/api/lineage/11/actions", {"values": {"action": "启用关系"}})
    result = publish(False, "启用旧自环关系")
    check("自由文本自环被拦截", any(c["type"] == "自环" for c in result["conflicts"]))
    call("POST", "/api/lineage/11/actions", {"values": {"action": "停用关系"}})
    publish(True, "恢复干净图")
    _, resp = call("POST", "/api/lineage", {"values": {"上游节点": "订单交易事实模型", "下游节点": "随便一个文本", "来源字段": "a", "目标字段": "b"}})
    text_row = resp["entry"]
    check("同名自由文本不伪造模型绑定", text_row["绑定状态"] == "自由文本" and text_row["上游节点key"].startswith("text:"))
    call("DELETE", f"/api/lineage/{text_row['id']}")

    # ---------------- 阶段 10：被引用模型不能废弃 ----------------
    print("阶段 10：被生效血缘引用的模型不能废弃")
    _, resp = call("POST", "/api/datamodel/2/actions", {"values": {"action": "废弃下线"}})
    check("M2 废弃被拦截", resp.get("ok") is False and "LINE-0001" in resp.get("message", ""))
    _, resp = call("POST", "/api/datamodel/5/actions", {"values": {"action": "废弃下线"}})
    check("无引用模型可废弃", resp.get("ok") is True)

    print(f"\n共 {CHECKS[0]} 项检查，失败 {len(FAILURES)} 项")
    if FAILURES:
        print("失败项：", "；".join(FAILURES))
        return 1
    print("全部通过 ✓")
    return 0


if __name__ == "__main__":
    sys.exit(main())
