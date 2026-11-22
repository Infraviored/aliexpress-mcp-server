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
