/** 评分卡降分依据的纯前端计算：与后端 composite_of / deduction_of 保持同一口径。 */

export type Contribution = {
  规则编号: string
  规则名称: string
  维度: string
  权重: number
  结果: string
  得分: number
  确认状态: string
  纳入: boolean
  未齐?: boolean
  未齐原因?: string | null
  证据编号?: string | null
  加权得分: number
  降分贡献: number
}

export type DeductionView = {
  记录编号: string
  目标表名: string
  评分版本: string
  版本状态: string
  修订号: number
  综合得分: number
  降分合计: number
  权重覆盖: number
  规则总数: number
  纳入规则数: number
  未齐项数: number
  规则贡献: Contribution[]
  降分依据: Contribution[]
  规则权重与得分: Pick<Contribution, '规则编号' | '规则名称' | '权重' | '结果' | '得分' | '确认状态'>[]
  未齐项: { 规则编号: string; 规则名称: string; 未齐原因: string; 当前结果: string; 当前得分: number }[]
  可复算: boolean
  摘要?: string
  计算进度?: { 检查点: number; 计划规则数: number; 已完成: boolean }
}

export type ReviewItem = {
  id: number
  复核项编号: string
  报告编号: string
  规则编号: string
  规则名称: string
  权重: number
  规则得分: number
  加权得分: number
  降分贡献: number
  报告综合得分: number
  分项类型: string
  是否降分: boolean
  未齐: boolean
  未齐原因?: string | null
  确认结果?: string
  派工状态: string
  状态: string
}

export type DailyReport = {
  报告编号: string
  评分记录ID: number
  评分版本: string
  引用修订号: number
  目标表名: string
  综合得分: number
  降分合计: number
  权重覆盖: number
  可复算: boolean
  未齐项数: number
  未齐项: DeductionView['未齐项']
  复核状态: string
  分项明细: Contribution[]
}

/** 按规则权重和结果复算综合分，只统计纳入项。 */
export function compositeOf(rules: Contribution[]): number {
  return round1(
    rules.filter((rule) => rule.纳入).reduce((sum, rule) => sum + rule.权重 * rule.得分, 0),
  )
}

/** 降分合计 = Σ 权重 × (100 - 得分)。 */
export function deductionOf(rules: Contribution[]): number {
  return round2(
    rules.filter((rule) => rule.纳入).reduce((sum, rule) => sum + rule.权重 * (100 - rule.得分), 0),
  )
}

export function round1(value: number): number {
  return Math.round(value * 10) / 10
}

export function round2(value: number): number {
  return Math.round(value * 100) / 100
}

/** 校验派工所用的分项明细与综合分是否一致，返回逐项差异。 */
export function checkDispatchBasis(
  rules: Pick<Contribution, '规则编号' | '权重' | '得分' | '加权得分'>[],
  composite: number,
): { reproducible: boolean; recomputed: number; diff: number } {
  const recomputed = round1(rules.reduce((sum, rule) => sum + rule.权重 * rule.得分, 0))
  const diff = round2(recomputed - composite)
  return { reproducible: Math.abs(diff) < 0.05, recomputed, diff }
}

export function formatWeight(weight: number): string {
  return `${Math.round(weight * 1000) / 10}%`
}

/** 未齐项单独标注：绝不把未确认规则混进“已确认降分”。 */
export function splitDeductions(view: DeductionView): { confirmed: Contribution[]; pending: Contribution[] } {
  return {
    confirmed: view.降分依据.filter((item) => item.确认状态 === '已确认' && !item.未齐),
    pending: view.规则贡献.filter((item) => item.未齐 || !item.纳入),
  }
}
