"""real_cost prices the main cart with and without lines; the difference is the real cost."""
import aliexpress_mcp_server as m

fn = getattr(m.real_cost, "fn", m.real_cost)


def ln(cid, item, sku, qty, price, valid=True):
    return {"cart_id": cid, "item_id": item, "sku_id": sku, "qty": qty, "price": price, "valid": valid,
            "checked": False, "title": f"Item {item}", "variant": f"v{sku}"}


def parsed(goods, ship, duty, total, disc=0.0, duty_off=0.0):
    return {"subtotal": goods, "discounts": disc, "shipping": ship, "duty": duty, "duty_off": duty_off,
            "total": total, "currency": "EUR", "lines": []}


class Env:
    def __init__(self, mp, lines, fail_render=False):
        self.lines, self.added, self.removed, self.renders = list(lines), [], [], []
        self.fail_render = fail_render
        mp.setattr(m, "load_cookies", lambda: {"a": "b"})
        mp.setattr(m, "_read_cart", lambda cookies, business="": {"lines": list(self.lines)})
        mp.setattr(m, "_render_order", self.render)
        mp.setattr(m, "add_to_cart", self.add)
        mp.setattr(m, "remove_from_cart", self.remove)

    def render(self, cookies, ids):
        if self.fail_render:
            raise RuntimeError("boom")
        self.renders.append(sorted(ids))
        by = {x["cart_id"]: x for x in self.lines}
        goods = sum(by[i]["price"] * by[i]["qty"] for i in ids)
        ship = 2.0 if ids else 0.0
        duty = 1.5 if len(ids) > 1 else 0.5 * len(ids)
        return parsed(goods, ship, duty, goods + ship + duty)

    def add(self, item_id, sku_id, quantity=1, list_name="", need="", combo=False, force=False):
        self.added.append((item_id, sku_id, quantity, combo))
        self.lines.append(ln("900", item_id, sku_id, quantity, 3.0))
        return "Added and checked: x. cart_id 900."

    def remove(self, cart_ids):
        self.removed.append(list(cart_ids))
        return "Removed"


BASE = [ln("1", "10", "100", 2, 5.0), ln("2", "20", "200", 1, 4.0), ln("3", "30", "300", 1, 9.0, valid=False)]


def test_mode_a_difference(monkeypatch):
    env = Env(monkeypatch, BASE)
    out = fn(cart_ids=["2"])
    assert env.renders == [["1", "2"], ["1"]]          # invalid line 3 not in the base
    assert env.added == [] and env.removed == []
    assert "DIFFERENCE" in out and "goods" in out and "duty reduction" in out
    # with: 14 + 2 + 1.5 = 17.5; without: 10 + 2 + 0.5 = 12.5 -> 5.00 for 1 piece
    assert "real cost of these lines: 5.00 EUR for 1 pieces = 5.00 EUR per piece" in out
    assert "Item 20" in out


def test_mode_a_unknown_id(monkeypatch):
    env = Env(monkeypatch, BASE)
    out = fn(cart_ids=["2", "777"])
    assert "777" in out and env.renders == []


def test_mode_b_adds_measures_removes(monkeypatch):
    env = Env(monkeypatch, BASE)
    out = fn(item_id="40", sku_id="400", quantity=3)
    assert env.added == [("40", "400", 3, False)]
    assert env.removed == [["900"]]
    assert env.renders == [["1", "2", "900"], ["1", "2"]]
    assert "3 pieces" in out


def test_mode_b_keep_does_not_remove(monkeypatch):
    env = Env(monkeypatch, BASE)
    fn(item_id="40", sku_id="400", quantity=1, keep=True)
    assert env.added and env.removed == []


def test_mode_b_existing_line_not_added(monkeypatch):
    env = Env(monkeypatch, BASE)
    out = fn(item_id="10", sku_id="100", quantity=5)
    assert env.added == [] and env.removed == []
    assert env.renders == [["1", "2"], ["2"]]
    assert "already in the cart" in out and "2 pieces" in out


def test_removal_when_render_fails(monkeypatch):
    env = Env(monkeypatch, BASE, fail_render=True)
    out = fn(item_id="40", sku_id="400", quantity=1)
    assert env.removed == [["900"]]
    assert "failed" in out
