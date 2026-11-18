"""A bot check counts as passed only when the page sets a NEW x5sec, not because our old one is in the browser."""
import slider


class _Ctx:
    def __init__(self, cookies):
        self._c = cookies

    def cookies(self):
        return self._c


def test_old_pass_cookie_is_no_pass():
    assert not slider._has_pass(_Ctx([{"name": "x5sec", "value": "old"}]), "old")


def test_new_pass_cookie_is_a_pass():
    assert slider._has_pass(_Ctx([{"name": "x5sec", "value": "new"}]), "old")
    assert slider._has_pass(_Ctx([{"name": "x5sec", "value": "first"}]), None)
    assert not slider._has_pass(_Ctx([]), None)
