"""个人画像（11.17 / V1.59）的 JSON 投影：节奏 / 关系 / 承诺 三面。

为什么单独一个文件：``serialize.py`` 已顶到 250 行上限
（``TestWebPackageStaysSmall`` 守着那条线），而画像是「个人模型」整面的
只读投影 —— 与 ``endpoints_model.py`` / ``js_model.py`` 同族，放一起
才读得懂。

**不做的事**：这里不重新发明任何业务规则。逾期标 ``overdue``、峰判定
（V1.101：集中在哪、够不够判定线）全是服务层说了算，web 层只做
「取数 + 拼形状」（12.1.1「一份能力一份实现」）。``hour_peak`` /
``dow_peak`` 判定不出时是 ``null`` —— 与 CLI 的 None 同义，前端据此
说「未达判定线」，不自己算一遍。
"""

from __future__ import annotations

from ..services.sorting import DISCLAIMER_MARKER

__all__ = ["model_payload"]


def model_payload(view, now=None) -> dict:
    """连续性个人模型（11.17 / V1.59）：节奏 / 关系 / 承诺 三面。

    只读聚合的投影，不含任何业务判断 —— 逾期标 ``overdue``、违约交给用户，
    与 /api/contacts、/api/promises 同一份「只摆事实」纪律（11.19.5）。

    V1.101：``hour_peak`` = ``[起, 止, 计数]``、``dow_peak`` =
    ``[weekday, 计数]``，判定不出为 ``null``。计数随身带（V1.96：
    没有计数的规律不可反驳），占比由前端按 ``done_in_window`` 现算
    （与 CLI 同一条口径，除法不落两层）。
    """
    return {
        "kind": "personal_model",
        "since": view.since.isoformat(),
        "until": view.until.isoformat(),
        "window_tasks": view.window_tasks,
        "done_in_window": view.done_in_window,
        "completion_by_hour": [list(p) for p in view.completion_by_hour],
        "completion_by_dow": [list(p) for p in view.completion_by_dow],
        "hour_peak": list(view.hour_peak) if view.hour_peak else None,
        "dow_peak": list(view.dow_peak) if view.dow_peak else None,
        "rollover_total": view.rollover_total,
        "tasks_rolled_over": [
            {"id8": t[0], "title": t[1], "n": t[2]} for t in view.tasks_rolled_over
        ],
        "stuck_waiting": [
            {"id8": t[0], "title": t[1], "age_days": t[2]} for t in view.stuck_waiting
        ],
        "role_activity": [{"name": n, "count": c} for (n, c) in view.role_activity],
        # V1.59 新增两面：关系账 + 承诺与目标。
        "relationships": [
            {"id": r[0], "name": r[1], "open_promises": r[2], "open_waitings": r[3]}
            for r in view.relationships
        ],
        "commitments": [{"kind": c[0], "text": c[1], "overdue": c[2]} for c in view.commitments],
        # V1.69 滋养兴趣（记录优先）：记下的「偏好」原样带出，不评价不催促
        "preferences": [{"text": t} for _k, t in view.preferences],
        "disclaimer": DISCLAIMER_MARKER,
    }
