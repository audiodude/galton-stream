import os, sys
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import redirect

class FakeS3:
    def __init__(self, rules=None): self.cfg = {"RoutingRules": rules} if rules else {}; self.put = None
    def get_bucket_website(self, Bucket): return self.cfg
    def put_bucket_website(self, Bucket, WebsiteConfiguration): self.put = WebsiteConfiguration
    def put_object(self, **kw): pass
class FakeCF:
    def create_invalidation(self, **kw): self.inv = kw

def test_set_radio_online_writes_302_live_rule():
    s3, cf = FakeS3(), FakeCF()
    assert redirect.set_radio_online("VID123", s3=s3, cf=cf) is True
    rule = s3.put["RoutingRules"][0]["Redirect"]
    assert rule["HttpRedirectCode"] == "302"
    assert rule["ReplaceKeyWith"] == "live/VID123"
    assert rule["HostName"] == "www.youtube.com"

def test_set_radio_offline_drops_rule():
    s3, cf = FakeS3(rules=[{"Redirect": {"ReplaceKeyWith": "live/X"}}]), FakeCF()
    assert redirect.set_radio_offline(s3=s3, cf=cf) is True
    assert "RoutingRules" not in s3.put

def test_current_video_id_reads_rule():
    s3 = FakeS3(rules=[{"Redirect": {"ReplaceKeyWith": "live/ABC"}}])
    assert redirect.current_video_id(s3=s3) == "ABC"
    assert redirect.current_video_id(s3=FakeS3()) is None
