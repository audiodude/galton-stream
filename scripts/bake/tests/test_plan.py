import os, sys
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import plan

def test_title_from_path():
    assert plan.title_from_path("/x/ele-1005.mp3") == "Ele 1005"
    assert plan.title_from_path("never_havin_kids.mp3") == "Never Havin Kids"

def test_shuffle_deterministic_and_total():
    a = plan.shuffle(list(range(20)), seed=7)
    b = plan.shuffle(list(range(20)), seed=7)
    c = plan.shuffle(list(range(20)), seed=8)
    assert a == b and a != c
    assert sorted(a) == list(range(20))  # permutation, nothing lost

def test_shuffle_no_repeat_join():
    # no element equal to its predecessor (best-effort across a single shuffle)
    out = plan.shuffle(["a", "b", "c", "d"], seed=3, no_repeat=True)
    assert all(out[i] != out[i-1] for i in range(1, len(out)))

def test_music_timeline_fills_target():
    ordered = [{"file": f"{i}.mp3", "title": str(i), "dur": 100.0} for i in range(10)]
    tl = plan.build_music_timeline(ordered, target_dur=250.0)
    assert len(tl) == 3                      # 100+100+100 >= 250, stops at 3
    assert tl[0]["start"] == 0.0 and tl[0]["end"] == 100.0
    assert tl[1]["start"] == 100.0 and tl[2]["end"] == 300.0

def test_song_boundaries():
    tl = [{"start": 0.0, "end": 100.0}, {"start": 100.0, "end": 250.0}, {"start": 250.0, "end": 400.0}]
    assert plan.song_boundaries(tl) == [100.0, 250.0]   # boundaries are song STARTS, excluding 0

def test_subtitles_one_per_song():
    tl = [{"title": "A", "start": 0.0, "end": 100.0}, {"title": "B", "start": 100.0, "end": 250.0}]
    subs = plan.build_subtitles(tl)
    assert subs == [{"text": "A", "start": 0.0, "end": 100.0}, {"text": "B", "start": 100.0, "end": 250.0}]
