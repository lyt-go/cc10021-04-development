"""业务路由汇总：所有模块共用一套资源路由，由领域服务完成分派。"""
from __future__ import annotations

from app.routers import resource, scorecard_deduction

# 固定路径（如 /api/scorecard-scenario、/api/daily-report/...）必须先于
# 含 /api/{module}/{entry_id} 的通用资源路由注册，否则会被动态路径抢先匹配。
ROUTERS = [scorecard_deduction, resource]
