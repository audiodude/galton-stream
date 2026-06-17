import json, os, subprocess, sys, tempfile
HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.dirname(HERE))
import plan, assemble

def _probe(path, stream, entry):
    out = subprocess.run(["ffprobe", "-v", "error", "-select_streams", stream,
        "-show_entries", entry, "-of", "default=noprint_wrappers=1:nokey=1", path],
        capture_output=True, text=True, check=True)
    return out.stdout.strip()

def test_assemble_produces_synced_show(tmp_path):
    fx = tmp_path / "fx"
    subprocess.run([os.path.join(HERE, "make_fixtures.sh"), str(fx)], check=True)
    catalog = plan._load_catalog(str(fx / "catalog" / "catalog.json"))
    import glob
    lib = [{"file": p, "title": plan.title_from_path(p), "dur": plan.ffprobe_duration(p)}
           for p in sorted(glob.glob(str(fx / "music" / "*.mp3")))]
    cfg = {"seed": 1, "show_dur_sec": 15.0, "dwell_sec": 5.0,
           "fps": 60, "resolution": "320x180", "straddle": True}
    p = plan.build_plan(catalog, lib, cfg)
    plan_path = fx / "plan.json"
    plan_path.write_text(json.dumps(p))
    out = fx / "show.mkv"
    assemble.assemble(str(plan_path), str(out))

    assert out.exists()
    dur = float(_probe(str(out), "v:0", "format=duration"))
    assert abs(dur - p["show_dur_sec"]) < 0.5            # video length == show
    assert _probe(str(out), "v:0", "stream=codec_name") == "h264"
    assert _probe(str(out), "a:0", "stream=codec_name") == "aac"
    assert _probe(str(out), "v:0", "stream=width") == "320"
