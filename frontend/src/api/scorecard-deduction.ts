import { request } from './client'
import type { Contribution, DailyReport, DeductionView, ReviewItem } from '@/domain/dq_scorecard_deduction'

async function postJson<T>(path: string, values: Record<string, unknown> = {}): Promise<T> {
  const response = await request(path, {
    method: 'POST',
    body: JSON.stringify({ values }),
  })
  if (!response.ok) throw new Error(`接口返回 ${response.status}，操作未生效`)
  return (await response.json()) as T
}

async function getJson<T>(path: string): Promise<T> {
  const response = await request(path)
  if (!response.ok) throw new Error(`接口返回 ${response.status}，数据未更新`)
  return (await response.json()) as T
}

export type ScenarioPayload = {
  record_id: number
  记录编号: string
  目标表名: string
  评分版本: string
  view: DeductionView
  confirmed_deductions: Contribution[]
  pending_items: DeductionView['未齐项']
}

export function loadScenario(): Promise<ScenarioPayload> {
  return getJson<ScenarioPayload>('/api/scorecard-scenario')
}

export function loadContribution(recordId: number, version?: string): Promise<{
  view: DeductionView
  versions: Record<string, unknown>[]
  evidence: Record<string, unknown>[]
}> {
  const query = version ? `?version=${encodeURIComponent(version)}` : ''
  return getJson(`/api/scorecard/${recordId}/contribution${query}`)
}

export type ReplayResult = {
  ok: boolean
  可重放: boolean
  评分版本: string
  修订号: number
  快照综合得分: number
  按权重复算: number
  差异: number
  摘要一致: boolean
  分项: Contribution[]
  message?: string
}

export function replayVersion(recordId: number, version: string, revision?: number): Promise<ReplayResult> {
  const params = new URLSearchParams({ version })
  if (revision !== undefined) params.set('revision', String(revision))
  return getJson<ReplayResult>(`/api/scorecard/${recordId}/replay?${params.toString()}`)
}

export type CorrectionResult = {
  ok: boolean
  评分版本?: string
  修订号?: number
  综合得分?: number
  未齐项数?: number
  版本冲突?: boolean
  并发冲突?: boolean
  message: string
}

export function acceptCorrection(
  recordId: number,
  values: { version: string; rule_id: string; result: string; score?: number; expected_revision?: number; evidence_id?: string },
): Promise<CorrectionResult> {
  return postJson<CorrectionResult>(`/api/scorecard/${recordId}/corrections`, values)
}

export type ActionResult = { ok: boolean; message: string } & Record<string, unknown>

export function createDailyReport(values: { record_id: number; report_no?: string; dispatch?: boolean }): Promise<
  ActionResult & { 复核项?: ReviewItem[]; 报告?: DailyReport }
> {
  return postJson(`/api/daily-report`, values)
}

export function dispatchReport(reportNo: string): Promise<ActionResult> {
  return postJson(`/api/daily-report/${reportNo}/dispatch`)
}

export function rebaseReport(reportNo: string): Promise<ActionResult> {
  return postJson(`/api/daily-report/${reportNo}/rebase`)
}

export function reopenReport(reportNo: string, evidenceNo: string): Promise<ActionResult> {
  return postJson(`/api/daily-report/${reportNo}/reopen`, { evidence_no: evidenceNo })
}

export function addEvidence(values: {
  record_no: string
  version: string
  revision?: number
  report_no?: string
  证据编号?: string
  规则编号?: string
  样本行数?: number
  失败行数?: number
  结论?: string
}): Promise<ActionResult> {
  return postJson('/api/evidence', values)
}

export type VerifyResult = {
  ok: boolean
  可复算: boolean
  报告综合得分: number
  分项复算综合分: number
  分项差异: number
  复核项复算综合分: number
  不一致复核项: { 复核项编号: string; 规则编号: string }[]
  版本与报告一致: boolean | null
  复核项核对: {
    复核项编号: string
    规则编号: string
    权重: number
    规则得分: number
    记录加权得分: number
    复算加权得分: number
    一致: boolean
    派工状态: string
    状态: string
  }[]
  message: string
}

export function verifyReport(reportNo: string): Promise<VerifyResult> {
  return getJson<VerifyResult>(`/api/daily-report/${reportNo}/verify`)
}

export function loadReport(reportNo: string): Promise<{ report: DailyReport; review_items: ReviewItem[] }> {
  return getJson(`/api/daily-report/${reportNo}`)
}
