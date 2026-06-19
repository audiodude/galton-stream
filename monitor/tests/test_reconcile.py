# monitor/tests/test_reconcile.py
import os, sys, datetime
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import reconcile
UTC = datetime.timezone.utc
NOW = datetime.datetime(2026, 6, 19, 13, 0, tzinfo=UTC)

def b(bid, life, bound, sst="2026-06-19T12:59:30Z"):  # recent by default
    return {"id": bid, "status": {"lifeCycleStatus": life},
            "contentDetails": {"boundStreamId": bound},
            "snippet": {"scheduledStartTime": sst}}

def plan(**kw):
    base = dict(now=NOW, in_op=True, in_consumer=True, stream_id="S1",
                broadcasts=[], stream_active=False, current_redirect_vid=None)
    base.update(kw)
    return reconcile.plan_actions(**base)

# THE headline regression: out of window, a FOREIGN live broadcast must be untouched
def test_out_of_window_foreign_live_is_noop():
    acts = plan(in_op=False, in_consumer=False,
                broadcasts=[b("foreign", "live", "S_OTHER")])
    assert ("end_broadcast", "foreign") not in acts
    assert all(a[0] != "end_broadcast" for a in acts)

def test_out_of_window_owned_live_is_ended():
    acts = plan(in_op=False, in_consumer=False,
                broadcasts=[b("mine", "live", "S1")])
    assert ("end_broadcast", "mine") in acts

def test_in_window_no_broadcast_creates():
    assert ("create_broadcast",) in plan(broadcasts=[])

def test_in_window_recent_pending_goes_live_when_stream_active():
    acts = plan(broadcasts=[b("mine", "ready", "S1")], stream_active=True)
    assert ("go_live", "mine") in acts

def test_in_window_pending_waits_when_stream_inactive():
    acts = plan(broadcasts=[b("mine", "ready", "S1")], stream_active=False)
    assert all(a[0] not in ("go_live", "create_broadcast") for a in acts)

def test_redirect_online_only_in_consumer_with_live_active():
    acts = plan(broadcasts=[b("mine", "live", "S1")], stream_active=True)
    assert ("redirect_online", "mine") in acts

def test_redirect_offline_when_outside_consumer():
    acts = plan(in_op=True, in_consumer=False, broadcasts=[b("mine", "live", "S1")],
                stream_active=True, current_redirect_vid="mine")
    assert ("redirect_offline",) in acts

def test_redirect_online_not_repeated_when_already_pointed():
    acts = plan(broadcasts=[b("mine", "live", "S1")], stream_active=True,
                current_redirect_vid="mine")
    assert all(a[0] != "redirect_online" for a in acts)

def test_in_window_stale_pending_deleted():
    acts = plan(broadcasts=[b("old", "ready", "S1", sst="2026-06-19T12:00:00Z")])  # 60 min old
    assert ("delete_broadcast", "old") in acts
