from __future__ import annotations

from datetime import datetime, timezone

from utils.discovery_logic import classify_places

NOW = datetime(2026, 10, 15, tzinfo=timezone.utc)


def _place(pid, reviews, site="https://x.com", score=4.5, **extra):
    return {"google_place_id": pid, "name": pid, "review_count": reviews, "score": score,
            "website": site, **extra}


def test_new_passing_place_inserts_immediately() -> None:
    _, actions = classify_places([_place("p1", 80)], {}, {}, NOW)
    assert [a["google_place_id"] for a in actions["insert"]] == ["p1"]


def test_below_line_then_crossing_requires_two_passes() -> None:
    cands, actions = classify_places([_place("p2", 40)], {}, {}, NOW)
    assert not actions["insert"] and not actions["promote"]
    cands, actions = classify_places([_place("p2", 55)], {}, cands, NOW)
    assert not actions["promote"] and cands["p2"]["consecutive_passes"] == 1
    cands, actions = classify_places([_place("p2", 60)], {}, cands, NOW)
    assert [a["google_place_id"] for a in actions["promote"]] == ["p2"]


def test_flapping_resets_passes() -> None:
    cands, _ = classify_places([_place("p3", 40)], {}, {}, NOW)
    cands, _ = classify_places([_place("p3", 55)], {}, cands, NOW)
    cands, actions = classify_places([_place("p3", 45)], {}, cands, NOW)
    assert cands["p3"]["consecutive_passes"] == 0 and not actions["promote"]


def test_existing_business_refreshed_and_never_dropped() -> None:
    master = {"p4": {"business_id": 7, "review_count": 100, "score": 4.5}}
    _, actions = classify_places([_place("p4", 120)], master, {}, NOW)
    assert actions["refresh"] == [{"business_id": 7, "fields": {"review_count": 120}}]
    _, actions = classify_places([_place("p4", 10)], master, {}, NOW)
    assert actions["flag_below"] == [{"business_id": 7, "google_place_id": "p4"}]
    assert not actions["insert"] and not actions["promote"]


def _master(business_id, name, city, website, place_id=None):
    return {"business_id": business_id, "name": name, "city": city, "website": website, "google_place_id": place_id}


def test_unique_host_links_instead_of_inserting() -> None:
    masters = [_master(1, "Halcyon Mind+Body", "Superior", "halcyonmindbody.com")]
    place = _place("P1", 217, site="https://www.halcyonmindbody.com/", name="Halcyon Mind + Body", city="Superior")
    _, actions = classify_places([place], {}, {}, NOW, unlinked_masters=masters)
    assert not actions["insert"]
    assert actions["link"][0]["business_id"] == 1 and actions["link"][0]["matched_by"] == "host"
    assert actions["link"][0]["fields"] == {"review_count": 217, "score": 4.5}


def test_same_site_in_another_city_is_not_linked_to_that_branch() -> None:
    masters = [_master(765, "New Look Skin Center", "Irvine", "newlookskincenter.com")]
    place = _place("ENC", 120, site="https://newlookskincenter.com/", name="New Look Skin Center Encino", city="Encino")
    _, actions = classify_places([place], {}, {}, NOW, unlinked_masters=masters)
    assert not actions["link"] and not actions["insert"]
    assert [a["google_place_id"] for a in actions["ambiguous"]] == ["ENC"]


def test_shared_host_chain_is_ambiguous_not_inserted() -> None:
    masters = [_master(7, "Simplicity Laser", "Henderson", "https://simplicitylaser.com/")]
    places = [_place("A", 90, site="https://simplicitylaser.com/pages/a", city="Reno"),
              _place("B", 80, site="https://simplicitylaser.com/pages/b", city="Sparks")]
    _, actions = classify_places(places, {}, {}, NOW, unlinked_masters=masters)
    assert not actions["insert"] and not actions["link"]
    assert [a["google_place_id"] for a in actions["ambiguous"]] == ["A", "B"]


def test_name_and_city_match_when_host_differs() -> None:
    masters = [_master(3, "Youthbar", "Superior", "youthbar.com")]
    place = _place("P3", 120, site="https://youthbar.example/booking", name="YouthBar", city="Superior")
    _, actions = classify_places([place], {}, {}, NOW, unlinked_masters=masters)
    assert actions["link"][0]["business_id"] == 3 and actions["link"][0]["matched_by"] == "name_city"


def test_one_master_is_claimed_once() -> None:
    masters = [_master(1, "Spa", "Reno", "spa.com")]
    places = [_place("X", 90, site="https://spa.com", name="Spa", city="Reno"),
              _place("Y", 95, site="https://spa.com/other", name="Spa", city="Reno")]
    _, actions = classify_places(places, {}, {}, NOW, unlinked_masters=masters)
    assert len(actions["link"]) <= 1
