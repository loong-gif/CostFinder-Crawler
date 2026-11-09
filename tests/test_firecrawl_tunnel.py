import json
import sys
from contextlib import contextmanager

from scripts import scrape_urls_to_raw
from utils.firecrawl_client import FIRECRAWL_REMOTE_PORT, firecrawl_tunnel
from utils.ssh_tunnel import MONITOR_HOST, tunnel_command


def test_firecrawl_tunnel_forwards_the_local_api_port_to_the_server_firecrawl():
    assert FIRECRAWL_REMOTE_PORT == 3002
    assert tunnel_command("http://127.0.0.1:13002", FIRECRAWL_REMOTE_PORT) == [
        "ssh", "-N", "-o", "ExitOnForwardFailure=yes", "-o", "BatchMode=yes",
        "-L", "127.0.0.1:13002:127.0.0.1:3002", MONITOR_HOST]


def test_reachable_firecrawl_needs_no_tunnel(monkeypatch):
    monkeypatch.setattr("utils.ssh_tunnel.service_up", lambda url: True)
    with firecrawl_tunnel("http://127.0.0.1:3002", command=[sys.executable, "-c", "raise SystemExit(9)"]):
        pass


def test_crawl_scrapes_inside_the_firecrawl_tunnel(tmp_path, monkeypatch):
    opened = []

    @contextmanager
    def tunnel(api_url):
        opened.append(api_url)
        yield
        opened.append("closed")

    class Firecrawl:
        api_url = "http://127.0.0.1:13002"

        def scrape(self, url, *a, **kw):
            assert opened == [self.api_url], "scraped before the tunnel was open"
            return {"markdown": ""}

    decisions = tmp_path / "decisions.json"
    decisions.write_text(json.dumps({"decisions": [{"url": "https://a.test/specials", "business_id": 1,
                                                    "kind": "promotion", "decision": "add"}]}))
    monkeypatch.setenv("COSTFINDER_RAW_DIR", str(tmp_path / "raw"))
    monkeypatch.setattr(scrape_urls_to_raw, "firecrawl_tunnel", tunnel)
    monkeypatch.setattr(scrape_urls_to_raw, "self_hosted_firecrawl", lambda browser: Firecrawl())
    monkeypatch.setattr(sys, "argv", ["crawl", "--decisions", str(decisions)])
    assert scrape_urls_to_raw.main() == 0
    assert opened == ["http://127.0.0.1:13002", "closed"]
