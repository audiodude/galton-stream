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


def test_offspec_audio_inputs_still_land_in_the_show(tmp_path):
    """A 48 kHz track and an AIFF-named-.mp3 must occupy their planned slots.

    Fed straight to the concat demuxer these dropped out entirely — a whole song
    of dead air in the middle of a baked show."""
    fx = tmp_path / "fx"
    subprocess.run([os.path.join(HERE, "make_fixtures.sh"), str(fx)], check=True)
    catalog = plan._load_catalog(str(fx / "catalog" / "catalog.json"))
    import glob
    lib = [{"file": p, "title": plan.title_from_path(p), "dur": plan.ffprobe_duration(p)}
           for p in sorted(glob.glob(str(fx / "music" / "*.mp3")))]
    cfg = {"seed": 3, "show_dur_sec": 30.0, "dwell_sec": 5.0,
           "fps": 60, "resolution": "320x180", "straddle": True}
    p = plan.build_plan(catalog, lib, cfg)
    assert any("song-five" in m["file"] for m in p["music"]), "fixture not in the timeline"
    plan_path = fx / "plan.json"
    plan_path.write_text(json.dumps(p))
    out = fx / "show.mkv"
    assemble.assemble(str(plan_path), str(out))

    # every song slot is filled: total audio matches the plan, nothing dropped
    seg = fx / "seg.wav"
    for name in ("song-five", "song-four"):
        m = next(m for m in p["music"] if name in m["file"])
        mid = (m["start"] + m["end"]) / 2.0
        subprocess.run(["ffmpeg", "-y", "-v", "error", "-ss", str(mid - 1.0), "-i", str(out),
                        "-t", "2", "-map", "0:a", "-c:a", "pcm_s16le", str(seg)], check=True)
        assert float(_probe(str(seg), "a:0", "format=duration")) > 1.5, f"{name}: no audio in slot"
        vol = subprocess.run(["ffmpeg", "-v", "info", "-i", str(seg), "-af", "volumedetect",
                              "-f", "null", "-"], capture_output=True, text=True).stderr
        mean = next(l.split("mean_volume:")[1] for l in vol.splitlines() if "mean_volume:" in l)
        assert float(mean.replace("dB", "").strip()) > -60.0, f"{name}: slot is silent"
