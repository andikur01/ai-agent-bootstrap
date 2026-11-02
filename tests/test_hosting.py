"""Tests for the self-hosted site serving.

These lock in three defects that were live in production and each of which the
user could see:

* every asset request returned index.html, so sites loaded with no CSS or JS;
* re-publishing a site deleted its own source directory;
* a path that climbed out of the site directory was served anyway.
"""
from __future__ import annotations

import asyncio
from pathlib import Path

import httpx
import pytest

from app.services.hosting_service import SiteHost

PORT = 12721


def _write_site(root: Path) -> None:
    root.mkdir(parents=True, exist_ok=True)
    (root / "index.html").write_text(
        "<!DOCTYPE html><html><head><link rel='stylesheet' href='styles.css'>"
        "</head><body><h1>Halo</h1><script src='app.js'></script></body></html>"
    )
    (root / "styles.css").write_text("body { background: #121212; }")
    (root / "app.js").write_text("console.log('siap');")


async def _serve(sites_root: Path, source: Path, name: str = "demo"):
    host = SiteHost(sites_root, port=PORT, public_base_url="http://127.0.0.1")
    assert await host.start(), "port did not bind"
    url = host.publish(source, project_name=name)
    return host, url


@pytest.mark.asyncio
async def test_assets_are_served_as_themselves(tmp_path):
    source = tmp_path / "build"
    _write_site(source)
    host, _ = await _serve(tmp_path / "sites", source)
    try:
        async with httpx.AsyncClient(timeout=10) as client:
            base = f"http://127.0.0.1:{PORT}/s/demo/"
            css = await client.get(base + "styles.css")
            js = await client.get(base + "app.js")
            html = await client.get(base)
        assert css.status_code == 200
        assert "text/css" in css.headers.get("content-type", "")
        assert "background" in css.text
        assert js.status_code == 200
        assert "javascript" in js.headers.get("content-type", "")
        assert "siap" in js.text
        # The bug was that this returned index.html for everything.
        assert "<h1>Halo</h1>" in html.text
        assert "background" not in html.text
    finally:
        await host.stop()


@pytest.mark.asyncio
async def test_republish_does_not_delete_its_own_source(tmp_path):
    sites_root = tmp_path / "sites"
    source = sites_root / "demo"
    _write_site(source)
    host = SiteHost(sites_root, port=PORT, public_base_url="http://127.0.0.1")
    assert await host.start()
    try:
        # Publishing the served directory onto itself must be a no-op, not an
        # rmtree followed by a copy of nothing.
        url = host.publish(source, project_name="demo")
        assert url.endswith("/s/demo/")
        assert (source / "index.html").exists()
        assert (source / "styles.css").exists()
    finally:
        await host.stop()


@pytest.mark.asyncio
async def test_path_traversal_is_rejected(tmp_path):
    source = tmp_path / "build"
    _write_site(source)
    secret = tmp_path / "rahasia.txt"
    secret.write_text("jangan dibaca")
    host, _ = await _serve(tmp_path / "sites", source)
    try:
        async with httpx.AsyncClient(timeout=10) as client:
            response = await client.get(
                f"http://127.0.0.1:{PORT}/s/demo/../../rahasia.txt"
            )
        assert response.status_code == 404
        assert "jangan dibaca" not in response.text
    finally:
        await host.stop()


@pytest.mark.asyncio
async def test_public_index_moves_to_root(tmp_path):
    source = tmp_path / "build"
    (source / "public").mkdir(parents=True)
    (source / "public" / "index.html").write_text("<h1>Publik</h1>")
    (source / "main.py").write_text("print('x')")
    host, _url = await _serve(tmp_path / "sites", source)
    try:
        async with httpx.AsyncClient(timeout=10) as client:
            response = await client.get(f"http://127.0.0.1:{PORT}/s/demo/")
        assert response.status_code == 200
        assert "Publik" in response.text
    finally:
        await host.stop()


@pytest.mark.asyncio
async def test_second_host_cannot_bind_the_same_port(tmp_path):
    source = tmp_path / "build"
    _write_site(source)
    host, _ = await _serve(tmp_path / "sites", source)
    try:
        other = SiteHost(tmp_path / "sites2", port=PORT)
        # A taken port must be reported, not raised, so the caller can fall back.
        assert await other.start() is False
    finally:
        await host.stop()


def test_favicon_is_served_not_404(tmp_path):
    """Browsers ask for /favicon.ico unprompted; a 404 fails QA on every site."""
    import asyncio

    from app.services.hosting_service import SiteHost

    source = tmp_path / "build"
    _write_site(source)

    async def run():
        host = SiteHost(tmp_path / "sites", port=PORT, public_base_url="http://127.0.0.1")
        assert await host.start()
        try:
            async with httpx.AsyncClient(timeout=10) as client:
                root = await client.get(f"http://127.0.0.1:{PORT}/favicon.ico")
                scoped = await client.get(f"http://127.0.0.1:{PORT}/s/demo/favicon.ico")
            assert root.status_code == 200
            assert scoped.status_code == 200
            assert "svg" in root.headers.get("content-type", "")
        finally:
            await host.stop()

    asyncio.run(run())


def test_login_page_counts_as_content():
    """A short page full of controls is not an empty page.

    A login/dashboard page has little prose but real interactive elements.
    Judging it on text length alone flagged it as empty and sent the repair
    loop chasing a defect that did not exist.
    """
    from app.services.browser_qa import PageReport

    login = PageReport(
        url="http://x",
        viewport="desktop",
        status=200,
        text_length=44,
        interactive_count=14,
    )
    assert login.ok
    assert login.issues() == []

    blank = PageReport(url="http://x", viewport="desktop", status=200, text_length=10)
    assert not blank.ok
    assert any("kosong" in issue for issue in blank.issues())


def test_real_console_error_still_fails_even_with_controls():
    from app.services.browser_qa import PageReport

    page = PageReport(
        url="http://x",
        viewport="desktop",
        status=200,
        text_length=5000,
        interactive_count=30,
        console_errors=["Uncaught TypeError: x is not a function"],
    )
    assert not page.ok
    assert any("console" in issue for issue in page.issues())
