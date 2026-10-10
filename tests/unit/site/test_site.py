"""Static site checks; no browser or network required."""

from html.parser import HTMLParser
from pathlib import Path
from urllib.parse import unquote, urlsplit


ROOT = Path(__file__).resolve().parents[3]
SITE = ROOT / "site"


class PageParser(HTMLParser):
    def __init__(self):
        super().__init__()
        self.links = []
        self.ids = set()

    def handle_starttag(self, tag, attrs):
        values = dict(attrs)
        if "id" in values:
            self.ids.add(values["id"])
        for attr in ("href", "src"):
            if values.get(attr):
                self.links.append(values[attr])


def test_local_links_and_assets_exist():
    page = PageParser()
    page.feed((SITE / "index.html").read_text(encoding="utf-8"))
    assert page.links, "No links or assets found"
    for link in page.links:
        url = urlsplit(link)
        if url.scheme or url.netloc:
            continue
        target = (SITE / unquote(url.path)).resolve() if url.path else SITE / "index.html"
        assert target.is_relative_to(SITE), f"Link escapes the Pages artifact: {link}"
        assert target.is_file(), f"Missing local link or asset: {link}"
        if url.fragment and target == SITE / "index.html":
            assert unquote(url.fragment) in page.ids, f"Missing fragment: {link}"


def test_hero_webp_size():
    hero = SITE / "assets" / "hero.webp"
    assert 0 < hero.stat().st_size < 500_000
    data = hero.read_bytes()
    assert data[:4] == b"RIFF" and data[8:12] == b"WEBP"
