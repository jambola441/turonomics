"""Which basemap the fleet view draws.

The reason this is configuration rather than a constant: Carto's dark basemap
started answering anonymous requests with an "API KEY REQUIRED" tile served as
HTTP 200, so the map looked broken while every check said it was fine. The
lesson is not "pick a different provider" — it is that the choice should be
changeable without a deploy, and that the default should be the one that needs
no account.
"""

from __future__ import annotations

from turonomics_api.settings import map_tiles


def test_without_a_key_it_falls_back_to_tiles_that_need_no_account(monkeypatch):
    monkeypatch.delenv("THUNDERFOREST_API_KEY", raising=False)
    cfg = map_tiles()
    assert "openstreetmap.org" in cfg["url"]
    assert "apikey" not in cfg["url"]
    assert cfg["invert"] is True, "OSM's basemap is light, so the UI inverts it"


def test_a_key_switches_to_thunderforest(monkeypatch):
    monkeypatch.setenv("THUNDERFOREST_API_KEY", "abc123")
    cfg = map_tiles()
    assert "thunderforest.com" in cfg["url"]
    assert "apikey=abc123" in cfg["url"]
    assert "transport-dark" in cfg["url"]


def test_an_already_dark_theme_is_not_inverted(monkeypatch):
    """Inverting a dark basemap would turn it light — the opposite of the
    point, and the bug a single hard-coded filter would have caused."""
    monkeypatch.setenv("THUNDERFOREST_API_KEY", "abc123")
    monkeypatch.setenv("THUNDERFOREST_STYLE", "transport-dark")
    assert map_tiles()["invert"] is False
    monkeypatch.setenv("THUNDERFOREST_STYLE", "spinal-map")
    assert map_tiles()["invert"] is False


def test_a_light_theme_is_inverted(monkeypatch):
    monkeypatch.setenv("THUNDERFOREST_API_KEY", "abc123")
    monkeypatch.setenv("THUNDERFOREST_STYLE", "landscape")
    cfg = map_tiles()
    assert "landscape" in cfg["url"]
    assert cfg["invert"] is True


def test_leaflet_placeholders_survive_the_key_interpolation(monkeypatch):
    """The URL is built with str.format to insert the key, and Leaflet's own
    {z}/{x}/{y} placeholders have to come out the other side intact — getting
    the brace escaping wrong would produce a URL that fetches nothing."""
    monkeypatch.setenv("THUNDERFOREST_API_KEY", "abc123")
    url = str(map_tiles()["url"])
    assert "{z}" in url and "{x}" in url and "{y}" in url


def test_the_key_is_not_logged_or_returned_anywhere_else(monkeypatch):
    """It reaches the browser by necessity — any map key does — but it should
    appear in the tile URL and nowhere else in the payload."""
    monkeypatch.setenv("THUNDERFOREST_API_KEY", "secret-key-value")
    cfg = map_tiles()
    elsewhere = [v for k, v in cfg.items() if k != "url" and "secret-key-value" in str(v)]
    assert not elsewhere
