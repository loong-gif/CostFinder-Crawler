from pathlib import Path

from utils import service_endpoints


def test_off_the_monitor_host_services_are_called_on_the_public_address():
    urls = service_endpoints.endpoints(Path("/Users/wyl/BrandRap/costfinder"))
    assert urls == {"firecrawl": "http://72.52.161.65:3002", "gmaps": "http://72.52.161.65:8081",
                    "ocr": "http://72.52.161.65:8082"}


def test_jobs_on_the_monitor_host_call_the_services_locally():
    urls = service_endpoints.endpoints(Path("/home/loong/costfinder_monitor"))
    assert urls == {"firecrawl": "http://127.0.0.1:3002", "gmaps": "http://127.0.0.1:8081",
                    "ocr": "http://127.0.0.1:8082"}


def test_clients_take_their_address_from_the_one_place():
    from utils import gmaps_scraper_client, paddle_ocr_markdown
    current = service_endpoints.endpoints(service_endpoints.PROJECT_ROOT)
    assert gmaps_scraper_client.GMAPS_SCRAPER_URL == current["gmaps"]
    assert paddle_ocr_markdown.OCR_SERVICE_URL == current["ocr"]
    assert service_endpoints.FIRECRAWL_URL == current["firecrawl"]
