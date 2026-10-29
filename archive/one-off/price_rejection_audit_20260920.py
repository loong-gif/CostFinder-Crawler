import csv
import json
import re
from collections import Counter

V3 = "/tmp/costfinder_clinic_services_insert_dryrun_20260920_v3.json"
V4 = "/tmp/costfinder_clinic_services_insert_dryrun_20260920_price_v4.json"
STATE = "/tmp/costfinder_price_attribution_20260920_state.json"
OUT = "/tmp/costfinder_price_rejection_audit_20260920.json"
CSV_OUT = "/tmp/costfinder_price_rejection_audit_20260920.csv"


def compact(value):
    return re.sub(r"\s+", " ", (value or "").lower()).strip()


def number(value):
    return round(float(value), 2)


def distinct(values):
    result = []
    for value in values:
        value = number(value)
        if value not in result:
            result.append(value)
    return result


def amount_windows(text, amount, radius=90):
    text = text or ""
    value = f"{amount:g}"
    patterns = [
        rf"\$\s*{re.escape(value)}(?:\.0+)?(?!\d)",
        rf"(?<!\d){re.escape(value)}(?:\.0+)?(?!\d)",
    ]
    windows = []
    for pattern in patterns:
        for match in re.finditer(pattern, text, flags=re.I):
            windows.append(text[max(0, match.start() - radius):min(len(text), match.end() + radius)])
    return windows or [text]


def labels_for(text, amount):
    text = (text or "").lower()
    raw_value = f"{amount:g}"
    formatted_value = f"{amount:,.2f}".rstrip("0").rstrip(".")
    values = {raw_value, formatted_value}
    value_pattern = "|".join(
        re.escape(value) + (r"(?:0+)?" if "." in value else r"(?:\.0+)?")
        for value in values
    )
    patterns = [rf"\$\s*(?:{value_pattern})(?!\d|\.\d)", rf"(?<!\d)(?:{value_pattern})(?!\d|\.\d)"]
    labels = set()
    long_context = " ".join(amount_windows(text, amount, radius=120)).lower()
    for pattern in patterns:
        for match in re.finditer(pattern, text, flags=re.I):
            before = text[max(0, match.start() - 60):match.start()]
            after = text[match.end():min(len(text), match.end() + 70)]
            local = before + " " + after
            role_local = before[-35:] + " " + after[:35]
            if re.search(r"\b(regular|full\s+price|base\s+(?:deal\s+)?regular|list\s+price|normally|single\s+(?:session|treatment))\b", before) or re.search(r"\bstandard\b|\bregular\b", after[:35]):
                labels.add("regular")
            if re.search(r"\b(member|membership|loyalty|rewards?|black\s+card|gold\s+and\s+platinum)\b", role_local):
                labels.add("membership")
            if re.search(r"\b(sale|special|promo|promotion|discount|save|limited\s+time|new\s+patients?|new\s+clients?|first[- ]time|now\s+only|offer|pre[- ]?book)\b", role_local):
                labels.add("promotion")
            if re.search(r"\b(package|packages|bundle|buy\s+\d|\d+\s*[- ]?session(?:s)?\s*(?:package|deal)|save\s+\$)\b", role_local):
                labels.add("package")
            if re.search(r"\b(start(?:ing)?\s+at|from|begin(?:s|ning)?\s+at|as\s+low\s+as|up\s+to|at\s+least|and\s+up)\b", role_local) or re.search(r"\+\s*$", after[:8]):
                labels.add("from_price")
            if re.search(r"\b(range|price\s+range|between|average|var(?:y|ies)|depending|depends|varies\s+widely|through)\b", role_local) or re.search(r"[-–—]\s*\$?\s*\d|\$\s*\d[\d,]*(?:\.\d+)?\s+(?:to|through)\s+\$?\s*\d", role_local):
                labels.add("price_range")
            if re.search(r"\b(to|through)\b", before[-55:]) and "$" in before[-55:]:
                labels.add("price_range")
            if re.search(r"\b(deposit(?:ed)?|consult(?:ation)?|shipping|free|monthly|per\s*/?\s*month|add[- ]?ons?|additional|extractions?|dues|enrollment|enrolment|cancellation\s+fee|late\s+fee)\b", role_local):
                labels.add("other_qualified")
            if re.search(r"\badditional\s+\$|\badd[- ]?on\b", long_context):
                labels.add("other_qualified")
            atomic_marker = re.search(r"\b(each|per\s+(?:unit|session|treatment|syringe|vial|shot)|one\s+(?:session|treatment))\b", before[-55:] + " " + after[:55])
            if atomic_marker:
                labels.discard("package")
                if "other_qualified" in labels and re.search(r"\b(book|schedule|complimentary|assessment)\b.*\bconsult", local):
                    labels.discard("other_qualified")
                if not labels & {"promotion", "membership", "from_price", "price_range", "other_qualified"}:
                    labels.add("regular")
    return labels


def unit_resolution(record, row):
    actual = record.get("price_unit")
    expected = (row.get("insert_unit_type") or "").lower()
    if actual:
        return "explicit" if actual.lower() == expected else "ambiguous"
    local = " ".join(amount_windows(record.get("evidence_text", ""), record["amount"], radius=45)).lower()
    explicit_words = {
        "unit": r"\bper\s+unit\b|\bunit\s*[:\-]?\s*\$",
        "session": r"\bper\s+session\b|\bper-session\b",
        "treatment": r"\bper\s+treatment\b|\bper-treatment\b",
        "syringe": r"\bper\s+syringe\b|\bsyringe\s*[:\-]?\s*\$",
        "vial": r"\bper\s+vial\b|\bvial\s*[:\-]?\s*\$",
        "shot": r"\bper\s+shot\b|\bshot\s*[:\-]?\s*\$",
    }
    for unit, pattern in explicit_words.items():
        if re.search(pattern, local):
            return "explicit" if unit == expected else "ambiguous"
    if re.search(r"\bcycle(?:s)?\b|\bper\s+area\b|\bper\s+month\b|/\s*month\b", local):
        return "ambiguous"
    if expected in {"unit", "session", "treatment", "syringe", "vial", "shot"}:
        return "service_default"
    return "ambiguous"


def service_terms(row):
    terms = []
    for value in (row.get("service_name"), row.get("matched_alias")):
        value = compact(value)
        if value and len(value) >= 4 and value not in terms:
            terms.append(value)
    variants = {
        "diamondglow": ["diamond glow"],
        "microneedling": ["micro needling"],
        "vi peel": ["vipeel"],
        "rf microneedling": ["rf micro needling"],
    }
    for value in variants.get(compact(row.get("service_name")), []):
        if value not in terms:
            terms.append(value)
    return terms


def attribution(row, record, url_rows):
    if record.get("price_attribution_confidence") == "high":
        return "strong"
    text = compact(record.get("evidence_text"))
    nearby = " ".join(amount_windows(record.get("evidence_text", ""), record["amount"], radius=120)).lower()
    if compact(row.get("service_name")) != "massage" and re.search(r"\bmassage\b|\bhotstones?\b", nearby):
        return "ambiguous"
    if compact(row.get("service_name")) != "skinvive" and "skinvive" in nearby and "sculptra" in compact(record.get("evidence_text")):
        return "ambiguous"
    if re.search(r"\brelated\s+products?\b|\badd\s+to\s+cart\b", nearby):
        return "ambiguous"
    terms = service_terms(row)
    hits = [(text.find(term), term) for term in terms if text.find(term) >= 0]
    if hits:
        competitors = []
        for other in url_rows:
            if other is row:
                continue
            for term in service_terms(other):
                pos = text.find(term)
                if pos >= 0:
                    competitors.append((pos, term))
        amount_positions = [m.start() for m in re.finditer(r"\$\s*\d", record.get("evidence_text", ""))]
        current_pos = hits[0][0]
        amount_pos = min(amount_positions, key=lambda p: abs(p - current_pos)) if amount_positions else len(text)
        current_distance = abs(amount_pos - current_pos)
        if not competitors and current_distance <= 60:
            return "strong"
        if not competitors:
            return "ambiguous"
        competitor_distance = min(abs(amount_pos - p) for p, _ in competitors)
        if current_distance <= competitor_distance and current_distance <= 120:
            return "strong"
        return "ambiguous"
    page = compact(row.get("page_title")) + " " + compact(row.get("evidence_url"))
    if any(term in page for term in terms):
        return "medium"
    return "weak"


def records_for(row, state):
    records = []
    seen = set()
    for record in state.get(row["evidence_url"], {}).get("results", []):
        signature = (number(record["amount"]), record.get("price_unit"), compact(record.get("evidence_text")))
        if signature not in seen:
            seen.add(signature)
            records.append(record)
    return records


def best(records):
    ranks = {"strong": 3, "medium": 2, "weak": 1, "ambiguous": 0}
    return sorted(records, key=lambda x: (x.get("unit_resolution") == "explicit", ranks.get(x.get("attribution"), 0), x.get("price_attribution_confidence") == "high", len(x.get("evidence_text", ""))), reverse=True)[0] if records else None


def classify(row, current, state, candidates):
    if current.get("result") == "ready_with_price":
        return {"audit_result": "existing_ready_with_price", "rejection_reason": None, "proposed_regular_price": current.get("regular_price"), "unit_resolution": "existing_v4_gate", "audit_confidence": "confirmed", "primary": None, "records": []}
    raw = records_for(row, state)
    if not raw:
        return {"audit_result": "still_ambiguous", "rejection_reason": "no_high_confidence_price", "proposed_regular_price": None, "unit_resolution": "ambiguous", "audit_confidence": "low", "primary": None, "records": []}
    url_rows = [other for other in candidates if other["evidence_url"] == row["evidence_url"]]
    records = []
    for source in raw:
        labels = labels_for(source.get("evidence_text", ""), source["amount"])
        if source.get("price_type") == "price_range":
            labels.add("price_range")
        if source.get("price_type") == "package_total":
            labels.add("package")
        for qualifier in source.get("price_qualifier") or []:
            qualifier = qualifier.lower()
            if qualifier in {"membership", "member", "loyalty", "reward"}:
                labels.add("membership")
            if qualifier in {"promotion", "special", "sale", "discount", "starting_at"}:
                labels.add("promotion")
            if qualifier == "package":
                labels.add("package")
            if qualifier == "range":
                labels.add("price_range")
            if qualifier in {"deposit", "consultation", "free", "monthly"}:
                labels.add("other_qualified")
        records.append({**source, "labels": sorted(labels), "unit_resolution": unit_resolution(source, row), "attribution": attribution(row, source, url_rows)})

    regular = [record for record in records if "regular" in record["labels"] and not set(record["labels"]) & {"membership", "promotion", "package", "price_range", "from_price", "other_qualified"}]
    unlabeled = [record for record in records if not record["labels"] and record.get("price_type") in {"regular_price", "package_total", "promo_or_qualified"}]
    regular_amounts = distinct([record["amount"] for record in regular])

    if len(regular_amounts) == 1:
        selected = best([record for record in regular if number(record["amount"]) == regular_amounts[0]])
        if selected and selected["attribution"] in {"strong", "medium"} and selected["unit_resolution"] in {"explicit", "service_default"}:
            return {"audit_result": "recoverable_regular_price", "rejection_reason": None, "proposed_regular_price": regular_amounts[0], "unit_resolution": selected["unit_resolution"], "audit_confidence": "high" if selected["attribution"] == "strong" else "medium", "primary": selected, "records": records}
        if selected and selected["unit_resolution"] == "ambiguous":
            return {"audit_result": "still_ambiguous", "rejection_reason": "unit_mismatch", "proposed_regular_price": None, "unit_resolution": "ambiguous", "audit_confidence": "medium", "primary": selected, "records": records}
    if len(regular_amounts) > 1:
        return {"audit_result": "still_ambiguous", "rejection_reason": "regular_and_sale_price_unresolved", "proposed_regular_price": None, "unit_resolution": "ambiguous", "audit_confidence": "medium", "primary": best(regular), "records": records}

    plain = [record for record in unlabeled if record["attribution"] in {"strong", "medium"} and record["unit_resolution"] in {"explicit", "service_default"}]
    plain_amounts = distinct([record["amount"] for record in plain])
    all_amounts = distinct([record["amount"] for record in records])
    other_unresolved = [
        record for record in records
        if number(record["amount"]) not in plain_amounts
        and not record["labels"]
        and record["attribution"] in {"strong", "medium"}
    ]
    if len(plain_amounts) == 1 and not other_unresolved:
        selected = best(plain)
        return {"audit_result": "recoverable_regular_price", "rejection_reason": None, "proposed_regular_price": plain_amounts[0], "unit_resolution": selected["unit_resolution"], "audit_confidence": "high" if selected["attribution"] == "strong" else "medium", "primary": selected, "records": records}

    labels = set().union(*(set(record["labels"]) for record in records))
    if len(all_amounts) == 1:
        if "other_qualified" in labels:
            reason = "qualified_price_other"
        elif "package" in labels:
            reason = "package_total"
        elif "membership" in labels and "promotion" not in labels:
            reason = "membership_only"
        elif "promotion" in labels:
            reason = "promotion_only"
        elif "price_range" in labels:
            reason = "price_range"
        elif "from_price" in labels:
            reason = "from_price"
        elif any(record["unit_resolution"] == "ambiguous" for record in records):
            reason = "unit_mismatch"
        elif all(record["attribution"] in {"weak", "ambiguous"} for record in records):
            reason = "service_attribution_ambiguous"
        else:
            reason = "plain_price_but_no_explicit_unit"
        outcome = "correctly_rejected" if reason in {"promotion_only", "package_total", "membership_only", "price_range", "from_price", "qualified_price_other"} else "still_ambiguous"
        return {"audit_result": outcome, "rejection_reason": reason, "proposed_regular_price": None, "unit_resolution": "ambiguous", "audit_confidence": "medium", "primary": best(records), "records": records}

    if "price_range" in labels:
        reason = "price_range"
    elif "from_price" in labels and "regular" not in labels:
        reason = "from_price"
    elif "package" in labels and "regular" not in labels:
        reason = "package_total"
    elif "membership" in labels and not ({"regular", "promotion"} & labels):
        reason = "membership_only"
    elif "promotion" in labels and "regular" not in labels:
        reason = "promotion_only"
    elif "other_qualified" in labels and "regular" not in labels:
        reason = "qualified_price_other"
    elif any(record["unit_resolution"] == "ambiguous" for record in records):
        reason = "unit_mismatch"
    elif any(record["attribution"] in {"weak", "ambiguous"} for record in records):
        reason = "service_attribution_ambiguous"
    elif {"promotion", "regular"} <= labels or {"membership", "regular"} <= labels:
        reason = "regular_and_sale_price_unresolved"
    else:
        reason = "multiple_prices_unresolved"
    outcome = "correctly_rejected" if reason in {"promotion_only", "package_total", "membership_only", "price_range", "from_price", "qualified_price_other"} else "still_ambiguous"
    return {"audit_result": outcome, "rejection_reason": reason, "proposed_regular_price": None, "unit_resolution": "ambiguous", "audit_confidence": "medium", "primary": best(records), "records": records}


def main():
    v3 = json.load(open(V3))
    v4 = json.load(open(V4))
    state = json.load(open(STATE))["urls"]
    rows3 = v3["rows"]
    rows4 = v4["rows"]
    make_key = lambda row: (str(row["business_id"]), row["service_name"], row["evidence_url"])
    current_by_key = {make_key(row): row for row in rows4}
    candidates = [row for row in rows3 if row["status"] == "ready" and row["price_signal"] in {"explicit_amount", "price_word_only"}]
    output_rows = []
    for row in candidates:
        audit = classify(row, current_by_key[make_key(row)], state, candidates)
        primary = audit["primary"] or {}
        current = current_by_key[make_key(row)]
        output_rows.append({
            "business_id": row["business_id"], "business_name": row["business_name"], "service_name": row["service_name"], "evidence_url": row["evidence_url"],
            "insert_unit_type": row["insert_unit_type"], "unit_type_source": row["unit_type_source"], "page_title": row["page_title"], "route_kind": row["route_kind"],
            "price_signal": row["price_signal"], "price_match_count": row["price_match_count"], "current_v4_result": current.get("result"), "current_v4_reason": current.get("result_reason"),
            "audit_result": audit["audit_result"], "rejection_reason": audit["rejection_reason"], "proposed_regular_price": audit["proposed_regular_price"],
            "unit_resolution": audit["unit_resolution"], "audit_confidence": audit["audit_confidence"], "price_candidate": primary.get("amount"), "price_type": primary.get("price_type"),
            "price_unit": primary.get("price_unit"), "price_attribution_type": primary.get("price_attribution_type"), "price_attribution_confidence": primary.get("price_attribution_confidence"),
            "price_qualifier": primary.get("price_qualifier"), "evidence_text": primary.get("evidence_text", ""), "raw_record_count": len(audit["records"]),
        })
    result_counts = Counter(row["audit_result"] for row in output_rows)
    reason_counts = Counter(row["rejection_reason"] for row in output_rows if row["rejection_reason"])
    for reason in ["promotion_only", "package_total", "membership_only", "multiple_prices_unresolved", "unit_mismatch", "service_attribution_ambiguous", "plain_price_but_no_explicit_unit", "dedicated_page_price_rejected", "regular_and_sale_price_unresolved", "price_range", "from_price"]:
        reason_counts.setdefault(reason, 0)
    report = {
        "generated_at": "2026-09-20T00:00:00+08:00",
        "source_artifacts": {"v3": V3, "v4": V4, "attribution_state": STATE},
        "scope": {"candidate_rows": len(output_rows), "price_urls": len(state), "db_writes": 0},
        "counts": {"existing_ready_with_price": result_counts["existing_ready_with_price"], "recoverable_regular_price": result_counts["recoverable_regular_price"], "correctly_rejected": result_counts["correctly_rejected"], "still_ambiguous": result_counts["still_ambiguous"]},
        "rejection_reason_counts": dict(sorted(reason_counts.items())),
        "notes": ["Audit only; no Supabase writes.", "service_default is allowed only for a live atomic unit of unit/session/treatment/syringe/vial/shot.", "Package totals are never divided.", "Regular-labelled amounts are resolved before promotion/member/package rejection."],
        "rows": output_rows,
    }
    with open(OUT, "w") as handle:
        json.dump(report, handle, ensure_ascii=False, indent=2)
    with open(CSV_OUT, "w", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=list(output_rows[0].keys()))
        writer.writeheader()
        writer.writerows(output_rows)
    print(json.dumps({"counts": report["counts"], "rejection_reason_counts": report["rejection_reason_counts"], "files": [OUT, CSV_OUT]}, ensure_ascii=False, indent=2))
    print("\nrecoverable examples:")
    for row in output_rows:
        if row["audit_result"] == "recoverable_regular_price":
            print(row["business_id"], row["service_name"], row["proposed_regular_price"], row["unit_resolution"], row["evidence_url"])


if __name__ == "__main__":
    main()
