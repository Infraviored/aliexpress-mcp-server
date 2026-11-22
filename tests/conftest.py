import pytest
import httpx

import aliexpress_mcp_server as m


@pytest.fixture(autouse=True)
def _clean_combo_items():
    """The search parser records Kombi item ids globally: no test may see another's."""
    m.COMBO_ITEMS.clear()
    m._FREIGHT_CACHE.clear()
    m.BOT_CHECK.clear()
    yield
    m.COMBO_ITEMS.clear()
    m._FREIGHT_CACHE.clear()
    m.BOT_CHECK.clear()


@pytest.fixture(autouse=True)
def _no_live_requests(monkeypatch):
    """Ensure no test ever makes live HTTP requests to AliExpress."""
    def fake_send(self, request, *args, **kwargs):
        raise RuntimeError(f"LIVE HTTP REQUEST DETECTED: {request.url}")
    monkeypatch.setattr(httpx.Client, "send", fake_send)


@pytest.fixture(autouse=True)
def _private_state_files(tmp_path, monkeypatch):
    """Tests never read or write the live bot-check, pacing, Kombi or product-page files: a real
    bot check running on this machine made 9 tests fail, and tests wrote into the live Kombi cache."""
    for name in ("_PACE_FILE", "_BOT_FILE", "_COMBO_FILE", "_PDP_FILE"):
        monkeypatch.setattr(m, name, tmp_path / name.strip("_").lower())
