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
    monkeypatch.delenv("MAP_INVERT", raising=False)
    cfg = map_tiles()
    assert "openstreetmap.org" in cfg["url"]
    assert "apikey" not in cfg["url"]


def test_a_key_switches_to_thunderforest(monkeypatch):
    monkeypatch.setenv("THUNDERFOREST_API_KEY", "abc123")
    cfg = map_tiles()
    assert "thunderforest.com" in cfg["url"]
    assert "apikey=abc123" in cfg["url"]
    assert "transport-dark" in cfg["url"]


def test_the_style_is_shown_as_chosen_rather_than_second_guessed(monkeypatch):
    """An earlier version inverted anything not on a list of known-dark theme
    names, so choosing a light style silently got you a dark map and the only
    way to see what you picked was to edit the code. A display preference
    should be a preference, not an inference."""
    monkeypatch.delenv("MAP_INVERT", raising=False)
    monkeypatch.setenv("THUNDERFOREST_API_KEY", "abc123")
    for style in ("landscape", "atlas", "transport-dark", "spinal-map"):
        monkeypatch.setenv("THUNDERFOREST_STYLE", style)
        cfg = map_tiles()
        assert style in str(cfg["url"])
        assert cfg["invert"] is False, f"{style} should render as published"


def test_inversion_is_available_when_asked_for(monkeypatch):
    """Still the only way to get a dark map out of the keyless default."""
    monkeypatch.delenv("THUNDERFOREST_API_KEY", raising=False)
    monkeypatch.setenv("MAP_INVERT", "true")
    assert map_tiles()["invert"] is True


def test_inversion_applies_to_thunderforest_too(monkeypatch):
    monkeypatch.setenv("THUNDERFOREST_API_KEY", "abc123")
    monkeypatch.setenv("THUNDERFOREST_STYLE", "landscape")
    monkeypatch.setenv("MAP_INVERT", "1")
    assert map_tiles()["invert"] is True


def test_leaflet_placeholders_survive_the_key_interpolation(monkeypatch):
    """The URL is built with str.format to insert the key, and Leaflet's own
    {z}/{x}/{y} placeholders have to come out the other side intact — getting
    the brace escaping wrong would produce a URL that fetches nothing."""
    monkeypatch.setenv("THUNDERFOREST_API_KEY", "abc123")
    url = str(map_tiles()["url"])
    assert "{z}" in url and "{x}" in url and "{y}" in url


def test_the_key_is_not_returned_anywhere_but_the_url(monkeypatch):
    """It reaches the browser by necessity — any map key does — but it should
    appear in the tile URL and nowhere else in the payload."""
    monkeypatch.setenv("THUNDERFOREST_API_KEY", "secret-key-value")
    cfg = map_tiles()
    elsewhere = [v for k, v in cfg.items() if k != "url" and "secret-key-value" in str(v)]
    assert not elsewhere
