import pytest

import aliexpress_mcp_server as m


@pytest.fixture(autouse=True)
def _clean_combo_items():
    """The search parser records Kombi item ids globally: no test may see another's."""
    m.COMBO_ITEMS.clear()
    m._FREIGHT_CACHE.clear()
    yield
    m.COMBO_ITEMS.clear()
    m._FREIGHT_CACHE.clear()
