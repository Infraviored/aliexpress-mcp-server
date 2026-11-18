"""decide_report fills delivery date, Choice and shipping from the product page; given values win."""
import datetime

import aliexpress_mcp_server as m


def _offer(choice, days, fee):
    return {"choice": choice, "shipping": {"days": days, "fee": fee, "free": fee == 0}}


def test_fills_what_the_card_left_out(monkeypatch):
    pages = {"1": _offer(False, (12, 19), 4.12), "2": _offer(True, (5, 9), 1.99), "3": None}
    monkeypatch.setattr(m, "_pdp_offer", lambda i: pages[i])
    cards = [{"item_id": "1"}, {"item_id": "2"}, {"item_id": "3"},
             {"item_id": "1", "ship": 0, "choice": True}, {"item_id": "1", "until": "2026-12-24"}]
    undated = m._attach_offer_facts(cards)
    today = datetime.date.today()
    assert cards[0]["until"] == (today + datetime.timedelta(days=19)).isoformat()
    assert cards[0]["choice"] is False and cards[0]["ship"] == 4.12
    assert cards[1]["choice"] is True and "ship" not in cards[1]       # Choice: shipping is shared, not per line
    assert cards[1]["until"] == (today + datetime.timedelta(days=9)).isoformat()
    assert "until" not in cards[2]
    assert cards[3]["ship"] == 0 and cards[3]["choice"] is True           # given values stay
    assert cards[4]["until"] == "2026-12-24"
    assert undated == 1
