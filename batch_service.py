import copy
import datetime


def _new_batch_id(now=None):
    now = now or datetime.datetime.now()
    return "BATCH-" + now.strftime("%Y%m%d-%H%M%S-%f")


def invalidate_batch(state, now=None):
    """結束舊刷並建立乾淨的新批次；只重置工作狀態，不反轉任何庫存帳。"""
    old_batch_id = state.get("batch_id")
    old_routes = copy.deepcopy(state.get("routes") or [])
    old_loose = copy.deepcopy(state.get("unplanned_stations") or [])

    if old_routes or old_loose or old_batch_id:
        history = state.setdefault("batch_history", [])
        history.append({
            "batch_id": old_batch_id,
            "closed_at": (now or datetime.datetime.now()).isoformat(timespec="seconds"),
            "reason": "玩家確認遊戲交換已刷新",
            "routes": old_routes,
            "unplanned_stations": old_loose,
        })
        state["batch_history"] = history[-20:]

    picker = dict(state.get("route_picker") or {})
    picker["selected"] = {}
    picker["selected_times"] = {}

    state["previous_batch_id"] = old_batch_id
    state["batch_id"] = _new_batch_id(now)
    state["status"] = "等待匯入新一刷"
    state["routes"] = []
    state["unplanned_stations"] = []
    state["route_picker"] = picker
    state.pop("route_planner_baseline", None)
    return state
