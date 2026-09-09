"""register_row_action end to end: a plugin's entry lists in /api/plugins,
the dispatch route resolves (source_id, rid) pairs to full cells — merges
included — enforces max_rows, and maps ValueError to 400."""

from __future__ import annotations

import textwrap

import pytest

from winnow.plugin_api import PluginRegistry

PLUGIN = textwrap.dedent("""
    PLUGIN = {"name": "vt-demo", "version": "0.1"}
    SEEN = []

    def lookup(req):
        b = req.body
        if b.get("column") == "boom":
            raise ValueError("no column called boom")
        SEEN.append(b)
        vals = sorted({r["cells"].get(b["column"] or "Host") for r in b["rows"]})
        return {"message": f"{len(b['rows'])} rows / {len(vals)} distinct", "open_url": "https://example.test/x"}

    def register(api):
        api.register_row_action(id="vt", label="Look up on VT", handler=lookup,
                                description="demo", max_rows=3)
""")


@pytest.fixture
def ra_client(client, store, write_csv, tmp_path, monkeypatch):
    import server
    (tmp_path / "vt_demo.py").write_text(PLUGIN)
    reg = PluginRegistry()
    reg.load([tmp_path])
    rec = next(p for p in reg.describe() if p["fs_name"] == "vt_demo")
    assert rec["error"] is None, rec["error"]
    monkeypatch.setattr(server, "PLUGINS", reg)
    sid = store.ingest_csv(write_csv([["Host", "Hash"], ["A", "h1"], ["B", "h2"], ["A", "h3"], ["C", "h4"]],
                                     "ra.csv"), name="ra", build_fts=False)["id"]
    return client, store, sid, reg


def test_row_action_is_listed(ra_client):
    client, _, _, _ = ra_client
    acts = client.get("/api/plugins").json()["row_actions"]
    assert len(acts) == 1
    a = acts[0]
    assert a["id"] == "vt-demo.vt" and a["local_id"] == "vt" and a["plugin_fs"] == "vt_demo"
    assert a["max_rows"] == 3 and "handler" not in a


def test_dispatch_resolves_rows_and_returns_ui_keys(ra_client):
    client, _, sid, _ = ra_client
    r = client.post("/api/plugins/row_action/vt_demo/vt",
                    json={"source_id": sid, "pairs": [[sid, 1], [sid, 3]], "column": "Host", "value": "A"})
    assert r.status_code == 200, r.text
    out = r.json()
    assert out["message"] == "2 rows / 1 distinct"        # both rows are Host A
    assert out["open_url"].startswith("https://")


def test_merge_pairs_resolve_per_member(ra_client, store, write_csv):
    client, _, sid, _ = ra_client
    sid2 = store.ingest_csv(write_csv([["Host", "Hash"], ["Z", "h9"]], "ra2.csv"), name="ra2", build_fts=False)["id"]
    mid = store.create_merge("ram", [sid, sid2])["id"]
    r = client.post("/api/plugins/row_action/vt_demo/vt",
                    json={"source_id": mid, "pairs": [[sid, 2], [sid2, 1]], "column": "Host"})
    assert r.status_code == 200, r.text
    assert r.json()["message"] == "2 rows / 2 distinct"     # B and Z


def test_max_rows_is_enforced_server_side(ra_client):
    client, _, sid, _ = ra_client
    r = client.post("/api/plugins/row_action/vt_demo/vt",
                    json={"source_id": sid, "pairs": [[sid, i] for i in range(1, 5)]})
    assert r.status_code == 400 and "at most 3" in r.json()["detail"]


def test_handler_value_error_is_a_400_and_unknown_action_a_404(ra_client):
    client, _, sid, _ = ra_client
    r = client.post("/api/plugins/row_action/vt_demo/vt",
                    json={"source_id": sid, "pairs": [[sid, 1]], "column": "boom"})
    assert r.status_code == 400 and "boom" in r.json()["detail"]
    r = client.post("/api/plugins/row_action/vt_demo/nope", json={"source_id": sid, "pairs": []})
    assert r.status_code == 404


def test_registration_validates(tmp_path):
    (tmp_path / "bad.py").write_text(textwrap.dedent("""
        def register(api):
            api.register_row_action(id="Bad Id", label="x", handler=lambda r: None)
    """))
    reg = PluginRegistry()
    reg.load([tmp_path])
    rec = next(p for p in reg.describe() if p["fs_name"] == "bad")
    assert rec["error"] and "lowercase" in rec["error"]
