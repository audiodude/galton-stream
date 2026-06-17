import json, os, subprocess, sys
HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.dirname(HERE))
import plan, assemble, validate

def test_validate_passes_on_good_bake(tmp_path):
    fx = tmp_path / "fx"
    subprocess.run([os.path.join(HERE, "make_fixtures.sh"), str(fx)], check=True)
    import glob
    catalog = plan._load_catalog(str(fx / "catalog" / "catalog.json"))
    lib = [{"file": p, "title": plan.title_from_path(p), "dur": plan.ffprobe_duration(p)}
           for p in sorted(glob.glob(str(fx / "music" / "*.mp3")))]
    cfg = {"seed": 1, "show_dur_sec": 15.0, "dwell_sec": 5.0,
           "fps": 60, "resolution": "320x180", "straddle": True}
    p = plan.build_plan(catalog, lib, cfg)
    (fx / "plan.json").write_text(json.dumps(p))
    assemble.assemble(str(fx / "plan.json"), str(fx / "show.mkv"))
    problems = validate.validate(str(fx / "plan.json"), str(fx / "show.mkv"))
    assert problems == [], problems

def test_validate_flags_wrong_duration(tmp_path):
    # a plan claiming 999s vs a short file -> duration problem
    fx = tmp_path / "fx"
    subprocess.run([os.path.join(HERE, "make_fixtures.sh"), str(fx)], check=True)
    import glob
    catalog = plan._load_catalog(str(fx / "catalog" / "catalog.json"))
    lib = [{"file": p, "title": plan.title_from_path(p), "dur": plan.ffprobe_duration(p)}
           for p in sorted(glob.glob(str(fx / "music" / "*.mp3")))]
    p = plan.build_plan(catalog, lib, {"seed": 1, "show_dur_sec": 15.0, "dwell_sec": 5.0,
                                       "fps": 60, "resolution": "320x180", "straddle": True})
    assemble.assemble(str(fx / "plan.json") if (fx/"plan.json").exists() else
                      _w(fx/"plan.json", p), str(fx / "show.mkv"))
    p["show_dur_sec"] = 999.0
    (fx / "plan2.json").write_text(json.dumps(p))
    problems = validate.validate(str(fx / "plan2.json"), str(fx / "show.mkv"))
    assert any("duration" in x for x in problems)

def _w(path, obj):
    path.write_text(json.dumps(obj)); return str(path)
