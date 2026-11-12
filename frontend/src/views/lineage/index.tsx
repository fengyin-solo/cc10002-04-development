import { useCallback, useEffect, useMemo, useState } from 'react'
import { useNavigate, useSearchParams } from 'react-router-dom'

import { request } from '@/api/client'
import LineageGraph from './graph'
import type {
  Conflict,
  GraphPayload,
  ImpactAnalysis,
  LineageRow,
  ModelEntry,
  PublishResult,
  SavedImpact,
  SnapshotInfo,
} from './types'

const ENDPOINT = '/api/lineage'
const MODEL_ENDPOINT = '/api/datamodel'

type TabKey = 'relations' | 'graph' | 'publish' | 'impact'

interface ModalState {
  mode: 'create' | 'edit'
  id?: number
  关系编号: string
  上游类型: 'model' | 'text'
  上游模型ID: number | ''
  上游节点: string
  来源字段: string
  下游类型: 'model' | 'text'
  下游模型ID: number | ''
  下游节点: string
  目标字段: string
  转换函数: string
}

const emptyModal: ModalState = {
  mode: 'create',
  关系编号: '',
  上游类型: 'model',
  上游模型ID: '',
  上游节点: '',
  来源字段: '',
  下游类型: 'model',
  下游模型ID: '',
  下游节点: '',
  目标字段: '',
  转换函数: '',
}

async function readJson(response: Response) {
  const payload = await response.json().catch(() => ({}))
  if (!response.ok) {
    throw new Error(payload.detail ?? `接口返回 ${response.status}`)
  }
  return payload
}

export default function LineageView() {
  const navigate = useNavigate()
  const [searchParams, setSearchParams] = useSearchParams()
  const [tab, setTab] = useState<TabKey>((searchParams.get('tab') as TabKey) || 'relations')

  const [rows, setRows] = useState<LineageRow[]>([])
  const [models, setModels] = useState<ModelEntry[]>([])
  const [snapshots, setSnapshots] = useState<SnapshotInfo[]>([])
  const [savedImpacts, setSavedImpacts] = useState<SavedImpact[]>([])
  const [graph, setGraph] = useState<GraphPayload | null>(null)
  const [graphScope, setGraphScope] = useState<'effective' | 'draft'>('effective')
  const [errorMessage, setErrorMessage] = useState('')

  const [publishResult, setPublishResult] = useState<PublishResult | null>(null)
  const [highlightNodes, setHighlightNodes] = useState<Set<string>>(new Set())
  const [highlightEdges, setHighlightEdges] = useState<Set<string>>(new Set())

  const [modal, setModal] = useState<ModalState | null>(null)
  const [snapshotDetail, setSnapshotDetail] = useState<{ version: number; relations: Record<string, unknown>[] } | null>(null)

  const [impactModelId, setImpactModelId] = useState<number | ''>('')
  const [impactField, setImpactField] = useState('')
  const [impactDirection, setImpactDirection] = useState<'downstream' | 'upstream'>('downstream')
  const [analysis, setAnalysis] = useState<ImpactAnalysis | null>(null)
  const [savedView, setSavedView] = useState<(SavedImpact & { impacts?: ImpactAnalysis['impacts']; legacy_skipped?: ImpactAnalysis['legacy_skipped'] }) | null>(null)
  const [selectedField, setSelectedField] = useState<{ nodeKey: string; field: string } | null>(null)

  const switchTab = useCallback((next: TabKey) => {
    setTab(next)
    setSearchParams((prev) => {
      const params = new URLSearchParams(prev)
      params.set('tab', next)
      return params
    })
  }, [setSearchParams])

  const loadGraph = useCallback(async (scope: 'effective' | 'draft') => {
    const payload = await readJson(await request(`${ENDPOINT}/graph?scope=${scope}`))
    setGraph(payload)
  }, [])

  const reload = useCallback(async () => {
    setErrorMessage('')
    try {
      const [rowPayload, modelPayload, snapshotPayload, impactPayload] = await Promise.all([
        readJson(await request(`${ENDPOINT}?size=200`)),
        readJson(await request(`${MODEL_ENDPOINT}?size=200`)),
        readJson(await request(`${ENDPOINT}/snapshots`)),
        readJson(await request(`${ENDPOINT}/impacts`)),
      ])
      setRows(rowPayload.items ?? [])
      setModels(modelPayload.items ?? [])
      setSnapshots(snapshotPayload.items ?? [])
      setSavedImpacts(impactPayload.items ?? [])
      await loadGraph(graphScope)
    } catch (error) {
      setErrorMessage(error instanceof Error ? error.message : '数据血缘数据读取失败')
    }
  }, [graphScope, loadGraph])

  useEffect(() => {
    void reload()
  }, [reload])

  // 从模型页或图跳转进来：?tab=impact&model=1&field=gmv 直接发起分析
  useEffect(() => {
    const model = Number(searchParams.get('model') || 0)
    const field = searchParams.get('field') || ''
    if (tab === 'impact' && model && field && models.length > 0 && !analysis && !savedView) {
      setImpactModelId(model)
      setImpactField(field)
      void runImpact(model, field, impactDirection, false)
    }
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [tab, models])

  const currentSnapshot = snapshots[0] ?? null

  const draftStats = useMemo(() => {
    const stats = { 新增: 0, 修订: 0, 停用待生效: 0, 生效中: 0, 已停用: 0 }
    for (const row of rows) {
      if (row.生效状态 === '待发布') stats.新增 += 1
      else if (row.生效状态 === '已修订待生效') stats.修订 += 1
      else if (row.生效状态 === '停用待生效') stats.停用待生效 += 1
      else if (row.生效状态 === '生效中') stats.生效中 += 1
      else stats.已停用 += 1
    }
    return stats
  }, [rows])

  const legacyCount = useMemo(() => rows.filter((row) => row.绑定状态 !== '已绑定模型').length, [rows])

  // ---------------------------------------------------------------
  // 关系维护
  // ---------------------------------------------------------------
  function openEdit(row: LineageRow) {
    setModal({
      mode: 'edit',
      id: row.id,
      关系编号: row.关系编号,
      上游类型: row.上游模型ID ? 'model' : 'text',
      上游模型ID: row.上游模型ID ?? '',
      上游节点: row.上游节点,
      来源字段: row.来源字段,
      下游类型: row.下游模型ID ? 'model' : 'text',
      下游模型ID: row.下游模型ID ?? '',
      下游节点: row.下游节点,
      目标字段: row.目标字段,
      转换函数: row.转换函数 ?? '',
    })
  }

  async function saveModal() {
    if (!modal) return
    const values: Record<string, unknown> = {
      关系编号: modal.关系编号,
      来源字段: modal.来源字段,
      目标字段: modal.目标字段,
      转换函数: modal.转换函数,
      上游模型ID: modal.上游类型 === 'model' && modal.上游模型ID !== '' ? modal.上游模型ID : null,
      下游模型ID: modal.下游类型 === 'model' && modal.下游模型ID !== '' ? modal.下游模型ID : null,
    }
    if (modal.上游类型 === 'text') values.上游节点 = modal.上游节点
    if (modal.下游类型 === 'text') values.下游节点 = modal.下游节点
    try {
      const response = modal.mode === 'create'
        ? await request(ENDPOINT, { method: 'POST', body: JSON.stringify({ values }) })
        : await request(`${ENDPOINT}/${modal.id}`, { method: 'PUT', body: JSON.stringify({ values }) })
      const payload = await readJson(response)
      if (!payload.ok) throw new Error(payload.message)
      setModal(null)
      await reload()
    } catch (error) {
      setErrorMessage(error instanceof Error ? error.message : '血缘关系保存失败')
    }
  }

  async function runRowAction(row: LineageRow, action: string) {
    try {
      const payload = await readJson(await request(`${ENDPOINT}/${row.id}/actions`, {
        method: 'POST',
        body: JSON.stringify({ values: { action } }),
      }))
      if (!payload.ok) throw new Error(payload.message)
      await reload()
    } catch (error) {
      setErrorMessage(error instanceof Error ? error.message : '数据血缘操作失败')
    }
  }

  async function removeRow(row: LineageRow) {
    try {
      const payload = await readJson(await request(`${ENDPOINT}/${row.id}`, { method: 'DELETE' }))
      if (!payload.ok) throw new Error(payload.message)
      await reload()
    } catch (error) {
      setErrorMessage(error instanceof Error ? error.message : '血缘关系删除失败')
    }
  }

  // ---------------------------------------------------------------
  // 发布
  // ---------------------------------------------------------------
  async function runPublish() {
    setErrorMessage('')
    try {
      const baseModelVersions: Record<string, number> = {}
      for (const model of models) baseModelVersions[String(model.id)] = model.version
      const payload: PublishResult = await readJson(await request(`${ENDPOINT}/publish`, {
        method: 'POST',
        body: JSON.stringify({
          base_snapshot_version: currentSnapshot?.version ?? 0,
          base_model_versions: baseModelVersions,
          remark: '',
        }),
      }))
      setPublishResult(payload)
      if (payload.ok) {
        setHighlightNodes(new Set())
        setHighlightEdges(new Set())
        await reload()
      } else if (payload.stage === '校验失败') {
        locateConflicts(payload.conflicts)
      }
    } catch (error) {
      setErrorMessage(error instanceof Error ? error.message : '血缘发布失败')
    }
  }

  function locateConflicts(conflicts: Conflict[]) {
    const nodeKeys = new Set<string>()
    const relationCodes = new Set<string>()
    for (const conflict of conflicts) {
      for (const key of conflict.node_keys ?? []) nodeKeys.add(key)
      for (const code of conflict.relations ?? []) relationCodes.add(code)
      if (conflict.relation) relationCodes.add(conflict.relation)
    }
    // 边 id 就是 e{关系id}，按关系列表换算，与当前图作用域无关
    const edgeIds = new Set<string>()
    for (const row of rows) {
      if (relationCodes.has(row.关系编号)) edgeIds.add(`e${row.id}`)
    }
    setHighlightNodes(nodeKeys)
    setHighlightEdges(edgeIds)
    setGraphScope('draft')
    switchTab('graph')
  }

  async function checkDraft() {
    await loadGraph('draft')
    setGraphScope('draft')
    switchTab('graph')
  }

  async function openSnapshot(version: number) {
    const payload = await readJson(await request(`${ENDPOINT}/snapshots/${version}`))
    setSnapshotDetail(payload)
  }

  // ---------------------------------------------------------------
  // 影响分析
  // ---------------------------------------------------------------
  async function runImpact(modelId: number, field: string, direction: string, save: boolean) {
    setErrorMessage('')
    try {
      const payload = await readJson(await request(`${ENDPOINT}/impact`, {
        method: 'POST',
        body: JSON.stringify({ model_id: modelId, field, direction, save }),
      }))
      setAnalysis(payload.analysis)
      setSavedView(null)
      if (save) {
        const impactPayload = await readJson(await request(`${ENDPOINT}/impacts`))
        setSavedImpacts(impactPayload.items ?? [])
      }
    } catch (error) {
      setErrorMessage(error instanceof Error ? error.message : '影响分析失败')
    }
  }

  async function openSavedImpact(id: number) {
    const payload = await readJson(await request(`${ENDPOINT}/impacts/${id}`))
    setSavedView(payload)
    setAnalysis(null)
  }

  function onGraphFieldClick(nodeKey: string, modelId: number | null, field: string) {
    if (!modelId) return
    setSelectedField({ nodeKey, field })
    setImpactModelId(modelId)
    setImpactField(field)
  }

  const impactModel = models.find((model) => model.id === impactModelId) ?? null
  const shownAnalysis = analysis ?? null

  // ---------------------------------------------------------------
  // 渲染
  // ---------------------------------------------------------------
  return (
    <section className="page" data-module="lineage">
      <header className="page-head">
        <div>
          <h2>数据血缘管理</h2>
          <p className="page-desc">
            血缘关系先落草稿、整组发布成生效快照；从模型字段出发沿字段链展开影响路径，可回到模型定义查看受影响字段。
          </p>
        </div>
        <div className="page-actions">
          <button className="btn primary" type="button" onClick={() => setModal({ ...emptyModal })}>登记血缘关系</button>
          <button className="btn" type="button" onClick={() => window.open(`${ENDPOINT}/export`, '_blank')}>导出数据血缘清单</button>
        </div>
      </header>

      <div className="stat-row">
        <article className="stat-card">
          <span className="stat-label">血缘关系总数</span>
          <strong className="stat-value">{rows.length}</strong>
        </article>
        <article className="stat-card">
          <span className="stat-label">当前生效快照</span>
          <strong className="stat-value">{currentSnapshot ? `v${currentSnapshot.version}` : '—'}</strong>
        </article>
        <article className="stat-card">
          <span className="stat-label">待发布变更</span>
          <strong className="stat-value">{draftStats.新增 + draftStats.修订 + draftStats.停用待生效}</strong>
        </article>
        <article className="stat-card">
          <span className="stat-label">自由文本关系</span>
          <strong className="stat-value">{legacyCount}</strong>
        </article>
      </div>

      <div className="tab-bar">
        {([['relations', '关系列表'], ['graph', '影响图'], ['publish', '发布中心'], ['impact', '影响分析']] as [TabKey, string][]).map(([key, label]) => (
          <button key={key} type="button" className={`tab-item ${tab === key ? 'active' : ''}`} onClick={() => switchTab(key)}>
            {label}
          </button>
        ))}
      </div>

      {tab === 'relations' && (
        <table className="data-table">
          <thead>
            <tr>
              <th>关系编号</th><th>上游节点</th><th>下游节点</th><th>来源字段</th><th>目标字段</th>
              <th>转换函数</th><th>绑定状态</th><th>生效状态</th><th>问题</th><th>可执行动作</th>
            </tr>
          </thead>
          <tbody>
            {rows.map((row) => (
              <tr key={row.id}>
                <td>{row.关系编号}</td>
                <td>{row.上游节点}</td>
                <td>{row.下游节点}</td>
                <td>{row.来源字段 || '—'}</td>
                <td>{row.目标字段 || '—'}</td>
                <td>{row.转换函数 || '—'}</td>
                <td>
                  <span className={`badge ${row.绑定状态 === '已绑定模型' ? 'ok' : 'muted'}`}>{row.绑定状态}</span>
                </td>
                <td>
                  <span className={`badge ${row.生效状态 === '生效中' ? 'ok' : row.生效状态 === '已停用' ? 'muted' : 'warn'}`}>
                    {row.生效状态}
                  </span>
                </td>
                <td>
                  {row.问题.length > 0
                    ? row.问题.map((problem) => <span key={problem} className="badge danger">{problem}</span>)
                    : '—'}
                </td>
                <td className="row-actions">
                  <button className="link" type="button" onClick={() => openEdit(row)}>修订</button>
                  {row.status === '已停用' ? (
                    <button className="link" type="button" onClick={() => void runRowAction(row, '启用关系')}>启用关系</button>
                  ) : (
                    <button className="link" type="button" onClick={() => void runRowAction(row, '停用关系')}>停用关系</button>
                  )}
                  <button className="link danger" type="button" onClick={() => void removeRow(row)}>删除</button>
                </td>
              </tr>
            ))}
            {rows.length === 0 && (
              <tr><td className="empty-state" colSpan={10}>暂无数据血缘数据，可先登记血缘关系</td></tr>
            )}
          </tbody>
        </table>
      )}

      {tab === 'graph' && (
        <div className="panel">
          <div className="panel-head">
            <div className="scope-toggle">
              <button type="button" className={graphScope === 'effective' ? 'active' : ''} onClick={() => { setGraphScope('effective'); void loadGraph('effective') }}>
                生效快照{currentSnapshot ? ` v${currentSnapshot.version}` : ''}
              </button>
              <button type="button" className={graphScope === 'draft' ? 'active' : ''} onClick={() => { setGraphScope('draft'); void loadGraph('draft') }}>
                草稿（含未发布变更）
              </button>
            </div>
            <span className="hint">点击模型节点上的字段，可对该字段发起影响分析；自由文本节点未绑定模型，不参与字段级分析。</span>
          </div>
          {graphScope === 'draft' && graph && graph.conflicts.length > 0 && (
            <div className="conflict-bar">
              {graph.conflicts.map((conflict, index) => (
                <span key={index} className="badge danger">{conflict.type}：{conflict.message}</span>
              ))}
            </div>
          )}
          {graph && (
            <LineageGraph
              nodes={graph.nodes}
              edges={graph.edges}
              highlightNodes={highlightNodes}
              highlightEdges={highlightEdges}
              selectedField={selectedField}
              onFieldClick={(node, field) => onGraphFieldClick(node.key, node.model_id, field)}
            />
          )}
          {selectedField && (
            <div className="panel-foot">
              <span>
                已选字段：<strong>{selectedField.nodeKey}.{selectedField.field}</strong>
              </span>
              <button className="btn primary" type="button" onClick={() => { switchTab('impact'); void runImpact(Number(selectedField.nodeKey.split(':')[1]), selectedField.field, 'downstream', false) }}>
                对该字段做下游影响分析
              </button>
            </div>
          )}
        </div>
      )}

      {tab === 'publish' && (
        <div className="panel">
          <div className="publish-grid">
            <div className="panel-block">
              <h3>当前生效快照</h3>
              {currentSnapshot ? (
                <p>v{currentSnapshot.version} · {currentSnapshot.created_at} · {currentSnapshot.relation_count} 条关系</p>
              ) : <p>尚未发布</p>}
              <h3>草稿待发布变更</h3>
              <p>
                新增 {draftStats.新增} · 修订 {draftStats.修订} · 停用待生效 {draftStats.停用待生效} · 生效中 {draftStats.生效中}
              </p>
              <h3>发布基准（并发裁决依据）</h3>
              <p>快照基准：v{currentSnapshot?.version ?? 0}</p>
              <div className="chip-row">
                {models.map((model) => (
                  <span key={model.id} className="chip">{model.模型编号} v{model.version}</span>
                ))}
              </div>
              <div className="row-actions" style={{ marginTop: 12 }}>
                <button className="btn" type="button" onClick={() => void checkDraft()}>校验草稿</button>
                <button className="btn primary" type="button" onClick={() => void runPublish()}>整组发布</button>
              </div>
            </div>
            <div className="panel-block">
              <h3>发布结果</h3>
              {!publishResult && <p className="hint">尚未发起发布。自环、跨多条关系形成的环、缺失模型字段都会让整组失败，失败时旧图继续生效。</p>}
              {publishResult && (
                <div className={publishResult.ok ? 'notice ok' : 'notice danger'}>
                  <p><strong>{publishResult.stage}</strong>：{publishResult.message}</p>
                  {publishResult.conflicts.map((conflict, index) => (
                    <div key={index} className="conflict-item">
                      <span className="badge danger">{conflict.type}</span>
                      <span className="conflict-message">{conflict.message}</span>
                      <span className="row-actions">
                        {conflict.relation_id != null && (
                          <button className="link" type="button" onClick={() => {
                            const row = rows.find((item) => item.id === conflict.relation_id)
                            if (row) openEdit(row)
                          }}>修订关系</button>
                        )}
                        <button className="link" type="button" onClick={() => locateConflicts([conflict])}>图上定位</button>
                      </span>
                    </div>
                  ))}
                </div>
              )}
            </div>
          </div>
          <div className="panel-block">
            <h3>快照历史</h3>
            <table className="data-table">
              <thead>
                <tr><th>版本</th><th>发布时间</th><th>关系数</th><th>模型版本</th><th>备注</th><th>操作</th></tr>
              </thead>
              <tbody>
                {snapshots.map((snapshot) => (
                  <tr key={snapshot.version}>
                    <td>v{snapshot.version}</td>
                    <td>{snapshot.created_at}</td>
                    <td>{snapshot.relation_count}</td>
                    <td>{Object.entries(snapshot.model_versions).map(([id, version]) => `模型${id}:v${version}`).join('，') || '—'}</td>
                    <td>{snapshot.remark || '—'}</td>
                    <td><button className="link" type="button" onClick={() => void openSnapshot(snapshot.version)}>查看</button></td>
                  </tr>
                ))}
              </tbody>
            </table>
            {snapshotDetail && (
              <div className="panel-block">
                <h3>快照 v{snapshotDetail.version} 的关系内容</h3>
                <table className="data-table">
                  <thead>
                    <tr><th>关系编号</th><th>上游节点</th><th>来源字段</th><th>下游节点</th><th>目标字段</th><th>绑定形态</th></tr>
                  </thead>
                  <tbody>
                    {snapshotDetail.relations.map((rel) => (
                      <tr key={String(rel.id)}>
                        <td>{String(rel.关系编号)}</td>
                        <td>{String(rel.上游节点)}</td>
                        <td>{String(rel.来源字段)}</td>
                        <td>{String(rel.下游节点)}</td>
                        <td>{String(rel.目标字段)}</td>
                        <td>{String(rel.绑定形态)}</td>
                      </tr>
                    ))}
                  </tbody>
                </table>
                <button className="btn ghost" type="button" onClick={() => setSnapshotDetail(null)}>收起</button>
              </div>
            )}
          </div>
        </div>
      )}

      {tab === 'impact' && (
        <div className="panel">
          <div className="filter-bar">
            <label className="filter-item">
              <span>模型</span>
              <select
                value={impactModelId}
                onChange={(event) => {
                  const id = event.target.value ? Number(event.target.value) : ''
                  setImpactModelId(id)
                  setImpactField('')
                  setAnalysis(null)
                  setSavedView(null)
                }}
              >
                <option value="">选择模型</option>
                {models.map((model) => (
                  <option key={model.id} value={model.id}>{model.模型名称}（{model.事实表名}）</option>
                ))}
              </select>
            </label>
            <label className="filter-item">
              <span>字段</span>
              <select value={impactField} onChange={(event) => { setImpactField(event.target.value); setAnalysis(null); setSavedView(null) }}>
                <option value="">选择字段</option>
                {(impactModel?.fields ?? []).map((field) => (
                  <option key={field.name} value={field.name}>{field.name}（{field.role}）</option>
                ))}
              </select>
            </label>
            <label className="filter-item">
              <span>方向</span>
              <select value={impactDirection} onChange={(event) => setImpactDirection(event.target.value as 'downstream' | 'upstream')}>
                <option value="downstream">下游影响</option>
                <option value="upstream">上游溯源</option>
              </select>
            </label>
            <button className="btn primary" type="button" disabled={!impactModelId || !impactField}
              onClick={() => void runImpact(Number(impactModelId), impactField, impactDirection, false)}>
              开始分析
            </button>
            <button className="btn" type="button" disabled={!impactModelId || !impactField}
              onClick={() => void runImpact(Number(impactModelId), impactField, impactDirection, true)}>
              分析并保存结论
            </button>
          </div>

          {savedView && (
            <div className="notice muted">
              <p>
                正在查看已保存结论「{savedView.title}」· 保存于 {savedView.created_at} · 基于快照 v{savedView.snapshot_version}
                {savedView.stale && <span className="badge warn">已过期：当前生效快照 v{savedView.current_snapshot_version}</span>}
                <button className="link" type="button" onClick={() => setSavedView(null)}>返回实时分析</button>
              </p>
            </div>
          )}

          {(shownAnalysis || savedView) && (
            <ImpactResult
              analysis={shownAnalysis ?? (savedView as unknown as ImpactAnalysis)}
              onOpenModel={(modelId, field) => navigate(`/datamodel?model=${modelId}&field=${field}`)}
            />
          )}

          <div className="panel-block">
            <h3>已保存的影响结论</h3>
            <table className="data-table">
              <thead>
                <tr><th>标题</th><th>起点</th><th>方向</th><th>基于快照</th><th>保存时间</th><th>状态</th><th>操作</th></tr>
              </thead>
              <tbody>
                {savedImpacts.map((item) => (
                  <tr key={item.id}>
                    <td>{item.title}</td>
                    <td>{item.root.label}.{item.root.field}</td>
                    <td>{item.direction === 'downstream' ? '下游影响' : '上游溯源'}</td>
                    <td>v{item.snapshot_version}</td>
                    <td>{item.created_at}</td>
                    <td>{item.stale
                      ? <span className="badge warn">已过期（当前 v{item.current_snapshot_version}）</span>
                      : <span className="badge ok">当前版本</span>}</td>
                    <td className="row-actions">
                      <button className="link" type="button" onClick={() => void openSavedImpact(item.id)}>查看</button>
                      <button className="link" type="button" onClick={() => navigate(`/datamodel?model=${item.root.model_id}&field=${item.root.field}`)}>查看模型</button>
                    </td>
                  </tr>
                ))}
                {savedImpacts.length === 0 && (
                  <tr><td className="empty-state" colSpan={7}>还没有保存过影响结论</td></tr>
                )}
              </tbody>
            </table>
          </div>
        </div>
      )}

      {modal && (
        <div className="modal-mask" onClick={() => setModal(null)}>
          <div className="modal" onClick={(event) => event.stopPropagation()}>
            <h3>{modal.mode === 'create' ? '登记血缘关系' : `修订血缘关系 ${modal.关系编号}`}</h3>
            <div className="modal-grid">
              <label className="filter-item">
                <span>关系编号（留空自动生成）</span>
                <input value={modal.关系编号} disabled={modal.mode === 'edit'}
                  onChange={(event) => setModal({ ...modal, 关系编号: event.target.value })} />
              </label>
              <label className="filter-item">
                <span>转换函数</span>
                <input value={modal.转换函数} onChange={(event) => setModal({ ...modal, 转换函数: event.target.value })} />
              </label>
              {(['上游', '下游'] as const).map((side) => {
                const typeKey = `${side}类型` as '上游类型' | '下游类型'
                const modelKey = `${side}模型ID` as '上游模型ID' | '下游模型ID'
                const nodeKey = `${side}节点` as '上游节点' | '下游节点'
                const fieldKey = side === '上游' ? '来源字段' : '目标字段'
                const sideModel = models.find((model) => model.id === modal[modelKey])
                return (
                  <div key={side} className="modal-side">
                    <label className="filter-item">
                      <span>{side}类型</span>
                      <select value={modal[typeKey]} onChange={(event) => setModal({ ...modal, [typeKey]: event.target.value })}>
                        <option value="model">绑定模型</option>
                        <option value="text">自由文本</option>
                      </select>
                    </label>
                    {modal[typeKey] === 'model' ? (
                      <>
                        <label className="filter-item">
                          <span>{side}模型</span>
                          <select value={modal[modelKey]} onChange={(event) => setModal({ ...modal, [modelKey]: event.target.value ? Number(event.target.value) : '' })}>
                            <option value="">选择模型</option>
                            {models.map((model) => (
                              <option key={model.id} value={model.id}>{model.模型名称}（{model.事实表名}）</option>
                            ))}
                          </select>
                        </label>
                        <label className="filter-item">
                          <span>{side === '上游' ? '来源字段' : '目标字段'}</span>
                          <select value={modal[fieldKey]} onChange={(event) => setModal({ ...modal, [fieldKey]: event.target.value })}>
                            <option value="">选择字段</option>
                            {(sideModel?.fields ?? []).map((field) => (
                              <option key={field.name} value={field.name}>{field.name}（{field.role}）</option>
                            ))}
                          </select>
                        </label>
                      </>
                    ) : (
                      <>
                        <label className="filter-item">
                          <span>{side}节点（自由文本）</span>
                          <input value={modal[nodeKey]} onChange={(event) => setModal({ ...modal, [nodeKey]: event.target.value })} />
                        </label>
                        <label className="filter-item">
                          <span>{side === '上游' ? '来源字段' : '目标字段'}</span>
                          <input value={modal[fieldKey]} onChange={(event) => setModal({ ...modal, [fieldKey]: event.target.value })} />
                        </label>
                      </>
                    )}
                  </div>
                )
              })}
            </div>
            <div className="row-actions modal-actions">
              <button className="btn primary" type="button" onClick={() => void saveModal()}>保存草稿</button>
              <button className="btn ghost" type="button" onClick={() => setModal(null)}>取消</button>
            </div>
          </div>
        </div>
      )}

      <footer className="page-foot">
        <span>共 {rows.length} 条数据血缘记录</span>
        {errorMessage && <span className="error-text">{errorMessage}</span>}
      </footer>
    </section>
  )
}

function ImpactResult({ analysis, onOpenModel }: {
  analysis: ImpactAnalysis | null
  onOpenModel: (modelId: number, field: string) => void
}) {
  if (!analysis || !analysis.root) return null
  const impacts = analysis.impacts ?? []
  return (
    <div className="panel-block">
      <h3>
        {analysis.root.label}.{analysis.root.field} 的{analysis.direction === 'downstream' ? '下游影响' : '上游溯源'}
        <span className="hint">（基于快照 v{analysis.snapshot_version}）</span>
      </h3>
      <div className="stat-row">
        <article className="stat-card"><span className="stat-label">直接影响</span><strong className="stat-value">{analysis.summary?.直接 ?? 0}</strong></article>
        <article className="stat-card"><span className="stat-label">间接影响</span><strong className="stat-value">{analysis.summary?.间接 ?? 0}</strong></article>
        <article className="stat-card"><span className="stat-label">多路径重复</span><strong className="stat-value">{analysis.summary?.多路径 ?? 0}</strong></article>
      </div>
      <table className="data-table">
        <thead>
          <tr><th>受影响模型</th><th>字段</th><th>类型</th><th>路径数</th><th>示例路径</th><th>操作</th></tr>
        </thead>
        <tbody>
          {impacts.map((item) => (
            <tr key={`${item.node_key}.${item.field}`}>
              <td>{item.label}（{item.table}）</td>
              <td>{item.field}</td>
              <td>
                <span className={`badge ${item.kind === '直接' ? 'ok' : 'warn'}`}>{item.kind}</span>
                {item.multi && <span className="badge danger">多路径重复</span>}
              </td>
              <td>{item.paths}</td>
              <td className="path-cell">
                {item.samples.length > 0 && (
                  <details>
                    <summary>{item.samples[0]}</summary>
                    {item.samples.slice(1).map((sample, index) => (
                      <div key={index} className="path-sample">{sample}</div>
                    ))}
                  </details>
                )}
              </td>
              <td>
                {item.model_id != null && (
                  <button className="link" type="button" onClick={() => onOpenModel(item.model_id as number, item.field)}>查看模型</button>
                )}
              </td>
            </tr>
          ))}
          {impacts.length === 0 && (
            <tr><td className="empty-state" colSpan={6}>该字段在生效图中没有{analysis.direction === 'downstream' ? '下游' : '上游'}影响</td></tr>
          )}
        </tbody>
      </table>
      {(analysis.legacy_skipped ?? []).length > 0 && (
        <div className="notice muted">
          <p>以下自由文本关系未纳入字段级分析（未绑定模型，无法确认字段）：</p>
          {analysis.legacy_skipped.map((item) => (
            <span key={item.关系编号} className="chip">{item.关系编号}：{item.上游节点} → {item.下游节点}</span>
          ))}
        </div>
      )}
    </div>
  )
}
