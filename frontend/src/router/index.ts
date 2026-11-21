import { createRouter, createWebHistory } from 'vue-router'

import Overview from '@/views/Overview.vue'
import ScorecardDeductionView from '@/views/ScorecardDeductionView.vue'
import WorkbenchView from '@/views/WorkbenchView.vue'

const router = createRouter({
  history: createWebHistory(),
  routes: [
    { path: '/', name: 'overview', component: Overview },
    // 固定路径要放在 /:moduleKey 之前，避免被动态模块路由吞掉。
    { path: '/scorecard-deduction', name: 'scorecard-deduction', component: ScorecardDeductionView },
    { path: '/:moduleKey', name: 'module', component: WorkbenchView, props: true },
  ],
})

export default router
