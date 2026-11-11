"""Audio matched to a slide deck by text: forward only, skipped slides, an early stop."""
from knowlib.media.slidealign import align, path

SLIDES = [(1, "Course title Operations Management"), (2, "Little's law inventory throughput flow time"),
          (3, "Bottleneck capacity utilization process"), (4, "Quality variation control charts"),
          (5, "Process mining Celonis event logs")]


def segs_for(texts):
    return [{"start": i * 30.0, "end": i * 30.0 + 30, "text": t} for i, t in enumerate(texts)]


def test_speech_follows_the_slides_and_a_skipped_slide_is_not_discussed():
    segs = segs_for(["inventory equals throughput times flow time, Little's law", "flow time and inventory again",
                     "the bottleneck sets capacity", "utilization of the bottleneck process",
                     "Celonis reads event logs", "process mining with Celonis"])
    places = {p.slide: p for p in align(segs, SLIDES, window_s=30)}
    assert places[2].start == 0 and places[3].start == 60 and places[5].start == 120
    assert places[1].start is None and places[4].start is None          # title shown before, quality skipped
    assert places[2].end == places[3].start                             # a slide lasts until the next


def test_the_path_never_goes_back_and_may_stop_early():
    sim = [[3, 0, 0], [0, 3, 0], [0, 3, 0], [3, 0, 0]]   # a look back at slide 1 stays on slide 2
    assert path(sim) == [0, 1, 1, 1]
    assert path([[3, 0, 0], [3, 0, 0]]) == [0, 0]       # never reaches slides 2 and 3


def test_a_transcript_cached_under_an_earlier_id_is_reused_when_its_settings_match(tmp_path):
    import json
    from knowlib.ingestors.media import _reuse_earlier, _seconds
    fp = {"audio": "a.m4a", "model": "large-v3"}
    (tmp_path / "old-id").mkdir()
    (tmp_path / "old-id" / "transcript.json").write_text(json.dumps({"fingerprint": fp, "value": {"segments": []}}))
    (tmp_path / "new-id").mkdir()
    _reuse_earlier(tmp_path / "new-id" / "transcript.json", {"audio": "a.m4a", "model": "turbo"})
    assert not (tmp_path / "new-id" / "transcript.json").exists()        # other settings: transcribe again
    _reuse_earlier(tmp_path / "new-id" / "transcript.json", fp)
    assert (tmp_path / "new-id" / "transcript.json").exists()
    assert (_seconds("1:02:41"), _seconds("17:48"), _seconds(30)) == (3761, 1068, 30)
