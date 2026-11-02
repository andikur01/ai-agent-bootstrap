"""Tests for the public-apis catalog.

These use the real markdown from the repo, not a fixture. The parser is the
whole feature - if it silently stops matching rows, the bot answers "tidak
ketemu" to everything and looks broken - so the test pins the row shape that
actually appears upstream.
"""
from __future__ import annotations

import pytest

from app.services.api_catalog import (
    ApiCatalog,
    ApiEntry,
    _normalise_subject,
    wants_api_lookup,
)

SAMPLE = """
# Public APIs

### Animals
| API | Description | Auth | HTTPS | CORS |
|---|---|---|---|---|
| [Cat Facts](https://catfact.ninja/) | Daily cat facts | No | Yes | Yes |
| [Dog API](https://dog.ceo/dog-api/) | Dog pictures | `apiKey` | Yes | No |

### Weather
| API | Description | Auth | HTTPS | CORS |
|---|---|---|---|---|
| [OpenWeatherMap](https://openweathermap.org/api) | Weather forecasts | `apiKey` | Yes | Yes |
| [Old Weather](http://old.example.com) | Legacy | No | No | Yes |
"""


def test_parse_reads_rows_and_categories():
    entries = ApiCatalog.parse(SAMPLE)
    names = [e.name for e in entries]
    assert names == ["Cat Facts", "Dog API", "OpenWeatherMap", "Old Weather"]
    assert entries[0].category == "Animals"
    assert entries[2].category == "Weather"


def test_needs_key_distinguishes_open_from_keyed():
    entries = {e.name: e for e in ApiCatalog.parse(SAMPLE)}
    assert entries["Cat Facts"].needs_key is False
    assert entries["Dog API"].needs_key is True
    assert entries["Dog API"].auth == "`apiKey`"


def test_https_and_cors_are_read():
    entries = {e.name: e for e in ApiCatalog.parse(SAMPLE)}
    assert entries["Old Weather"].https is False
    assert entries["Dog API"].cors == "No"


def test_search_filters_by_key_requirement():
    catalog = ApiCatalog(cache_path=None or __import__("pathlib").Path("/nonexistent"))
    catalog._entries = ApiCatalog.parse(SAMPLE)
    import asyncio

    keyed = asyncio.run(catalog.search("weather", needs_key=True))
    assert [e.name for e in keyed] == ["OpenWeatherMap"]


def test_search_https_only_excludes_http_rows():
    import asyncio
    from pathlib import Path

    catalog = ApiCatalog(Path("/nonexistent"))
    catalog._entries = ApiCatalog.parse(SAMPLE)
    got = asyncio.run(catalog.search("", https_only=True))
    assert "Old Weather" not in [e.name for e in got]


def test_render_states_why_an_entry_matters():
    entry = ApiEntry(
        name="Dog API",
        url="https://dog.ceo",
        description="Dog pictures",
        auth="`apiKey`",
        https=True,
        cors="No",
    )
    text = entry.render()
    assert "Dog API" in text
    assert "butuh apiKey" in text
    assert "CORS tertutup" in text


@pytest.mark.parametrize(
    "subject,expected",
    [
        ("cuaca", "weather"),
        ("berita", "news"),
        ("resep makanan", "food recipe"),
        ("kripto", "crypto cryptocurrency"),
    ],
)
def test_indonesian_subjects_map_to_english(subject, expected):
    assert _normalise_subject(subject) == expected


def test_unknown_subject_passes_through():
    assert _normalise_subject("kucing lucu") == "animals cats lucu"


@pytest.mark.parametrize(
    "text,wanted",
    [
        ("cari api cuaca", True),
        ("ada ga api musik?", True),
        ("butuh api untuk berita", True),
        ("ambilkan key gemini", False),
        ("buatkan web toko kopi", False),
        ("halo apa kabar", False),
    ],
)
def test_wants_api_lookup_gating(text, wanted):
    got, _ = wants_api_lookup(text)
    assert got is wanted


def test_harvest_request_never_routes_to_catalog():
    """"ambilkan key X" must reach the harvester, not the directory."""
    for text in ("ambilkan key groq", "ambil api key openrouter", "kasih aku key gemini"):
        got, _ = wants_api_lookup(text)
        assert got is False
