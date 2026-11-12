import { useCallback, useEffect, useState } from 'react'
import { useNavigate, useSearchParams } from 'react-router-dom'

import { request } from '@/api/client'
import type { Conflict, ImpactAnalysis, ModelEntry, ModelField, ModelVersion } from '../lineage/types'

const ENDPOINT = '/api/datamodel'
const LINEAGE_ENDPOINT = '/api/lineage'

const actions = ['提交审核', '生效发布', '废弃下线']
const FIELD_ROLES = ['维度键', '度量', '分区', '普通']

interface FieldUpdateResult {
  ok: boolean
  stage: string
  message: string
  conflicts: Conflict[]
  current_version?: number
}

async function readJson(response: Response) {
  const payload = await response.json().catch(() => ({}))
  if (!response.ok) {
    throw new Error(payload.detail ?? `接口返回 ${response.status}`)
  }
  return payload
}

export default function DatamodelView() {
  const navigate = useNavigate()
  const [searchParams] = useSearchParams()
  const [rows, setRows] = useState<ModelEntry[]>([])
  const [total, setTotal] = useState(0)
  const [errorMessage, setErrorMessage] = useState('')
  const [keyword, setKeyword] = useState('')

  const [selected, setSelected] = useState<ModelEntry | null>(null)
  const [versions, setVersions] = useState<ModelVersion[]>([])
  const [editing, setEditing] = useState(false)
  const [draftFields, setDraftFields] = useState<ModelField[]>([])
  const [updateResult, setUpdateResult] = useState<FieldUpdateResult | null>(null)
  const [impactField, setImpactField] = useState('')
  const [impact, setImpact] = useState<ImpactAnalysis | null>(null)

  const reload = useCallback(async () => {
    setErrorMessage('')
    const query = keyword ? `?keyword=${encodeURIComponent(keyword)}&size=200` : '?size=200'
    try {
      const payload = await readJson(await request(`${ENDPOINT}${query}`))
      setRows(payload.items ?? [])
      setTotal(payload.total ?? 0)
    } catch (error) {
      setErrorMessage(error instanceof Error ? error.message : '数据建模列表读取失败')
    }
  }, [keyword])

  useEffect(() => {
    void reload()
  }, [reload])

  const openModel = useCallback(async (modelId: number, field?: string) => {
    try {
      const detail: ModelEntry = await readJson(await request(`${ENDPOINT}/${modelId}`))
      setSelected(detail)
      setEditing(false)
      setUpdateResult(null)
      const versionPayload = await readJson(await request(`${ENDPOINT}/${modelId}/versions`))
      setVersions(versionPayload.items ?? [])
      if (field) {
        setImpactField(field)
        try {
          const impactPayload = await readJson(await request(`${LINEAGE_ENDPOINT}/impact`, {
            method: 'POST',
            body: JSON.stringify({ model_id: modelId, field, direction: 'downstream', save: false }),
          }))
          setImpact(impactPayload.analysis ?? null)
        } catch {
          // 字段可能刚被删掉或暂无血缘：影响面板置空，错误交给页脚提示
          setImpact(null)
          setErrorMessage(`字段「${field}」的影响分析暂不可用，可能已不在生效图中`)
        }
      } else {
        setImpactField('')
        setImpact(null)
      }
    } catch (error) {
      setErrorMessage(error instanceof Error ? error.message : '模型详情读取失败')
    }
  }, [])

  // 从血缘页跳转：?model=1&field=gmv → 打开模型详情并展示该字段影响
  useEffect(() => {
    const model = Number(searchParams.get('model') || 0)
    const field = searchParams.get('field') || ''
    if (model) {
      void openModel(model, field || undefined)
    }
  }, [searchParams, openModel])

  async function runAction(action: string, row: ModelEntry) {
    setErrorMessage('')
    try {
      const payload = await readJson(await request(`${ENDPOINT}/${row.id}/actions`, {
        method: 'POST',
        body: JSON.stringify({ values: { action } }),
      }))
      if (!payload.ok) throw new Error(payload.message)
      await reload()
      if (selected?.id === row.id) await openModel(row.id)
    } catch (error) {
      setErrorMessage(error instanceof Error ? error.message : '数据建模操作失败')
    }
  }

  function startEdit() {
    if (!selected) return
    setDraftFields(selected.fields.map((field) => ({ ...field })))
    setUpdateResult(null)
    setEditing(true)
  }

  async function saveFields() {
    if (!selected) return
    try {
      const payload: FieldUpdateResult = await readJson(await request(`${ENDPOINT}/${selected.id}/fields`, {
        method: 'PUT',
        body: JSON.stringify({ base_version: selected.version, fields: draftFields, remark: '' }),
      }))
      setUpdateResult(payload)
      if (payload.ok) {
        await reload()
        await openModel(selected.id, impactField || undefined)
        setEditing(false)
      }
    } catch (error) {
      setErrorMessage(error instanceof Error ? error.message : '模型字段保存失败')
    }
  }

  async function runImpact(field: string) {
    if (!selected) return
    setImpactField(field)
    const payload = await readJson(await request(`${LINEAGE_ENDPOINT}/impact`, {
      method: 'POST',
      body: JSON.stringify({ model_id: selected.id, field, direction: 'downstream', save: false }),
    }))
    setImpact(payload.analysis ?? null)
  }

  return (
    <section className="page" data-module="datamodel">
      <header className="page-head">
        <div>
          <h2>数据建模管理</h2>
          <p className="page-desc">
            模型字段版本化管理；字段变更只接受基于当前版本的修改，且不能打断生效血缘快照。
          </p>
        </div>
        <div className="page-actions">
          <button className="btn" type="button" onClick={() => window.open(`${ENDPOINT}/export`, '_blank')}>导出数据建模清单</button>
        </div>
      </header>

      <form className="filter-bar" onSubmit={(event) => { event.preventDefault(); void reload() }}>
        <label className="filter-item">
          <span>模型编号</span>
          <input value={keyword} placeholder="按模型编号检索" onChange={(event) => setKeyword(event.target.value)} />
        </label>
        <button className="btn" type="submit">查询</button>
        <button className="btn ghost" type="button" onClick={() => setKeyword('')}>重置条件</button>
      </form>

      <table className="data-table">
        <thead>
          <tr>
            <th>模型编号</th><th>模型名称</th><th>事实表名</th><th>关联维度</th><th>度量列定义</th>
            <th>分区字段</th><th>刷新周期</th><th>状态</th><th>版本</th><th>可执行动作</th>
          </tr>
        </thead>
        <tbody>
          {rows.map((row) => (
            <tr key={row.id} className={selected?.id === row.id ? 'row-selected' : ''}>
              <td>{row.模型编号}</td>
              <td>{row.模型名称}</td>
              <td>{row.事实表名}</td>
              <td>{row.关联维度}</td>
              <td>{row.度量列定义}</td>
              <td>{row.分区字段}</td>
              <td>{row.刷新周期}</td>
              <td><span className={`badge ${row.status === '已生效' ? 'ok' : row.status === '已废弃' ? 'muted' : 'warn'}`}>{row.status}</span></td>
              <td>v{row.version}</td>
              <td className="row-actions">
                <button className="link" type="button" onClick={() => void openModel(row.id)}>详情</button>
                {actions.map((action) => (
                  <button key={action} className="link" type="button" onClick={() => void runAction(action, row)}>{action}</button>
                ))}
              </td>
            </tr>
          ))}
          {rows.length === 0 && (
            <tr><td className="empty-state" colSpan={10}>暂无数据建模数据</td></tr>
          )}
        </tbody>
      </table>

      {selected && (
        <div className="panel">
          <div className="panel-head">
            <h3>{selected.模型名称}（{selected.事实表名}）· 当前 v{selected.version}</h3>
            <div className="row-actions">
              <button className="btn" type="button" onClick={() => navigate(`/lineage?tab=graph`)}>在血缘图中查看</button>
              {!editing && <button className="btn primary" type="button" onClick={startEdit}>编辑字段</button>}
              <button className="btn ghost" type="button" onClick={() => setSelected(null)}>收起</button>
            </div>
          </div>

          {updateResult && !updateResult.ok && (
            <div className="notice danger">
              <p><strong>{updateResult.stage}</strong>：{updateResult.message}</p>
              {updateResult.conflicts.map((conflict, index) => (
                <div key={index} className="conflict-item">
                  <span className="badge danger">{conflict.type}</span>
                  <span className="conflict-message">{conflict.message}</span>
                  <button className="link" type="button" onClick={() => navigate('/lineage?tab=publish')}>前往血缘页修订</button>
                </div>
              ))}
            </div>
          )}
          {updateResult?.ok && <div className="notice ok"><p>{updateResult.message}</p></div>}

          <table className="data-table">
            <thead>
              <tr><th>字段名</th><th>类型</th><th>角色</th><th>血缘影响</th>{editing && <th>操作</th>}</tr>
            </thead>
            <tbody>
              {(editing ? draftFields : selected.fields).map((field, index) => (
                <tr key={editing ? index : field.name} className={impactField === field.name ? 'row-selected' : ''}>
                  {editing ? (
                    <>
                      <td><input value={field.name} onChange={(event) => {
                        const next = [...draftFields]
                        next[index] = { ...next[index], name: event.target.value }
                        setDraftFields(next)
                      }} /></td>
                      <td><input value={field.type} onChange={(event) => {
                        const next = [...draftFields]
                        next[index] = { ...next[index], type: event.target.value }
                        setDraftFields(next)
                      }} /></td>
                      <td>
                        <select value={field.role} onChange={(event) => {
                          const next = [...draftFields]
                          next[index] = { ...next[index], role: event.target.value }
                          setDraftFields(next)
                        }}>
                          {FIELD_ROLES.map((role) => <option key={role} value={role}>{role}</option>)}
                        </select>
                      </td>
                      <td>—</td>
                      <td>
                        <button className="link danger" type="button" onClick={() => setDraftFields(draftFields.filter((_, i) => i !== index))}>移除</button>
                      </td>
                    </>
                  ) : (
                    <>
                      <td>{field.name}</td>
                      <td>{field.type}</td>
                      <td><span className="badge muted">{field.role}</span></td>
                      <td><button className="link" type="button" onClick={() => void runImpact(field.name)}>查看下游影响</button></td>
                    </>
                  )}
                </tr>
              ))}
            </tbody>
          </table>
          {editing && (
            <div className="row-actions" style={{ marginTop: 8 }}>
              <button className="btn" type="button" onClick={() => setDraftFields([...draftFields, { name: '', type: 'string', role: '普通' }])}>添加字段</button>
              <button className="btn primary" type="button" onClick={() => void saveFields()}>保存字段（基于 v{selected.version}）</button>
              <button className="btn ghost" type="button" onClick={() => setEditing(false)}>取消</button>
            </div>
          )}

          {impact && (
            <div className="panel-block">
              <h3>
                字段 {impact.root.label}.{impact.root.field} 的下游影响
                <span className="hint">（基于血缘快照 v{impact.snapshot_version}）</span>
                <button className="link" type="button" onClick={() => navigate(`/lineage?tab=impact&model=${selected.id}&field=${impactField}`)}>到血缘页展开</button>
              </h3>
              <div className="stat-row">
                <article className="stat-card"><span className="stat-label">直接影响</span><strong className="stat-value">{impact.summary.直接}</strong></article>
                <article className="stat-card"><span className="stat-label">间接影响</span><strong className="stat-value">{impact.summary.间接}</strong></article>
                <article className="stat-card"><span className="stat-label">多路径重复</span><strong className="stat-value">{impact.summary.多路径}</strong></article>
              </div>
              <table className="data-table">
                <thead>
                  <tr><th>受影响模型</th><th>字段</th><th>类型</th><th>路径数</th><th>示例路径</th></tr>
                </thead>
                <tbody>
                  {impact.impacts.map((item) => (
                    <tr key={`${item.node_key}.${item.field}`}>
                      <td>{item.label}（{item.table}）</td>
                      <td>{item.field}</td>
                      <td>
                        <span className={`badge ${item.kind === '直接' ? 'ok' : 'warn'}`}>{item.kind}</span>
                        {item.multi && <span className="badge danger">多路径重复</span>}
                      </td>
                      <td>{item.paths}</td>
                      <td className="path-cell">{item.samples[0] ?? '—'}</td>
                    </tr>
                  ))}
                  {impact.impacts.length === 0 && (
                    <tr><td className="empty-state" colSpan={5}>该字段在生效血缘图中没有下游影响</td></tr>
                  )}
                </tbody>
              </table>
            </div>
          )}

          <div className="panel-block">
            <h3>版本历史</h3>
            <table className="data-table">
              <thead>
                <tr><th>版本</th><th>时间</th><th>基准版本</th><th>删除字段</th><th>字段数</th><th>备注</th></tr>
              </thead>
              <tbody>
                {versions.map((version) => (
                  <tr key={version.version}>
                    <td>v{version.version}</td>
                    <td>{version.time}</td>
                    <td>{version.base_version != null ? `v${version.base_version}` : '—'}</td>
                    <td>{version.removed && version.removed.length > 0 ? version.removed.join('、') : '—'}</td>
                    <td>{version.fields?.length ?? 0}</td>
                    <td>{version.remark || '—'}</td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        </div>
      )}

      <footer className="page-foot">
        <span>共 {total} 条数据建模记录</span>
        {errorMessage && <span className="error-text">{errorMessage}</span>}
      </footer>
    </section>
  )
}
