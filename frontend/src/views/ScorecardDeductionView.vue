<template>
  <section class="page" data-testid="scorecard-deduction">
    <header class="page-head">
      <div>
        <h2>评分降分依据</h2>
        <p class="page-desc">
          评分卡只保留结果；这里从规则贡献账本还原“这张表为什么降分”，并据此生成日报复查项、派工与复算核对。
        </p>
      </div>
      <div class="page-actions">
        <button class="btn primary" data-testid="build-report" type="button" :disabled="busy" @click="buildReport">
          生成日报并生成复查项
        </button>
      </div>
    </header>

    <p v-if="errorMessage" class="error-text" data-testid="scorecard-error">{{ errorMessage }}</p>
    <p v-if="notice" class="notice-text" data-testid="scorecard-notice">{{ notice }}</p>

    <template v-if="view">
      <div class="stat-row">
        <article class="stat-card">
          <span class="stat-label">目标表 / 版本</span>
          <strong class="stat-value small">{{ view.目标表名 }} · {{ view.评分版本 }}</strong>
        </article>
        <article class="stat-card">
          <span class="stat-label">综合得分（按权重复算）</span>
          <strong class="stat-value" data-testid="composite-score">{{ view.综合得分 }}</strong>
        </article>
        <article class="stat-card">
          <span class="stat-label">降分合计</span>
          <strong class="stat-value" data-testid="deduction-total">{{ view.降分合计 }}</strong>
        </article>
        <article class="stat-card">
          <span class="stat-label">权重覆盖 / 未齐项</span>
          <strong class="stat-value small">
            {{ formatWeight(view.权重覆盖) }} / {{ view.未齐项数 }}
          </strong>
        </article>
        <article class="stat-card">
          <span class="stat-label">版本修订 / 复算</span>
          <strong class="stat-value small">
            r{{ view.修订号 }} · {{ view.可复算 ? '可复算' : '不可复算' }}
          </strong>
        </article>
      </div>

      <section class="panel">
        <header class="panel-head">
          <h3>规则贡献账本（权重 × 结果 = 综合分）</h3>
          <span class="panel-hint">
            降分合计 {{ deductionOf(view.规则贡献) }}；复算 {{ compositeOf(view.规则贡献) }}
          </span>
        </header>
        <table class="data-table" data-testid="contribution-table">
          <thead>
            <tr>
              <th>规则编号</th><th>规则名称</th><th>维度</th><th>权重</th><th>结果</th>
              <th>规则得分</th><th>加权得分</th><th>降分贡献</th><th>确认状态</th><th>未齐</th>
            </tr>
          </thead>
          <tbody>
            <tr
              v-for="rule in view.规则贡献"
              :key="rule.规则编号"
              :data-testid="`contribution-${rule.规则编号}`"
              :class="{ 'row-pending': rule.未齐 || !rule.纳入 }"
            >
              <td>{{ rule.规则编号 }}</td>
              <td>{{ rule.规则名称 }}</td>
              <td>{{ rule.维度 }}</td>
              <td>{{ formatWeight(rule.权重) }}</td>
              <td>{{ rule.结果 }}</td>
              <td>{{ rule.得分 }}</td>
              <td>{{ rule.加权得分 }}</td>
              <td>{{ rule.降分贡献 }}</td>
              <td>{{ rule.确认状态 }}</td>
              <td>
                <span v-if="rule.未齐 || !rule.纳入" class="tag warn">未齐</span>
                <span v-else class="tag ok">已齐</span>
              </td>
            </tr>
          </tbody>
        </table>
      </section>

      <div v-if="view.未齐项.length" class="panel warn-panel" data-testid="pending-panel">
        <header class="panel-head">
          <h3>未齐项（单独标明，不计入已确认降分）</h3>
        </header>
        <ul class="plain-list">
          <li v-for="item in view.未齐项" :key="item.规则编号" :data-testid="`pending-${item.规则编号}`">
            {{ item.规则编号 }} {{ item.规则名称 }}：{{ item.未齐原因 }}
            （当前 {{ item.当前结果 }} / {{ item.当前得分 }} 分）
          </li>
        </ul>
      </div>

      <section class="panel">
        <header class="panel-head">
          <h3>降分依据（按降分贡献排序）</h3>
          <span class="panel-hint">已确认 {{ confirmedDeductions.length }} 条</span>
        </header>
        <table class="data-table" data-testid="deduction-table">
          <thead>
            <tr><th>规则编号</th><th>规则名称</th><th>权重</th><th>规则得分</th><th>降分贡献</th><th>确认状态</th></tr>
          </thead>
          <tbody>
            <tr v-for="rule in view.降分依据" :key="rule.规则编号" :data-testid="`deduction-${rule.规则编号}`">
              <td>{{ rule.规则编号 }}</td>
              <td>{{ rule.规则名称 }}</td>
              <td>{{ formatWeight(rule.权重) }}</td>
              <td>{{ rule.得分 }}</td>
              <td>{{ rule.降分贡献 }}</td>
              <td>{{ rule.确认状态 }}</td>
            </tr>
            <tr v-if="!view.降分依据.length">
              <td colspan="6" class="empty-state">当前版本没有降分规则</td>
            </tr>
          </tbody>
        </table>
      </section>

      <section class="panel">
        <header class="panel-head">
          <h3>版本与重放</h3>
          <span class="panel-hint">修订 {{ view.修订号 }}；旧报告引用的版本可重放</span>
        </header>
        <div class="inline-form">
          <label>
            <span>修订号</span>
            <input v-model="replayRevision" data-testid="replay-revision" placeholder="留空为当前修订" />
          </label>
          <button class="btn" data-testid="replay" type="button" :disabled="busy" @click="runReplay">
            重放该修订
          </button>
          <label>
            <span>更正规则</span>
            <input v-model="correction.rule_id" data-testid="correction-rule" placeholder="DQ_R-1005" />
          </label>
          <label>
            <span>更正结果</span>
            <input v-model="correction.result" data-testid="correction-result" placeholder="通过" />
          </label>
          <label>
            <span>更正得分</span>
            <input v-model="correction.score" data-testid="correction-score" placeholder="80" />
          </label>
          <button class="btn" data-testid="accept-correction" type="button" :disabled="busy" @click="submitCorrection">
            接纳更正
          </button>
        </div>
        <p v-if="replay" class="replay-result" data-testid="replay-result">
          修订 r{{ replay.修订号 }}：快照 {{ replay.快照综合得分 }}，复算 {{ replay.按权重复算 }}，
          差异 {{ replay.差异 }}，{{ replay.可重放 ? '可重放' : '不可重放' }}
        </p>
      </section>
    </template>

    <template v-if="report">
      <section class="panel">
        <header class="panel-head">
          <h3>日报 {{ report.报告编号 }}</h3>
          <span class="panel-hint">
            {{ report.目标表名 }} · {{ report.评分版本 }} · r{{ report.引用修订号 }} ·
            综合分 {{ report.综合得分 }} · {{ report.复核状态 }}
          </span>
        </header>
        <div class="inline-form">
          <button class="btn" data-testid="rebase-report" type="button" :disabled="busy" @click="rebase">
            重挂到最新修订
          </button>
          <button class="btn" data-testid="dispatch-report" type="button" :disabled="busy" @click="dispatch">
            派工复查项
          </button>
          <label>
            <span>证据编号</span>
            <input v-model="evidenceNo" data-testid="evidence-no" placeholder="EV-0001" />
          </label>
          <button class="btn" data-testid="add-evidence" type="button" :disabled="busy" @click="submitEvidence">
            补采集证据
          </button>
          <button class="btn" data-testid="reopen-report" type="button" :disabled="busy" @click="reopen">
            重开复核项
          </button>
          <button class="btn primary" data-testid="verify-report" type="button" :disabled="busy" @click="verify">
            核对派工分项
          </button>
        </div>

        <table class="data-table" data-testid="review-table">
          <thead>
            <tr>
              <th>复核项</th><th>规则编号</th><th>分项类型</th><th>权重</th><th>规则得分</th>
              <th>加权得分</th><th>降分贡献</th><th>未齐</th><th>派工状态</th><th>状态</th>
            </tr>
          </thead>
          <tbody>
            <tr
              v-for="item in reviewItems"
              :key="item.id"
              :data-testid="`review-${item.规则编号}`"
              :class="{ 'row-pending': item.未齐 }"
            >
              <td>{{ item.复核项编号 }}</td>
              <td>{{ item.规则编号 }}</td>
              <td>{{ item.分项类型 }}</td>
              <td>{{ formatWeight(item.权重) }}</td>
              <td>{{ item.规则得分 }}</td>
              <td>{{ item.加权得分 }}</td>
              <td>{{ item.降分贡献 }}</td>
              <td>
                <span v-if="item.未齐" class="tag warn">未齐</span>
                <span v-else class="tag ok">已齐</span>
              </td>
              <td>{{ item.派工状态 }}</td>
              <td>{{ item.状态 }}</td>
            </tr>
          </tbody>
        </table>
      </section>

      <section v-if="verifyResult" class="panel" data-testid="verify-panel">
        <header class="panel-head">
          <h3>派工依据核对</h3>
          <span class="panel-hint">
            {{ verifyResult.可复算 ? '可复算' : '不可复算' }}：报告 {{ verifyResult.报告综合得分 }} /
            分项复算 {{ verifyResult.分项复算综合分 }} / 复核项复算 {{ verifyResult.复核项复算综合分 }}
          </span>
        </header>
        <ul class="plain-list">
          <li v-for="check in verifyResult.复核项核对" :key="check.复核项编号">
            {{ check.复核项编号 }} {{ check.规则编号 }}：{{ formatWeight(check.权重) }} ×
            {{ check.规则得分 }} = {{ check.复算加权得分 }}
            <span :class="check.一致 ? 'tag ok' : 'tag warn'">{{ check.一致 ? '一致' : '不一致' }}</span>
            {{ check.派工状态 }} / {{ check.状态 }}
          </li>
        </ul>
      </section>
    </template>
  </section>
</template>

<script setup lang="ts">
import { computed, onMounted, ref } from 'vue'

import {
  acceptCorrection,
  addEvidence,
  createDailyReport,
  dispatchReport,
  loadReport,
  loadScenario,
  rebaseReport,
  reopenReport,
  replayVersion,
  verifyReport,
} from '@/api/scorecard-deduction'
import type { ReplayResult, VerifyResult } from '@/api/scorecard-deduction'
import {
  compositeOf,
  deductionOf,
  formatWeight,
  splitDeductions,
} from '@/domain/dq_scorecard_deduction'
import type { DailyReport, DeductionView, ReviewItem } from '@/domain/dq_scorecard_deduction'

const view = ref<DeductionView | null>(null)
const recordId = ref(0)
const recordNo = ref('')
const version = ref('v1')
const report = ref<DailyReport | null>(null)
const reviewItems = ref<ReviewItem[]>([])
const replay = ref<ReplayResult | null>(null)
const verifyResult = ref<VerifyResult | null>(null)
const replayRevision = ref('')
const evidenceNo = ref('')
const busy = ref(false)
const notice = ref('')
const errorMessage = ref('')
const correction = ref({ rule_id: '', result: '', score: '' })

const confirmedDeductions = computed(() =>
  view.value ? splitDeductions(view.value).confirmed : [],
)

function fail(error: unknown, fallback: string) {
  errorMessage.value = error instanceof Error ? error.message : fallback
}

async function loadScenarioView() {
  const payload = await loadScenario()
  recordId.value = payload.record_id
  recordNo.value = payload.记录编号
  version.value = payload.评分版本
  view.value = payload.view
}

async function refreshView() {
  await loadScenarioView()
  if (report.value) {
    const detail = await loadReport(report.value.报告编号)
    report.value = detail.report
    reviewItems.value = detail.review_items
  }
}

async function withBusy(action: () => Promise<void>, fallback: string) {
  busy.value = true
  errorMessage.value = ''
  notice.value = ''
  try {
    await action()
  } catch (error) {
    fail(error, fallback)
  } finally {
    busy.value = false
  }
}

function buildReport() {
  return withBusy(async () => {
    const result = await createDailyReport({ record_id: recordId.value })
    if (!result.ok) {
      notice.value = result.message
      return
    }
    const reportNo = String(result.报告编号)
    const detail = await loadReport(reportNo)
    report.value = detail.report
    reviewItems.value = detail.review_items
    verifyResult.value = null
    notice.value = result.message
  }, '日报生成失败')
}

function submitCorrection() {
  return withBusy(async () => {
    const result = await acceptCorrection(recordId.value, {
      version: version.value,
      rule_id: correction.value.rule_id,
      result: correction.value.result,
      score: correction.value.score ? Number(correction.value.score) : undefined,
    })
    notice.value = result.message
    if (result.ok) {
      correction.value = { rule_id: '', result: '', score: '' }
      await refreshView()
    }
  }, '更正接纳失败')
}

function runReplay() {
  return withBusy(async () => {
    const revision = replayRevision.value.trim()
    replay.value = await replayVersion(
      recordId.value,
      version.value,
      revision ? Number(revision) : undefined,
    )
  }, '版本重放失败')
}

function rebase() {
  return withBusy(async () => {
    if (!report.value) return
    const result = await rebaseReport(report.value.报告编号)
    notice.value = result.message
    await refreshView()
  }, '报告重挂失败')
}

function dispatch() {
  return withBusy(async () => {
    if (!report.value) return
    const result = await dispatchReport(report.value.报告编号)
    notice.value = result.message
    await refreshView()
  }, '派工失败')
}

function submitEvidence() {
  return withBusy(async () => {
    const evidenceNoValue = evidenceNo.value.trim() || `EV-${Date.now()}`
    const result = await addEvidence({
      record_no: recordNo.value,
      version: version.value,
      revision: view.value?.修订号,
      report_no: report.value?.报告编号,
      证据编号: evidenceNoValue,
      结论: '复核采集证据',
    })
    evidenceNo.value = evidenceNoValue
    notice.value = `已登记采集证据 ${evidenceNoValue}：${result.结论 ?? result.message}`
  }, '采集证据登记失败')
}

function reopen() {
  return withBusy(async () => {
    if (!report.value) return
    const result = await reopenReport(report.value.报告编号, evidenceNo.value.trim())
    notice.value = result.message
    await refreshView()
  }, '复核项重开失败')
}

function verify() {
  return withBusy(async () => {
    if (!report.value) return
    verifyResult.value = await verifyReport(report.value.报告编号)
    notice.value = verifyResult.value.message
  }, '派工分项核对失败')
}

onMounted(() => withBusy(loadScenarioView, '评分降分依据读取失败'))
</script>
