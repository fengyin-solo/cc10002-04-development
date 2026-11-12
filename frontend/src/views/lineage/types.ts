/** 数据血缘 × 数据建模联动的接口类型。 */

export interface ModelField {
  name: string
  type: string
  role: string
}

export interface ModelEntry {
  id: number
  status: string
  模型编号: string
  模型名称: string
  事实表名: string
  关联维度: string
  度量列定义: string
  分区字段: string
  增量键: string
  刷新周期: string
  version: number
  fields: ModelField[]
  版本历史?: ModelVersion[]
}

export interface ModelVersion {
  version: number
  time: string
  remark: string
  base_version?: number
  removed?: string[]
  fields: ModelField[]
}

export interface LineageRow {
  id: number
  status: string
  关系编号: string
  上游节点: string
  下游节点: string
  来源字段: string
  目标字段: string
  转换函数: string
  采集批次: string
  关系版本: string
  上游模型ID: number | null
  下游模型ID: number | null
  绑定状态: string
  生效状态: string
  问题: string[]
  上游节点key: string
  下游节点key: string
}

export interface GraphNode {
  key: string
  kind: 'model' | 'text'
  label: string
  sub: string
  code: string
  model_id: number | null
  version: number | null
  dimensions: string
  fields: { name: string; role: string; type: string }[]
}

export interface GraphEdge {
  id: string
  relation_id: number
  code: string
  from: string
  to: string
  source_field: string
  target_field: string
  transform: string
  binding: string
  status: string
  problems: string[]
}

export interface Conflict {
  type: string
  message: string
  relation?: string | null
  relation_id?: number | null
  node_keys?: string[]
  relations?: string[]
  path?: string[]
  model_id?: number
  current_version?: number
  base_version?: number | null
}

export interface GraphPayload {
  scope: string
  snapshot_version: number | null
  current_snapshot_version: number
  nodes: GraphNode[]
  edges: GraphEdge[]
  conflicts: Conflict[]
}

export interface ImpactItem {
  node_key: string
  model_id: number | null
  label: string
  table: string
  dimensions: string
  field: string
  depth: number
  kind: '直接' | '间接'
  multi: boolean
  paths: number
  samples: string[]
}

export interface ImpactAnalysis {
  root: { node_key: string; model_id: number; label: string; table: string; field: string }
  direction: string
  snapshot_version: number
  model_versions: Record<string, number>
  impacts: ImpactItem[]
  legacy_skipped: { 关系编号: string; 上游节点: string; 下游节点: string; 原因: string }[]
  summary: { 直接: number; 间接: number; 多路径: number }
}

export interface SavedImpact {
  id: number
  title: string
  created_at: string
  root: ImpactAnalysis['root']
  direction: string
  snapshot_version: number
  current_snapshot_version: number
  stale: boolean
  summary: { 直接: number; 间接: number; 多路径: number }
}

export interface SnapshotInfo {
  version: number
  created_at: string
  remark: string
  base_version: number
  relation_count: number
  model_versions: Record<string, number>
}

export interface PublishResult {
  ok: boolean
  stage: string
  message: string
  conflicts: Conflict[]
  snapshot?: { version: number; created_at: string; remark: string }
  current?: { snapshot_version: number; model_versions?: Record<string, number> }
}
