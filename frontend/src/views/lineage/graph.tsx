import { useMemo } from 'react'

import type { GraphEdge, GraphNode } from './types'

/** 分层布局的影响图：左到右按拓扑层摆放，模型节点可点字段，冲突高亮。 */

const NODE_WIDTH = 208
const HEADER_HEIGHT = 46
const FIELD_HEIGHT = 20
const MAX_FIELDS = 7
const LAYER_GAP = 120
const NODE_GAP = 26

interface LayoutNode extends GraphNode {
  x: number
  y: number
  height: number
  layer: number
}

interface Props {
  nodes: GraphNode[]
  edges: GraphEdge[]
  highlightNodes?: Set<string>
  highlightEdges?: Set<string>
  impactNodes?: Set<string>
  selectedField?: { nodeKey: string; field: string } | null
  onFieldClick?: (node: GraphNode, field: string) => void
}

function nodeHeight(node: GraphNode): number {
  const shown = Math.min(node.fields.length, MAX_FIELDS)
  return HEADER_HEIGHT + (node.kind === 'model' ? Math.max(shown, 1) * FIELD_HEIGHT + 10 : 12)
}

function layout(nodes: GraphNode[], edges: GraphEdge[]): LayoutNode[] {
  const activeEdges = edges.filter((edge) => edge.status !== '已停用')
  const indegree = new Map<string, number>()
  const outgoing = new Map<string, string[]>()
  for (const node of nodes) {
    indegree.set(node.key, 0)
    outgoing.set(node.key, [])
  }
  for (const edge of activeEdges) {
    if (!outgoing.has(edge.from) || !indegree.has(edge.to)) continue
    outgoing.get(edge.from)!.push(edge.to)
    indegree.set(edge.to, (indegree.get(edge.to) ?? 0) + 1)
  }
  // Kahn 最长路径分层；草稿可能有环，走不完的节点追加到最后一层
  const layer = new Map<string, number>()
  const queue: string[] = []
  for (const node of nodes) {
    if ((indegree.get(node.key) ?? 0) === 0) {
      layer.set(node.key, 0)
      queue.push(node.key)
    }
  }
  const remaining = new Map(indegree)
  const processed = new Set<string>()
  while (queue.length > 0) {
    const key = queue.shift()!
    processed.add(key)
    for (const next of outgoing.get(key) ?? []) {
      layer.set(next, Math.max(layer.get(next) ?? 0, (layer.get(key) ?? 0) + 1))
      remaining.set(next, (remaining.get(next) ?? 1) - 1)
      if ((remaining.get(next) ?? 0) <= 0 && !processed.has(next)) {
        queue.push(next)
        processed.add(next)
      }
    }
  }
  let maxLayer = Math.max(0, ...layer.values())
  for (const node of nodes) {
    if (!layer.has(node.key)) {
      maxLayer += 1
      layer.set(node.key, maxLayer)
    }
  }
  const byLayer = new Map<number, LayoutNode[]>()
  for (const node of nodes) {
    const l = layer.get(node.key) ?? 0
    const item: LayoutNode = { ...node, x: 0, y: 0, height: nodeHeight(node), layer: l }
    const list = byLayer.get(l) ?? []
    list.push(item)
    byLayer.set(l, list)
  }
  const result: LayoutNode[] = []
  for (const [l, list] of byLayer) {
    let y = 30
    for (const item of list) {
      item.x = 30 + l * (NODE_WIDTH + LAYER_GAP)
      item.y = y
      y += item.height + NODE_GAP
      result.push(item)
    }
  }
  return result
}

export default function LineageGraph({
  nodes,
  edges,
  highlightNodes,
  highlightEdges,
  impactNodes,
  selectedField,
  onFieldClick,
}: Props) {
  const laidOut = useMemo(() => layout(nodes, edges), [nodes, edges])
  const positions = useMemo(() => new Map(laidOut.map((node) => [node.key, node])), [laidOut])
  const width = Math.max(600, ...laidOut.map((node) => node.x + NODE_WIDTH + 40))
  const height = Math.max(320, ...laidOut.map((node) => node.y + node.height + 40))

  return (
    <div className="graph-scroll">
      <svg className="lineage-graph" width={width} height={height} role="img">
        <defs>
          <marker id="arrow" viewBox="0 0 10 10" refX="9" refY="5" markerWidth="7" markerHeight="7" orient="auto-start-reverse">
            <path d="M 0 0 L 10 5 L 0 10 z" fill="#94a3b8" />
          </marker>
          <marker id="arrow-conflict" viewBox="0 0 10 10" refX="9" refY="5" markerWidth="7" markerHeight="7" orient="auto-start-reverse">
            <path d="M 0 0 L 10 5 L 0 10 z" fill="#dc2626" />
          </marker>
        </defs>
        {edges.map((edge) => {
          const from = positions.get(edge.from)
          const to = positions.get(edge.to)
          if (!from || !to) return null
          const x1 = from.x + NODE_WIDTH
          const y1 = from.y + from.height / 2
          const x2 = to.x
          const y2 = to.y + to.height / 2
          const midX = (x1 + x2) / 2
          const conflicted = highlightEdges?.has(edge.id) || edge.problems.length > 0
          const disabled = edge.status === '已停用'
          const impacted = impactNodes?.has(edge.from) && impactNodes?.has(edge.to)
          const className = [
            'graph-edge',
            conflicted ? 'conflicted' : '',
            disabled ? 'disabled' : '',
            impacted ? 'impacted' : '',
          ].join(' ')
          return (
            <g key={edge.id} className={className}>
              <path
                d={`M ${x1} ${y1} C ${midX} ${y1}, ${midX} ${y2}, ${x2} ${y2}`}
                fill="none"
                markerEnd={conflicted ? 'url(#arrow-conflict)' : 'url(#arrow)'}
              />
              <text x={midX} y={(y1 + y2) / 2 - 6} textAnchor="middle" className="graph-edge-label">
                {edge.code} · {edge.source_field || '?'} → {edge.target_field || '?'}
              </text>
            </g>
          )
        })}
        {laidOut.map((node) => {
          const conflicted = highlightNodes?.has(node.key)
          const impacted = impactNodes?.has(node.key)
          const className = [
            'graph-node',
            node.kind === 'text' ? 'text-node' : '',
            conflicted ? 'conflicted' : '',
            impacted ? 'impacted' : '',
          ].join(' ')
          const shownFields = node.fields.slice(0, MAX_FIELDS)
          return (
            <g key={node.key} className={className} transform={`translate(${node.x}, ${node.y})`}>
              <rect width={NODE_WIDTH} height={node.height} rx={8} />
              <text x={12} y={20} className="graph-node-title">{node.label}</text>
              <text x={12} y={37} className="graph-node-sub">
                {node.kind === 'model' ? `${node.sub} · v${node.version}` : '自由文本 · 未绑定模型'}
              </text>
              {node.kind === 'model' &&
                shownFields.map((field, index) => {
                  const selected =
                    selectedField?.nodeKey === node.key && selectedField.field === field.name
                  return (
                    <g
                      key={field.name}
                      className={`graph-field ${selected ? 'selected' : ''}`}
                      transform={`translate(6, ${HEADER_HEIGHT + index * FIELD_HEIGHT})`}
                      onClick={() => onFieldClick?.(node, field.name)}
                    >
                      <rect width={NODE_WIDTH - 12} height={FIELD_HEIGHT - 3} rx={4} />
                      <text x={8} y={14} className="graph-field-name">{field.name}</text>
                      <text x={NODE_WIDTH - 20} y={14} textAnchor="end" className="graph-field-role">
                        {field.role}
                      </text>
                    </g>
                  )
                })}
              {node.kind === 'model' && node.fields.length > MAX_FIELDS && (
                <text x={12} y={HEADER_HEIGHT + MAX_FIELDS * FIELD_HEIGHT + 12} className="graph-node-sub">
                  … 共 {node.fields.length} 个字段
                </text>
              )}
            </g>
          )
        })}
      </svg>
    </div>
  )
}
