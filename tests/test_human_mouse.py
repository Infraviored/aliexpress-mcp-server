"""The mouse moves like a hand: ends on the target, curves and trembles a little, sometimes overshoots."""
import math
import random

import slider


def test_path_ends_on_target_and_stays_near_the_line():
    random.seed(1)
    overshoots = 0
    for _ in range(200):
        pts = slider.human_path(0, 0, 300, 100)
        assert pts[-1] == (300, 100)
        assert 12 <= len(pts) <= 60
        assert all(-120 < x < 330 and -120 < y < 220 for x, y in pts)
        overshoots += any(x > 303 for x, _ in pts)
    assert 20 < overshoots < 140                       # about a third of the moves overshoot


def test_path_is_not_a_straight_line():
    random.seed(2)
    pts = slider.human_path(0, 0, 400, 0)
    assert max(abs(y) for _, y in pts) > 1.0
    assert all(math.isfinite(x) and math.isfinite(y) for x, y in pts)
