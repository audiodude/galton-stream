"""radio.dangerthirdrail.com S3 website redirect + CloudFront invalidation.
In-window: 302 -> youtube.com/live/<id>. Out: drop the rule (serve index.html)."""
import os
import sys
import time

RADIO_BUCKET = os.environ.get("RADIO_BUCKET", "radio.dangerthirdrail.com")
RADIO_CF_DISTRIBUTION_ID = os.environ.get("RADIO_CF_DISTRIBUTION_ID", "E24RTA588S2VSH")
RADIO_REGION = os.environ.get("RADIO_REGION", "us-east-1")
RADIO_OFFLINE_HTML_PATH = os.environ.get("RADIO_OFFLINE_HTML_PATH", "/app/radio-offline.html")

_s3 = _cf = None


def _log(msg):
    print(f"[monitor] {msg}", file=sys.stderr, flush=True)


def _clients(s3, cf):
    global _s3, _cf
    if s3 is None or cf is None:
        import boto3
        if _s3 is None:
            _s3 = boto3.client("s3", region_name=RADIO_REGION)
            _cf = boto3.client("cloudfront", region_name=RADIO_REGION)
    return (s3 or _s3), (cf or _cf)


def _invalidate(cf):
    try:
        cf.create_invalidation(
            DistributionId=RADIO_CF_DISTRIBUTION_ID,
            InvalidationBatch={"Paths": {"Quantity": 1, "Items": ["/*"]},
                               "CallerReference": f"radio-{int(time.time())}"})
    except Exception as e:
        _log(f"CloudFront invalidation failed: {e}")


def current_video_id(s3=None):
    s3, _ = _clients(s3, s3)
    try:
        cfg = s3.get_bucket_website(Bucket=RADIO_BUCKET)
    except Exception as e:
        _log(f"get_bucket_website failed: {e}")
        return None
    rules = cfg.get("RoutingRules") or []
    if not rules:
        return None
    key = rules[0].get("Redirect", {}).get("ReplaceKeyWith", "")
    return key[len("live/"):] if key.startswith("live/") else None


def set_radio_online(video_id, s3=None, cf=None):
    s3, cf = _clients(s3, cf)
    try:
        s3.put_bucket_website(Bucket=RADIO_BUCKET, WebsiteConfiguration={
            "IndexDocument": {"Suffix": "index.html"},
            "RoutingRules": [{"Redirect": {
                "HostName": "www.youtube.com", "HttpRedirectCode": "302",
                "Protocol": "https", "ReplaceKeyWith": f"live/{video_id}"}}]})
        _invalidate(cf)
        _log(f"Radio ONLINE -> youtube.com/live/{video_id}")
        return True
    except Exception as e:
        _log(f"set_radio_online failed: {e}")
        return False


def set_radio_offline(s3=None, cf=None):
    s3, cf = _clients(s3, cf)
    try:
        if os.path.exists(RADIO_OFFLINE_HTML_PATH):
            with open(RADIO_OFFLINE_HTML_PATH, "rb") as f:
                s3.put_object(Bucket=RADIO_BUCKET, Key="index.html", Body=f.read(),
                              ContentType="text/html; charset=utf-8",
                              CacheControl="public, max-age=60")
        s3.put_bucket_website(Bucket=RADIO_BUCKET,
                              WebsiteConfiguration={"IndexDocument": {"Suffix": "index.html"}})
        _invalidate(cf)
        _log("Radio OFFLINE -> serving index.html")
        return True
    except Exception as e:
        _log(f"set_radio_offline failed: {e}")
        return False
