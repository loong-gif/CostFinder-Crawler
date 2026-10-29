from __future__ import annotations

import json

import pytest

from utils.gmaps_scraper_client import GmapsClient, GmapsJobError, parse_csv, rows_to_places

CSV_TEXT = (
    "title,category,address,website,phone,review_count,review_rating,place_id,complete_address\n"
    'Simplicity Laser,Laser hair removal service,"870 Seven Hills Dr #201, Henderson, NV 89052",'
    "https://www.simplicitylaser.com/,(800) 559-9138,651,4.800000,PID1,"
    '"{""city"":""Henderson"",""state"":""Nevada"",""country"":""US""}"\n'
    "Duplicate,,,,,10,3.0,PID1,{}\n"
    "No Place Id,,,,,5,5.0,,{}\n"
)


class _Resp:
    def __init__(self, payload=None, text=""):
        self._payload, self.text = payload, text

    def raise_for_status(self) -> None:
        pass

    def json(self):
        return self._payload


class _Session:
    def __init__(self, statuses):
        self.statuses, self.posted, self.trust_env = list(statuses), [], True

    def post(self, url, json=None, timeout=None):
        self.posted.append((url, json))
        return _Resp({"id": "job-1"})

    def get(self, url, timeout=None):
        if url.endswith("/download"):
            return _Resp(text=CSV_TEXT)
        return _Resp({"Status": self.statuses.pop(0)})


def test_rows_to_places_dedupes_and_maps_fields() -> None:
    places = rows_to_places(parse_csv(CSV_TEXT))
    assert len(places) == 1
    place = places[0]
    assert place["google_place_id"] == "PID1" and place["city"] == "Henderson" and place["state"] == "NV"
    assert place["review_count"] == 651 and place["score"] == 4.8


def test_discover_creates_job_with_proxy_and_returns_places() -> None:
    session = _Session(["working", "ok"])
    client = GmapsClient("http://scraper:8081", proxies=["http://x:x@127.0.0.1:7890"], session=session)
    places = client.discover("t", ["med spa in Reno, NV"], poll=0, depth=2)
    url, body = session.posted[0]
    assert url == "http://scraper:8081/api/v1/jobs" and body["proxies"] == ["http://x:x@127.0.0.1:7890"]
    assert body["depth"] == 2 and session.trust_env is False
    assert [p["google_place_id"] for p in places] == ["PID1"]


def test_failed_job_raises() -> None:
    client = GmapsClient("http://scraper:8081", session=_Session(["failed"]))
    with pytest.raises(GmapsJobError):
        client.discover("t", ["k"], poll=0)
