import json
import time

import aliexpress_mcp_server as m


def _pace(monkeypatch, tmp_path, per_minute):
    monkeypatch.setattr(m, "_PACE_FILE", tmp_path / "pace")
    monkeypatch.setattr(m, "MTOP_MIN_GAP", 0.0)
    monkeypatch.setattr(m, "MTOP_JITTER", 0.0)
    monkeypatch.setattr(m, "MTOP_PER_MINUTE", per_minute)
    slept = []
    monkeypatch.setattr(m.time, "sleep", lambda s: slept.append(s))
    return slept


def test_full_minute_window_makes_the_next_call_wait(monkeypatch, tmp_path):
    slept = _pace(monkeypatch, tmp_path, 3)
    now = time.time()
    (tmp_path / "pace").write_text(json.dumps([now - 50, now - 20, now - 5]))
    import fcntl, random
    m._mtop_pace_locked(fcntl, random)
    assert slept and 9 < slept[0] <= 10.5            # oldest call is 50 s old: 10 s until it leaves the window
    assert len(json.loads((tmp_path / "pace").read_text())) == 3


def test_open_window_does_not_wait_and_old_format_is_read(monkeypatch, tmp_path):
    slept = _pace(monkeypatch, tmp_path, 3)
    (tmp_path / "pace").write_text(str(time.time() - 100))      # the old single-number file
    import fcntl, random
    m._mtop_pace_locked(fcntl, random)
    assert not slept


def test_quick_call_prices_only_a_few_new_product_pages(monkeypatch):
    seen = []
    monkeypatch.setattr(m, "FRESH_PAGES_QUICK", 2)
    monkeypatch.setattr(m, "_PDP_CACHE", {"c": (time.time(), {})})
    monkeypatch.setattr(m.time, "sleep", lambda s: None)

    def ways(item_id, spec, x):
        seen.append(item_id)
        return [{"net": 1.0, "pieces": 1}], []
    monkeypatch.setattr(m, "_ways_for", ways)
    hits = [{"item_id": i, "title": "t", "choice": False} for i in ("a", "b", "c", "d", "e")]
    rows, looked, notes = m._price_hits(hits, 1, "", "", 8, False)
    assert seen == ["a", "b", "c"]                  # two new pages plus the cached one
    assert len(rows) == 3 and sum("not priced" in n for n in notes) == 2


def test_background_job_is_not_limited_per_request(monkeypatch):
    seen = []
    monkeypatch.setattr(m, "FRESH_PAGES_QUICK", 2)
    monkeypatch.setattr(m, "_PDP_CACHE", {})
    monkeypatch.setattr(m.time, "sleep", lambda s: None)
    monkeypatch.setattr(m, "_ways_for", lambda i, s, x: (seen.append(i), ([{"net": 1.0, "pieces": 1}], []))[1])
    m._CALL.background = True
    try:
        m._price_hits([{"item_id": i, "title": "t"} for i in "abcde"], 1, "", "", 8, False)
    finally:
        m._CALL.background = False
    assert len(seen) == 5
