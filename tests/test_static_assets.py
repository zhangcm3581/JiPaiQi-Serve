"""A refreshed page must fetch one complete asset version, including JS imports."""

import os
import re
import shutil
from urllib.parse import urljoin

import pytest
from fastapi.testclient import TestClient

from app import main


@pytest.fixture
def site(tmp_path, monkeypatch):
    root = tmp_path / "site"
    for name in ("static", "templates"):
        shutil.copytree(main.ROOT / name, root / name)
    monkeypatch.setattr(main, "ROOT", root)
    return root


def entry_urls(client):
    page = client.get("/live")
    assert page.status_code == 200
    css = re.search(r'href="([^"]+/css/app.css)"', page.text).group(1)
    script = re.search(r'src="([^"]+/js/app.js)"', page.text).group(1)
    return css, script


def test_page_and_entire_module_graph_have_a_release_namespace(tmp_path, site):
    with TestClient(main.create_app(tmp_path / "cache.sqlite3")) as client:
        css, script = entry_urls(client)
        assert re.fullmatch(r"/static/[0-9a-f]{16}/css/app.css", css)
        prefix = css.removesuffix("/css/app.css")
        assert script == prefix + "/js/app.js"
        for path in ("/", "/live", "/tenants", "/history", "/static/css/app.css"):
            assert client.get(path).headers["cache-control"] == "no-cache"
        assert (
            client.get("/static/js/components/cards.js").headers["cache-control"]
            == "no-cache"
        )
        pending, visited = [script], set()
        while pending:
            path = pending.pop()
            if path in visited:
                continue
            visited.add(path)
            assert path.startswith(prefix + "/")
            asset = client.get(path)
            assert asset.status_code == 200, path
            assert "immutable" in asset.headers["cache-control"]
            for imported in re.findall(r'from\s+["\']([^"\']+)["\']', asset.text):
                pending.append(urljoin(path, imported))
        assert prefix + "/js/components/cards.js" in visited
        assert len(visited) == 7
        first = client.get(css)
        cached = client.get(css, headers={"If-None-Match": first.headers["etag"]})
        assert cached.status_code == 304
        assert "immutable" in cached.headers["cache-control"]


def test_nested_module_change_invalidates_all_entry_urls(tmp_path, site):
    with TestClient(main.create_app(tmp_path / "before.sqlite3")) as client:
        old_css, old_script = entry_urls(client)
    cards = site / "static/js/components/cards.js"
    cards.write_text(cards.read_text() + "\n// new deployed module\n")
    with TestClient(main.create_app(tmp_path / "after.sqlite3")) as client:
        css, script = entry_urls(client)
        assert script != old_script
        assert css != old_css
        assert client.get(old_script).status_code == 404
        nested = urljoin(script, "./components/cards.js")
        assert client.get(nested).text.endswith("// new deployed module\n")


def test_unchanged_contents_keep_the_same_asset_urls(tmp_path, site):
    with TestClient(main.create_app(tmp_path / "before.sqlite3")) as client:
        previous = entry_urls(client)
    for path in (site / "static").rglob("*"):
        if path.is_file():
            os.utime(path, (1_600_000_000, 1_600_000_000))
    with TestClient(main.create_app(tmp_path / "after.sqlite3")) as client:
        assert entry_urls(client) == previous
