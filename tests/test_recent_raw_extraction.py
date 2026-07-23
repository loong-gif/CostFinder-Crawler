from utils.recent_raw_extraction import (
    build_promotion_content,
    deduplicate_templates,
    detect_multilocation_hosts,
    expand_promotion_content,
    extract_promotion_content_from_markdown,
    pricing_template_fingerprint,
    resolve_business,
    validate_membership,
    validate_promotion,
    validate_service,
)


BUSINESSES = [
    {
        "business_id": 1720,
        "name": "VIO Med Spa",
        "website": "viomedspa.com",
        "city": "Boulder",
        "address": "2100 28th St, Boulder, CO 80301",
    }
]


def test_rejects_vio_other_city_location() -> None:
    candidates = [
        {
            "url": "https://viomedspa.com/canton",
            "title": "VIO Med Spa Canton",
            "text": "$99 / month\nExplore our locations, including Boulder.",
        },
        {"url": "https://viomedspa.com/clifton", "title": "VIO Med Spa Clifton", "text": "$99 / month"},
        {"url": "https://viomedspa.com/dunwoody", "title": "VIO Med Spa Dunwoody", "text": "$99 / month"},
    ]
    hosts = detect_multilocation_hosts(candidates)
    decision = resolve_business(candidates[0], BUSINESSES, hosts)
    assert not decision.accepted
    assert decision.reason == "multilocation_without_target_identity"


def test_rejects_nested_vio_location_detected_end_to_end() -> None:
    candidates = [
        {
            "url": "https://viomedspa.com/locations/canton",
            "title": "VIO Med Spa Canton",
            "text": "$99 / month\nExplore our locations, including Boulder.",
        },
        {
            "url": "https://viomedspa.com/locations/clifton",
            "title": "VIO Med Spa Clifton",
            "text": "$99 / month",
        },
        {
            "url": "https://viomedspa.com/locations/dunwoody",
            "title": "VIO Med Spa Dunwoody",
            "text": "$99 / month",
        },
    ]

    hosts = detect_multilocation_hosts(candidates)
    decision = resolve_business(candidates[0], BUSINESSES, hosts)

    assert hosts == {"viomedspa.com"}
    assert not decision.accepted
    assert decision.reason == "multilocation_without_target_identity"


def test_accepts_target_city_location() -> None:
    source = {
        "url": "https://viomedspa.com/boulder",
        "title": "VIO Med Spa Boulder",
        "text": "2100 28th St, Boulder, CO 80301",
    }
    decision = resolve_business(source, BUSINESSES, {"viomedspa.com"})
    assert decision.accepted
    assert decision.business_id == 1720


def test_accepts_target_city_in_nested_location_path() -> None:
    source = {
        "url": "https://viomedspa.com/locations/boulder",
        "title": "VIO Med Spa Boulder",
        "text": "2100 28th St, Boulder, CO 80301",
    }

    decision = resolve_business(source, BUSINESSES, {"viomedspa.com"})

    assert decision.accepted
    assert decision.business_id == 1720


def test_accepts_generic_membership_page_with_exact_address() -> None:
    source = {
        "url": "https://viomedspa.com/membership",
        "title": "VIO Med Spa Membership",
        "text": "$99/month\n2100 28th St, Boulder, CO 80301",
    }

    decision = resolve_business(source, BUSINESSES, {"viomedspa.com"})

    assert decision.accepted
    assert decision.business_id == 1720


def test_rejects_ambiguous_shared_platform() -> None:
    businesses = [
        {
            "business_id": 1,
            "website": "facebook.com",
            "name": "A Clinic",
            "city": "A",
            "address": "1 Main St, A, CO 80000",
        },
    ]
    decision = resolve_business(
        {
            "url": "https://facebook.com/groups/example",
            "title": "Save today",
            "text": "$10/unit available",
        },
        businesses,
        set(),
    )
    assert not decision.accepted
    assert decision.reason == "ambiguous_host"


def test_shared_platform_rejects_city_without_business_name() -> None:
    businesses = [
        {
            "business_id": 1,
            "website": "facebook.com",
            "name": "A Clinic",
            "city": "Denver",
            "address": "1 Main St, Denver, CO",
        }
    ]

    decision = resolve_business(
        {
            "url": "https://facebook.com/groups/example",
            "title": "Denver Botox prices",
            "text": "Denver Botox is $10/unit",
        },
        businesses,
        set(),
    )

    assert not decision.accepted
    assert decision.reason == "ambiguous_host"


def test_shared_platform_accepts_normalized_business_name_and_city() -> None:
    businesses = [
        {
            "business_id": 1,
            "website": "facebook.com",
            "name": "A Clinic & Med Spa",
            "city": "Denver",
            "address": "1 Main St, Denver, CO",
        }
    ]

    decision = resolve_business(
        {
            "url": "https://facebook.com/groups/example",
            "title": "A Clinic and Med Spa — Denver",
            "text": "A Clinic & Med Spa in Denver offers Botox.",
        },
        businesses,
        set(),
    )

    assert decision.accepted
    assert decision.business_id == 1


def test_detects_multilocation_host_from_scheme_less_urls() -> None:
    candidates = [
        {"url": "viomedspa.com/canton"},
        {"url": "viomedspa.com/clifton"},
        {"url": "viomedspa.com/dunwoody"},
    ]

    assert detect_multilocation_hosts(candidates) == {"viomedspa.com"}


def test_validate_service_requires_name_and_price() -> None:
    assert validate_service(
        {"service_name": "Botox", "regular_price": 15},
        "",
    ).accepted
    assert not validate_service({"service_name": "Botox"}, "").accepted
    assert not validate_service({"regular_price": 15}, "").accepted


def test_validate_service_rejects_market_average_without_per_unit() -> None:
    result = validate_service(
        {"service_name": "Botox", "regular_price": 20, "unit_type": "unit"},
        "Botox typically ranges from $10 to $20 per unit in Orange County.",
        source_url="https://example.com/services",
    )
    assert not result.accepted
    assert result.reason == "market_average_not_clinic_price"


def test_validate_service_rejects_blog_source_url() -> None:
    result = validate_service(
        {"service_name": "Botox", "regular_price": 10, "unit_type": "unit"},
        "Glow Up offers Botox for $10/unit.",
        source_url="https://glowupmedspa.com/blogs/news/botox-specials",
    )
    assert not result.accepted
    assert result.reason == "ineligible_source_url"


def test_validate_membership_requires_name_and_price() -> None:
    item = {"membership_name": "Club", "membership_price": 99, "billing_period": None}
    assert validate_membership(item, "").accepted
    assert not validate_membership({"membership_name": "Club"}, "").accepted
    assert not validate_membership({"membership_price": 99}, "").accepted


def test_validate_membership_accepts_without_evidence() -> None:
    item = {
        "membership_name": "Club",
        "membership_price": 99,
        "billing_period": "monthly",
        "benefits": [],
    }
    assert validate_membership(
        item,
        "Club membership benefits, while Botox costs $99",
    ).accepted


def test_validate_promotion_requires_title_and_content() -> None:
    item = {
        "promotion_title": "Summer Botox",
        "promotion_content": ["$50 off Botox."],
        "campaign_start_date": None,
        "campaign_end_date": None,
    }
    assert validate_promotion(item, "").accepted
    assert not validate_promotion({**item, "promotion_title": ""}, "").accepted
    assert not validate_promotion({**item, "promotion_content": []}, "").accepted


def test_validate_promotion_rejects_ocr_garbage() -> None:
    item = {
        "promotion_title": "Special Offers",
        "promotion_content": ["BUY 4O UNITS", "&GETFREEA SKINBETTER", "20%0FF"],
    }
    assert not validate_promotion(item, "").accepted


def test_build_promotion_content_prefers_markdown_over_ocr_llm() -> None:
    evidence = (
        "## July Specials\n\n"
        "![Botox Special Zen Aesthetics and Wellness](https://example.com/botox.jpg)\n\n"
        "![$75 Off Juvederm Filler First Time User](https://example.com/juv.png)\n\n"
        "FAQ - Does Zen Aesthetics offer any new client offers for Botox? "
        "Yes, Zen Aesthetics offers Botox Cosmetic for $10/unit."
    )
    item = {
        "promotion_title": "Special Offers | Zen Aesthetics Medical Spa",
        "promotion_content": [
            "Botox Special",
            "BUY 4O UNITS",
            "&GETFREEA SKINBETTER",
            "$11/UNIT",
        ],
    }
    content = build_promotion_content(item, evidence)
    assert "BUY 4O UNITS" not in content
    assert any("Juvederm" in segment for segment in content)
    assert any("$10/unit" in segment for segment in content)


def test_extract_promotion_content_from_markdown_image_alts() -> None:
    evidence = "![SkinMedica TNS $50 Off Zen Aesthetics](https://x/y.png)"
    content = extract_promotion_content_from_markdown(evidence)
    assert content == ["SkinMedica TNS $50 Off Zen Aesthetics"]


def test_expand_promotion_content_adds_section_lines() -> None:
    evidence = (
        "## Tox\n\n- New Patient Tox 20% OFF\n\n- Botox $13/unit\n\n"
        "- Dysport $5.20/unit\n\n## Filler\n\n- 1 Syringe $675"
    )
    item = {
        "promotion_title": "New Patient Tox 20% OFF",
        "promotion_content": ["New Patient Tox 20% OFF"],
    }
    expanded = expand_promotion_content(item, evidence)
    assert len(expanded) >= 3
    assert "Botox $13/unit" in expanded


def test_deduplicates_same_domain_pricing_template() -> None:
    candidates = [
        {"url": "https://example.com/a", "text": "$99 / month\n$3 off each unit\n15% off services"},
        {"url": "https://example.com/b", "text": "$99/month\n$3 OFF each unit\n15% off services"},
    ]
    kept, rejected = deduplicate_templates(candidates)
    assert len(kept) == 1
    assert rejected[0]["reason"] == "duplicate_template"
    assert rejected[0]["kept_url"] == "https://example.com/a"
    assert rejected[0]["template_fingerprint"] == pricing_template_fingerprint(
        candidates[0]["text"]
    )
    assert pricing_template_fingerprint(candidates[0]["text"]) == pricing_template_fingerprint(
        candidates[1]["text"]
    )
