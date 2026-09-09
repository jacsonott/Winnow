"""The look (skin/theme/accent) mirrored into machine app settings, so a
different origin — an association quick-look on a free port — can adopt
it instead of booting into the default skin."""

from __future__ import annotations


def test_appearance_round_trips_and_filters_keys(client):
    r = client.post("/api/settings/app", json={"appearance": {
        "style": "phosphor", "themeMode": "dark", "accent": "#39e881",
        "accentCustomized": False, "splash": False, "remoteSession": True, "junk": 1}})
    assert r.status_code == 200, r.text
    look = r.json()["appearance"]
    assert look["style"] == "phosphor" and look["splash"] is False
    # remoteSession is a separate machine fact; unknown keys are dropped.
    assert "remoteSession" not in look and "junk" not in look
    assert client.get("/api/settings/app").json()["appearance"]["style"] == "phosphor"


def test_other_settings_survive_an_appearance_write(client):
    client.post("/api/settings/app", json={"default_ts_format": "us"})
    client.post("/api/settings/app", json={"appearance": {"style": "studio"}})
    got = client.get("/api/settings/app").json()
    assert got["default_ts_format"] == "us" and got["appearance"]["style"] == "studio"


def test_non_object_appearance_is_rejected(client):
    r = client.post("/api/settings/app", json={"appearance": "phosphor"})
    assert r.status_code in (400, 422)
