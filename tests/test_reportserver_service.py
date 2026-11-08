import threading
import urllib.request

import reportserver


def test_standalone_server_serves_pages_with_the_token(tmp_path):
    (tmp_path / "p.html").write_text("hallo")
    tok = tmp_path / "tok"
    s = reportserver.server(tmp_path, tok, port=0)
    threading.Thread(target=s.serve_forever, daemon=True).start()
    try:
        port = s.server_address[1]
        t = reportserver.token(tok)
        assert urllib.request.urlopen(f"http://127.0.0.1:{port}/{t}/p.html", timeout=3).read() == b"hallo"
        assert urllib.request.urlopen(f"http://127.0.0.1:{port}/{t}/.ping", timeout=3).read() == b"ok"
    finally:
        s.shutdown()
