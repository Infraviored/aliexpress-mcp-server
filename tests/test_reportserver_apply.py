import json
import threading
import time
import urllib.error
import urllib.request

import reportserver


def _srv(tmp_path, run):
    tok = tmp_path / "tok"
    s = reportserver.server(tmp_path, tok, port=0, applier=reportserver.Applier(run, tmp_path / "applied.log"))
    threading.Thread(target=s.serve_forever, daemon=True).start()
    return s, f"http://127.0.0.1:{s.server_address[1]}/{reportserver.token(tok)}"


def _post(url, data, header=True):
    req = urllib.request.Request(url, data=json.dumps(data).encode(), method="POST",
                                 headers={"Content-Type": "application/json", **({"X-Decide": "1"} if header else {})})
    try:
        with urllib.request.urlopen(req, timeout=3) as r:
            return r.status, json.loads(r.read())
    except urllib.error.HTTPError as e:
        return e.code, None


def test_page_runs_the_selection_and_polls_the_result(tmp_path):
    seen = []
    s, base = _srv(tmp_path, lambda text: (seen.append(text), "Checked 3 line(s)")[1])
    try:
        assert json.loads(urllib.request.urlopen(base + "/apply", timeout=3).read()) == {"ok": True, "busy": False}
        code, j = _post(base + "/apply", {"text": "Auswahl ..."})
        assert code == 202
        for _ in range(50):
            res = json.loads(urllib.request.urlopen(base + "/apply/" + j["job"], timeout=3).read())
            if res["done"]:
                break
            time.sleep(0.05)
        assert res["ok"] and res["result"] == "Checked 3 line(s)" and seen == ["Auswahl ..."]
        assert "Checked 3" in (tmp_path / "applied.log").read_text()
    finally:
        s.shutdown()


def test_apply_needs_the_token_and_the_header_and_runs_one_at_a_time(tmp_path):
    gate = threading.Event()
    s, base = _srv(tmp_path, lambda text: (gate.wait(3), "ok")[1])
    try:
        assert _post(base + "/apply", {"text": "x"}, header=False)[0] == 404      # a foreign page cannot set the header
        assert _post(base.rsplit("/", 1)[0] + "/wrong/apply", {"text": "x"})[0] == 404
        assert _post(base + "/apply", {"text": "x"})[0] == 202
        assert _post(base + "/apply", {"text": "y"})[0] == 409                   # busy
        gate.set()
    finally:
        s.shutdown()


def test_without_an_applier_there_is_no_apply_endpoint(tmp_path):
    tok = tmp_path / "tok"
    s = reportserver.server(tmp_path, tok, port=0)
    threading.Thread(target=s.serve_forever, daemon=True).start()
    try:
        base = f"http://127.0.0.1:{s.server_address[1]}/{reportserver.token(tok)}"
        assert _post(base + "/apply", {"text": "x"})[0] == 404
    finally:
        s.shutdown()
