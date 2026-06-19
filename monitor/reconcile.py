# monitor/reconcile.py
"""Pure reconcile planner. Given current state, return the list of actions to
take. No I/O — every action is a tuple the executor in monitor.py runs. All
broadcast actions are scoped to the owned stream via youtube.owned_broadcasts."""
import youtube


def plan_actions(now, in_op, in_consumer, stream_id, broadcasts, stream_active,
                 current_redirect_vid):
    owned = youtube.owned_broadcasts(broadcasts, stream_id)
    live = next((b for b in owned if youtube.life(b) == "live"), None)
    pending = [b for b in owned if youtube.life(b) in ("created", "ready", "testing")]
    recent_pending = next((b for b in pending if youtube.is_recent(b, now)), None)
    actions = []

    if not in_op:
        for b in owned:
            if youtube.life(b) in ("live", "testing"):
                actions.append(("end_broadcast", b["id"]))
        if current_redirect_vid is not None:
            actions.append(("redirect_offline",))
        return actions

    # in operational window
    for b in pending:
        if not youtube.is_recent(b, now):
            actions.append(("delete_broadcast", b["id"]))

    if live is None and recent_pending is None:
        actions.append(("create_broadcast",))
    elif live is None and recent_pending is not None and stream_active:
        actions.append(("go_live", recent_pending["id"]))

    target = live["id"] if (in_consumer and live is not None and stream_active) else None
    if target is not None and current_redirect_vid != target:
        actions.append(("redirect_online", target))
    elif target is None and current_redirect_vid is not None:
        actions.append(("redirect_offline",))

    return actions
