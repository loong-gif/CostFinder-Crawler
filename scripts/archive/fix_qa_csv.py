import csv
from copy import deepcopy


INPUT_PATH = "/Users/wyl/Downloads/4.3 CF - qa.csv"
OUTPUT_PATH = "/Users/wyl/Downloads/4.3_CF_qa_fixed.csv"


def as_str(value):
    if value is None:
        return ""
    return str(value)


def make_row(base, **updates):
    row = deepcopy(base)
    row["qa"] = "checked"
    for key, value in updates.items():
        row[key] = as_str(value)
    return row


def split_brand_rows(base, service_defs, brand_prices, *,
                     template_type="FIXED_PRICE",
                     unit_type=None,
                     membership_name=None,
                     membership_price=None,
                     billing_period=None,
                     minimum_term=None,
                     is_membership_required=None,
                     discount_amount=None,
                     discount_percent=None):
    rows = []
    for service_name, service_area in service_defs:
        for brand, price in brand_prices:
            row = make_row(
                base,
                service_name=f"{service_name} {brand}",
                offer_content=f'{{"{brand}":1}}',
                template_type=template_type,
                original_price=price,
                discount_price="",
                membership_price=membership_price if membership_price is not None else "",
                billing_period=billing_period if billing_period is not None else "",
                membership_name=membership_name if membership_name is not None else "",
                minimum_term=minimum_term if minimum_term is not None else "",
                discount_amount=discount_amount if discount_amount is not None else "",
                discount_percent=discount_percent if discount_percent is not None else "",
                service_area=service_area,
            )
            if unit_type is not None:
                row["unit_type"] = unit_type
            if is_membership_required is not None:
                row["is_membership_required"] = is_membership_required
            rows.append(row)
    return rows


with open(INPUT_PATH, newline="", encoding="utf-8-sig") as f:
    reader = csv.DictReader(f)
    fieldnames = reader.fieldnames
    original_rows = list(reader)


deletes = {146, 147, 216, 359, 368, 460, 504, 637, 642, 654}
replacements = {}


def add(line_no, rows):
    replacements[line_no] = rows


for idx, row in enumerate(original_rows, start=2):
    qa = (row.get("qa") or "").strip()
    if qa == "checked":
        continue

    if idx == 2:
        rows = []
        for units, total in [(100, "8"), (60, "8.5"), (40, "9")]:
            rows.append(
                make_row(
                    row,
                    service_name=f"Tox Package {units} Units",
                    offer_content=f'{{"Tox Package":{units}}}',
                    original_price="",
                    discount_price=total,
                    min_unit=units,
                    unit_type="unit",
                    delivered_unit=units,
                )
            )
        add(idx, rows)
    elif idx == 3:
        add(idx, split_brand_rows(row, [("Masseter Facial Slimming", "jaw")], [("Botox", "685"), ("Dysport", "685"), ("Daxxify", "822")]))
    elif idx == 4:
        add(
            idx,
            [
                make_row(
                    row,
                    service_name="Filler Treatment",
                    offer_content='{"Filler Treatment":1}',
                    original_price="550+",
                    discount_price="500+",
                    unit_type="treatment",
                    delivered_unit="1",
                ),
                make_row(
                    row,
                    service_name="Biostimulator",
                    offer_content='{"Biostimulator":1}',
                    original_price="900+",
                    discount_price="850+",
                    unit_type="syringe",
                    delivered_unit="1",
                ),
            ],
        )
    elif idx == 5:
        add(idx, [make_row(row, original_price="389-799", discount_price="")])
    elif idx == 6:
        rows = []
        for areas, avg in [(2, "175"), (3, "166.67"), (4, "150"), (5, "135"), (6, "125")]:
            rows.append(
                make_row(
                    row,
                    service_name=f"Classic Areas Tox ({areas} Areas)",
                    offer_content=f'{{"Classic Areas Tox":{areas}}}',
                    discount_price=avg,
                    min_unit=areas,
                    unit_type="area",
                    delivered_unit=areas,
                )
            )
        add(idx, rows)
    elif idx == 7:
        add(idx, [make_row(row, original_price="389-799", discount_price="330.65-679.15", membership_price="330.65-679.15")])
    elif idx == 8:
        add(idx, [make_row(row, original_price="300-3000+", discount_price="")])
    elif idx == 9:
        add(idx, split_brand_rows(row, [("Crows Feet", "eye")], [("Botox", "275"), ("Dysport", "275"), ("Daxxify", "330")]))
    elif idx == 10:
        add(idx, split_brand_rows(row, [("Glabellar Line", "glabellar")], [("Botox", "250"), ("Dysport", "250"), ("Daxxify", "300")]))
    elif idx == 11:
        service_defs = [
            ("Brow Lift", "brow"),
            ("Bunny Lines", "nose"),
            ("Nasal Recontouring", "nose"),
            ("Gummy Smile", "mouth"),
            ("Lip Flip", "lip"),
            ("Chin Dimpling", "chin"),
        ]
        add(idx, split_brand_rows(row, service_defs, [("Botox", "175"), ("Dysport", "175"), ("Daxxify", "210")]))
    elif idx == 12:
        add(idx, split_brand_rows(row, [("Shoulder Slimming", "shoulder")], [("Botox", "1325"), ("Dysport", "1325"), ("Daxxify", "1590")], unit_type="treatment"))
    elif idx == 13:
        add(
            idx,
            [
                make_row(
                    row,
                    service_name="Botox",
                    offer_content='{"Botox":1}',
                    original_price="14",
                    discount_price="13",
                    membership_price="13",
                ),
                make_row(
                    row,
                    service_name="Jeuveau",
                    offer_content='{"Jeuveau":1}',
                    original_price="12",
                    discount_price="11",
                    membership_price="11",
                ),
            ],
        )
    elif idx == 14:
        add(idx, split_brand_rows(row, [("Hyperhidrosis Treatment", "underarm")], [("Botox", "1225"), ("Dysport", "1225"), ("Daxxify", "1470")], unit_type="treatment"))
    elif idx == 24:
        add(idx, [make_row(row, membership_price="1599")])
    elif idx == 30:
        add(
            idx,
            [
                make_row(
                    row,
                    original_price="1500+",
                    discount_price="",
                    min_unit="1",
                    unit_type="treatment",
                    delivered_unit="1",
                    offer_content='{"Sculptra Skin Tightening":1}',
                )
            ],
        )
    elif idx == 38:
        rows = []
        for syringes, avg in [(2, "625"), (3, "600"), (4, "575")]:
            rows.append(
                make_row(
                    row,
                    service_name=f"Filler Multiple Syringes ({syringes} Syringes)",
                    offer_content=f'{{"Filler":{syringes}}}',
                    discount_price=avg,
                    min_unit=syringes,
                    unit_type="syringe",
                    delivered_unit=syringes,
                )
            )
        add(idx, rows)
    elif idx == 49:
        add(
            idx,
            [
                make_row(
                    row,
                    service_name="Fillers (2 Syringes)",
                    is_membership_required="TRUE",
                    eligibility="1. Must be an active Gold Coast Aesthetics member",
                    original_price="1400",
                    discount_price="549",
                    membership_price="149",
                    billing_period="monthly",
                    membership_name="Gold Coast Aesthetics Membership",
                    discount_amount="302",
                )
            ],
        )
    elif idx == 50:
        add(
            idx,
            [
                make_row(row, service_name="Xeomin (2 Areas)", offer_content='{"Xeomin":2}', min_unit="2", delivered_unit="2", discount_amount="50"),
                make_row(row, service_name="Xeomin (3 Areas)", offer_content='{"Xeomin":3}', min_unit="3", delivered_unit="3", discount_amount="100"),
            ],
        )
    elif idx == 52:
        add(idx, [make_row(row, is_package="TRUE", original_price="1500", discount_price="700", delivered_unit="2")])
    elif idx == 53:
        add(idx, [make_row(row, is_package="TRUE", original_price="1500", discount_price="700", delivered_unit="2")])
    elif idx == 69:
        add(
            idx,
            [
                make_row(
                    row,
                    service_name="Exosome Injections 3 Pack",
                    template_type="BUNDLE",
                    is_package="TRUE",
                    original_price="1497",
                    discount_price="433",
                    min_unit="3",
                    unit_type="session",
                    delivered_unit="3",
                )
            ],
        )
    elif idx == 71:
        add(
            idx,
            [
                make_row(
                    row,
                    service_name="PRP Injections 3 Pack",
                    template_type="BUNDLE",
                    is_package="TRUE",
                    is_membership_required="FALSE",
                    eligibility="Open to all",
                    original_price="1287",
                    discount_price="399.67",
                    min_unit="3",
                    unit_type="syringe",
                    delivered_unit="3",
                    membership_price="",
                    billing_period="",
                    membership_name="",
                )
            ],
        )
    elif idx == 73:
        add(idx, [make_row(row, is_package="TRUE", discount_price="575", unit_type="syringe", min_unit="4", delivered_unit="4")])
    elif idx == 75:
        add(idx, [make_row(row, service_name="Signature Combinations", discount_price="650", unit_type="session", min_unit="6", delivered_unit="6")])
    elif idx == 83:
        add(
            idx,
            [
                make_row(row, service_name="Masseter Botox Jawline Slimming", offer_content='{"Botox":50}', original_price="550", discount_price="", min_unit="50", unit_type="unit", delivered_unit="50"),
                make_row(row, service_name="Masseter Dysport Jawline Slimming", offer_content='{"Dysport":150}', original_price="550", discount_price="", min_unit="150", unit_type="unit", delivered_unit="150"),
            ],
        )
    elif idx == 114:
        add(idx, [make_row(row, original_price="175+", discount_price="", unit_type="treatment", delivered_unit="1")])
    elif idx == 116:
        add(idx, [make_row(row, discount_price="", discount_amount="40")])
    elif idx == 121:
        add(idx, [make_row(row, original_price="", discount_price="", discount_percent="10")])
    elif idx == 129:
        add(
            idx,
            [
                make_row(row, service_name="Neurotoxin Units (40+ Units)", offer_content='{"Neurotoxin":40}', original_price="13", discount_price="10", min_unit="40", delivered_unit="40"),
                make_row(row, service_name="Neurotoxin Units (90+ Units)", offer_content='{"Neurotoxin":90}', original_price="13", discount_price="9", min_unit="90", delivered_unit="90"),
            ],
        )
    elif idx == 131:
        add(idx, [make_row(row, discount_price="8.99", min_unit="", delivered_unit="", offer_content='{"wrinkle relaxer injectables":"up to 40 units"}')])
    elif idx == 132:
        add(idx, [make_row(row, discount_price="10.2")])
    elif idx == 151:
        add(idx, [make_row(row, original_price="", discount_price="")])
    elif idx == 154:
        add(idx, [make_row(row, service_name="Custom Full Facial Balancing Package", template_type="BUNDLE", is_package="TRUE", original_price="", discount_price="")])
    elif idx == 171:
        add(idx, [make_row(row, service_name="Neurotoxins Spend Discount", original_price="2500-5000", min_unit="2500", delivered_unit="", unit_type="dollar spend")])
    elif idx == 188:
        add(idx, split_brand_rows(row, [("Shoulder Slimming", "shoulder")], [("Botox", "1325"), ("Dysport", "1325")], unit_type="treatment"))
    elif idx == 189:
        add(idx, split_brand_rows(row, [("Bruxism", "jaw")], [("Botox", "1300"), ("Dysport", "1300")], unit_type="treatment"))
    elif idx == 234:
        add(idx, [make_row(row)])
    elif idx == 250:
        add(idx, split_brand_rows(row, [("Bruxism", "jaw")], [("Botox", "1300"), ("Dysport", "1300"), ("Daxxify", "840")], unit_type="treatment"))
    elif idx == 280:
        add(idx, [make_row(row, original_price="389-799", discount_price="")])
    elif idx == 301:
        add(idx, [make_row(row, original_price="750-2800", discount_price="")])
    elif idx == 302:
        add(idx, [make_row(row, original_price="750-1500", discount_price="")])
    elif idx == 314:
        add(idx, split_brand_rows(row, [("Masseter Slimming", "jaw")], [("Botox", "685"), ("Dysport", "685")], unit_type="treatment"))
    elif idx == 348:
        rows = []
        for name in ["Restylane Defyne", "Restylane Refyne", "Restylane Lyft", "Restylane Silk"]:
            rows.append(make_row(row, service_name=name, offer_content=f'{{"{name}":1}}', original_price="600", unit_type="syringe", delivered_unit="1"))
        add(idx, rows)
    elif idx == 354:
        add(
            idx,
            [
                make_row(row, service_name="Restylane Portfolio (Half Syringe)", original_price="599", unit_type="half syringe", delivered_unit="0.5"),
                make_row(row, service_name="Restylane Portfolio (Full Syringe)", original_price="879", unit_type="syringe", delivered_unit="1"),
            ],
        )
    elif idx == 355:
        add(
            idx,
            [
                make_row(row, service_name="Juvederm Portfolio (Half Syringe)", original_price="599", unit_type="half syringe", delivered_unit="0.5"),
                make_row(row, service_name="Juvederm Portfolio (Full Syringe)", original_price="879", unit_type="syringe", delivered_unit="1"),
            ],
        )
    elif idx == 369:
        add(idx, [make_row(row, original_price="500-1200", discount_price="")])
    elif idx == 370:
        add(
            idx,
            [
                make_row(row, service_name="Filler", offer_content='{"Filler":1}', original_price="550+", discount_price="500+", unit_type="treatment"),
                make_row(row, service_name="Biostimulator", offer_content='{"Biostimulator":1}', original_price="900+", discount_price="850+", unit_type="syringe"),
            ],
        )
    elif idx == 385:
        add(
            idx,
            [
                make_row(row, service_name="Filler", offer_content='{"Filler":1}', original_price="550+", discount_price="", discount_amount="75", unit_type="treatment"),
                make_row(row, service_name="Biostimulator", offer_content='{"Biostimulator":1}', original_price="900+", discount_price="", discount_amount="75", unit_type="syringe"),
            ],
        )
    elif idx == 392:
        add(idx, split_brand_rows(row, [("Forehead Lines", "forehead")], [("Botox", "430"), ("Dysport", "430"), ("Daxxify", "430")]))
    elif idx == 402:
        add(idx, [make_row(row, discount_price="360", delivered_unit="45")])
    elif idx == 414:
        add(idx, [make_row(row, original_price="300-500", discount_price="")])
    elif idx == 427:
        add(idx, [make_row(row, original_price="250-600", discount_price="")])
    elif idx == 440:
        add(idx, [make_row(row, original_price="200-300", discount_price="")])
    elif idx == 451:
        add(idx, [make_row(row, original_price="", discount_price="")])
    elif idx == 458:
        add(idx, [make_row(row, original_price="125-500", discount_price="")])
    elif idx == 466:
        add(idx, [make_row(row, original_price="100-150", discount_price="")])
    elif idx == 486:
        add(
            idx,
            [
                make_row(
                    row,
                    service_category="Fillers & Other Injectables",
                    service_name="Botox + JUVÉDERM Filler",
                    offer_content='{"Botox treatment":1,"JUVÉDERM filler":1,"Double Allē Points":1}',
                    membership_name="Allē",
                )
            ],
        )
    elif idx == 494:
        add(idx, [make_row(row, membership_name="Allergan Rewards Program", discount_amount="20")])
    elif idx == 515:
        add(
            idx,
            [
                make_row(row, service_name="Botox Per Unit", offer_content='{"Botox":1}', original_price="13"),
                make_row(row, service_name="Dysport Per Unit", offer_content='{"Dysport":1}', original_price="4.33"),
            ],
        )
    elif idx == 521:
        add(
            idx,
            [
                make_row(row, service_name="Botox", offer_content='{"Botox":1}', original_price="13"),
                make_row(row, service_name="Dysport", offer_content='{"Dysport":1}', original_price="13"),
            ],
        )
    elif idx == 552:
        add(idx, [make_row(row, membership_price="11.2", discount_price="11.2")])
    elif idx == 559:
        add(
            idx,
            [
                make_row(row, service_name="Botox touch-up refinement", offer_content='{"Botox":1}', original_price="11"),
                make_row(row, service_name="Dysport touch-up refinement", offer_content='{"Dysport":1}', original_price="11"),
            ],
        )
    elif idx == 576:
        add(idx, [make_row(row, original_price="10-20", discount_price="")])
    elif idx == 582:
        add(
            idx,
            [
                make_row(row, service_name="Botox 150 Units", is_package="TRUE", offer_content='{"Botox":150}', original_price="12.5", discount_price="10", min_unit="150", delivered_unit="150", membership_name="150 Unit Offer", membership_price="", billing_period="", discount_amount="375", discount_percent=""),
                make_row(row, service_name="Botox", offer_content='{"Botox":1}', original_price="12.5", discount_price="10.5", membership_name="Gold Membership", membership_price="149", billing_period="monthly"),
                make_row(row, service_name="Botox", offer_content='{"Botox":1}', original_price="12.5", discount_price="11", membership_name="Active Membership", membership_price="", billing_period="monthly"),
                make_row(row, service_name="Botox", offer_content='{"Botox":1}', original_price="12.5", discount_price="11.5", membership_name="Botox Membership", membership_price="40", billing_period="monthly", discount_percent="10"),
            ],
        )
    elif idx == 583:
        add(
            idx,
            [
                make_row(row, service_name="Dysport", offer_content='{"Dysport":1}', discount_price="10", membership_name="Silver Membership", billing_period="monthly"),
                make_row(row, service_name="Dysport", offer_content='{"Dysport":1}', discount_price="10.5", membership_name="Gold Membership", membership_price="149", billing_period="monthly"),
            ],
        )
    elif idx == 604:
        add(idx, [make_row(row, discount_percent="88")])
    elif idx == 618:
        add(idx, [make_row(row, original_price="4.75+", discount_price="")])
    elif idx == 629:
        add(idx, [make_row(row, is_membership_required="TRUE", eligibility="1. Must be a member, 2. 60 unit minimum", membership_name="Member", membership_price="3.72", discount_price="3.72")])
    elif idx == 630:
        add(
            idx,
            [
                make_row(
                    row,
                    original_price="",
                    discount_price="550",
                    min_unit="1",
                    unit_type="package",
                    service_area="lip/face",
                    delivered_unit="1",
                    membership_price="",
                    membership_name="",
                )
            ],
        )
    elif idx == 650:
        add(idx, [make_row(row, original_price="575")])
    elif idx == 651:
        add(idx, [make_row(row, original_price="1400")])
    elif idx == 652:
        add(idx, [make_row(row, original_price="1200", offer_raw_text="Under Arm Botox For Sweating")])
    elif idx == 653:
        add(idx, [make_row(row, original_price="299")])
    elif idx == 656:
        add(idx, [make_row(row, original_price="799")])
    elif idx == 680:
        add(
            idx,
            [
                make_row(
                    row,
                    service_name="Collagen Restore Package",
                    offer_content='{"Morpheus8 Face Package":3,"Sculptra":4}',
                    discount_price="5000",
                    min_unit="1",
                    unit_type="package",
                    delivered_unit="1",
                ),
                make_row(
                    row,
                    service_name="Collagen Restore Package with SkinVive",
                    offer_content='{"Morpheus8 Face Package":3,"Sculptra":4,"SkinVive":1}',
                    discount_price="5500",
                    min_unit="1",
                    unit_type="package",
                    delivered_unit="1",
                ),
            ],
        )
    elif idx == 682:
        add(
            idx,
            [
                make_row(row, service_name="Dermal Filler", offer_content='{"Dermal Filler":1}', discount_percent="5"),
                make_row(row, service_name="PRF Treatments", offer_content='{"PRF Treatments":1}', discount_percent="5"),
                make_row(row, service_name="Sculptra Treatments", offer_content='{"Sculptra Treatments":1}', discount_percent="5"),
            ],
        )
    elif idx == 686:
        add(
            idx,
            [
                make_row(row, service_name="Botox", offer_raw_text="50% off Botox (up to 40 units)", offer_content='{"Botox":1}', discount_percent="50", min_unit="1", unit_type="unit", delivered_unit="1"),
                make_row(row, service_category="Fillers & Other Injectables", service_name="Weekly B-12 Vitamin Injection", offer_raw_text="Free Weekly B-12 Vitamin Injection", offer_content='{"Weekly B-12 Vitamin Injection":1}', discount_percent="", unit_type="injection"),
                make_row(row, service_category="Fillers & Other Injectables", service_name="Anniversary Treatment Credit", offer_raw_text="Anniversary Perk $219 towards a treatment of your choice.", offer_content='{"Anniversary Treatment Credit":1}', discount_percent="", discount_amount="219", unit_type="treatment"),
            ],
        )
    elif idx == 708:
        add(
            idx,
            [
                make_row(row, service_name="Any 1 treatment up to $1,000 value", offer_content='{"Any treatment up to $1,000 value":1}', unit_type="treatment", delivered_unit="1"),
                make_row(row, service_name="Botox 50 unit bank", offer_content='{"Botox":50}', min_unit="50", unit_type="unit", delivered_unit="50"),
            ],
        )
    elif idx == 714:
        add(
            idx,
            [
                make_row(row, source_name="Longevity", service_name="Botox", offer_raw_text="$10.50 Botox Unit", offer_content='{"Botox":1}', membership_name="Gold", membership_price="149", billing_period="monthly", discount_price="10.5", unit_type="unit", service_area="face", delivered_unit="1"),
                make_row(row, source_name="Longevity", service_name="Dysport", offer_raw_text="$10.50 Dysport Unit", offer_content='{"Dysport":1}', membership_name="Gold", membership_price="149", billing_period="monthly", discount_price="10.5", unit_type="unit", service_area="face", delivered_unit="1"),
            ],
        )
    elif idx == 715:
        add(
            idx,
            [
                make_row(row, source_name="Norman Medspa", service_name="Botox", offer_raw_text="$9/unit Botox", offer_content='{"Botox":1}', discount_price="9", membership_name="Platinum Skin", membership_price="199", billing_period="monthly", unit_type="unit", service_area="face", delivered_unit="1"),
                make_row(row, source_name="Norman Medspa", service_name="Dysport", offer_raw_text="$9/unit Dysport", offer_content='{"Dysport":1}', discount_price="9", membership_name="Platinum Skin", membership_price="199", billing_period="monthly", unit_type="unit", service_area="face", delivered_unit="1"),
                make_row(row, source_name="Norman Medspa", service_name="Jeuveau", offer_raw_text="$9/unit Jeuveau", offer_content='{"Jeuveau":1}', discount_price="9", membership_name="Platinum Skin", membership_price="199", billing_period="monthly", unit_type="unit", service_area="face", delivered_unit="1"),
                make_row(row, source_name="Norman Medspa", service_name="Daxxify", offer_raw_text="$9/unit Daxxify", offer_content='{"Daxxify":1}', discount_price="9", membership_name="Platinum Skin", membership_price="199", billing_period="monthly", unit_type="unit", service_area="face", delivered_unit="1"),
                make_row(row, source_name="Norman Medspa", service_category="Fillers & Other Injectables", service_name="Filler Discount", offer_raw_text="15% off filler", offer_content='{"Filler":1}', discount_percent="15", membership_name="Platinum Skin", membership_price="199", billing_period="monthly", unit_type="treatment", service_area="face", delivered_unit="1"),
            ],
        )
    else:
        add(idx, [make_row(row)])


output_rows = []
handled_nonchecked = set()

for idx, row in enumerate(original_rows, start=2):
    if not any((row.get(key) or "").strip() for key in ("source_url", "source_name", "service_name", "offer_raw_text")):
        continue
    qa = (row.get("qa") or "").strip()
    if qa == "checked":
        output_rows.append(row)
        continue

    handled_nonchecked.add(idx)
    if idx in deletes:
        continue
    if idx in replacements:
        output_rows.extend(replacements[idx])
    else:
        raise RuntimeError(f"Unhandled row {idx}")


expected_nonchecked = {
    idx for idx, row in enumerate(original_rows, start=2)
    if (row.get("qa") or "").strip() != "checked"
}
assert handled_nonchecked == expected_nonchecked


def split_membership_services(row, services, *, price=None, original_price=None, offer_contents=None):
    rows = []
    for idx, service in enumerate(services):
        content = ""
        if offer_contents:
            content = offer_contents[idx]
        else:
            content = f'{{"{service}":1}}'
        rows.append(
            make_row(
                row,
                service_name=service,
                offer_content=content,
                discount_price=price if price is not None else row.get("discount_price", ""),
                original_price=original_price if original_price is not None else row.get("original_price", ""),
            )
        )
    return rows


normalized_rows = []
for row in output_rows:
    source_name = row.get("source_name", "")
    service_name = row.get("service_name", "")
    membership_name = row.get("membership_name", "")
    offer_raw_text = row.get("offer_raw_text", "")
    source_url = row.get("source_url", "")

    if source_name == "Luxe Room Cosmetic" and service_name == "Botox" and offer_raw_text == "12% off Botox ($12.32/unit)":
        continue
    if source_name == "Luxe Room Cosmetic" and service_name == "Dysport" and offer_raw_text == "12% off Dysport ($3.96/unit)":
        continue
    if source_url == "https://h-md.com/about/memberships/" and service_name in {"Botox", "Dysport", "Xeomin"} and "$9 PER UNIT" in offer_raw_text:
        continue

    if source_name == "Luxe Room Cosmetic" and service_name == "Botox / Dysport" and membership_name == "The Luxe Club":
        normalized_rows.extend(
            [
                make_row(row, service_name="Botox", offer_content='{"Botox":1}', discount_price="12.32"),
                make_row(row, service_name="Dysport", offer_content='{"Dysport":1}', discount_price="3.96"),
            ]
        )
        continue

    if source_name == "Luxe Room Cosmetic" and service_name == "Botox / Dysport" and membership_name == "TLC Black Card":
        normalized_rows.extend(
            [
                make_row(row, service_name="Botox", offer_content='{"Botox":1}', discount_price="11.20"),
                make_row(row, service_name="Dysport", offer_content='{"Dysport":1}', discount_price="3.60"),
            ]
        )
        continue

    if source_name == "The Pur Health" and service_name in {"Botox Membership", "The Pur Botox Club"} and "15 Botox Units" in offer_raw_text:
        normalized_rows.extend(
            [
                make_row(row, service_name="Botox", offer_content='{"Botox":15}', min_unit="15", delivered_unit="15", unit_type="unit", is_package="FALSE", discount_price=""),
                make_row(row, service_name="Dysport", offer_content='{"Dysport":20}', min_unit="20", delivered_unit="20", unit_type="unit", is_package="FALSE", discount_price=""),
                make_row(row, service_name="Xeomin", offer_content='{"Xeomin":49}', min_unit="49", delivered_unit="49", unit_type="unit", is_package="FALSE", discount_price=""),
            ]
        )
        continue

    if source_name == "L3 Aesthetics" and service_name == "L3 Neuromodulators Membership":
        normalized_rows.append(make_row(row, service_name="Neurotoxin", offer_content='{"Neurotoxin":1}'))
        continue

    if source_name == "The Pur Health" and service_name == "The Pur Filler Club":
        normalized_rows.append(make_row(row, service_name="Dermal Filler", offer_content='{"Dermal Filler":1}'))
        continue

    if source_name == "Tru Balanced Aesthetics" and service_name == "Botox / Dysport" and membership_name == "Tru Signature":
        normalized_rows.extend(
            [
                make_row(row, service_name="Botox", offer_content='{"Botox":1}', original_price="13.5", discount_price="11.5"),
                make_row(row, service_name="Dysport", offer_content='{"Dysport":1}', original_price="13.5", discount_price="11.5"),
            ]
        )
        continue

    if source_name == "Wheeler Med Spa" and service_name == "Dysport or Botox":
        normalized_rows.extend(
            [
                make_row(row, service_name="Botox", offer_content='{"Botox":1}', original_price="13", discount_price="11.5"),
                make_row(row, service_name="Dysport", offer_content='{"Dysport":1}', original_price="13", discount_price="11.5"),
            ]
        )
        continue

    if source_name == "Wheeler Med Spa" and service_name == "Jeuveau, Letybo, or Xeomin":
        normalized_rows.extend(
            [
                make_row(row, service_name="Jeuveau", offer_content='{"Jeuveau":1}', original_price="12", discount_price="10.5"),
                make_row(row, service_name="Letybo", offer_content='{"Letybo":1}', original_price="12", discount_price="10.5"),
                make_row(row, service_name="Xeomin", offer_content='{"Xeomin":1}', original_price="12", discount_price="10.5"),
            ]
        )
        continue

    if source_name == "Luxe Room Cosmetic" and service_name == "Botox / Dysport" and membership_name == "Member":
        normalized_rows.extend(
            [
                make_row(row, service_name="Botox", offer_content='{"Botox":1}', discount_price="11.2"),
                make_row(row, service_name="Dysport", offer_content='{"Dysport":1}', discount_price="11.2"),
            ]
        )
        continue

    if source_name == "Vasu Skin Solutions" and service_name in {"Botox and Jeuveau", "Botox + Jeuveau"}:
        normalized_rows.extend(
            [
                make_row(row, service_name="Botox", offer_content='{"Botox":1}', discount_price="11", discount_percent=""),
                make_row(row, service_name="Jeuveau", offer_content='{"Jeuveau":1}', discount_price="11", discount_percent=""),
            ]
        )
        continue

    if source_name == "Norman Medspa" and service_name == "Gold Skin Membership Tox":
        normalized_rows.extend(
            [
                make_row(row, service_name="Botox", offer_content='{"Botox":1}', discount_price="10"),
                make_row(row, service_name="Dysport", offer_content='{"Dysport":1}', discount_price="10"),
                make_row(row, service_name="Jeuveau", offer_content='{"Jeuveau":1}', discount_price="10"),
                make_row(row, service_name="Daxxify", offer_content='{"Daxxify":1}', discount_price="10"),
            ]
        )
        continue

    if source_name == "Skinjectables" and service_name == "VIP Pricing Tox":
        normalized_rows.append(make_row(row, service_name="Neurotoxin", offer_content='{"Neurotoxin":1}'))
        continue

    if source_name == "Alchemy Face Bar" and service_name == "Ritual Membership Botox":
        normalized_rows.append(make_row(row, service_name="Neurotoxin", offer_content='{"Neurotoxin":1}', discount_price="9"))
        continue

    if source_name == "Norman Medspa" and service_name == "Tox Membership Neurotoxins":
        normalized_rows.extend(
            [
                make_row(row, service_name="Botox", offer_content='{"Botox":1}', discount_price="9"),
                make_row(row, service_name="Dysport", offer_content='{"Dysport":1}', discount_price="9"),
                make_row(row, service_name="Jeuveau", offer_content='{"Jeuveau":1}', discount_price="9"),
                make_row(row, service_name="Daxxify", offer_content='{"Daxxify":1}', discount_price="9"),
                make_row(row, service_name="Xeomin", offer_content='{"Xeomin":1}', discount_price="9"),
            ]
        )
        continue

    if source_name == "Lou Lou Med Spa" and service_name == "The Refresh Membership":
        normalized_rows.extend(
            [
                make_row(
                    row,
                    service_name="Neurotoxin",
                    offer_content='{"Neurotoxin":"4 treatments/year, up to 60 units each"}',
                    discount_percent="",
                    min_unit="4",
                    delivered_unit="4",
                    unit_type="treatment/year",
                ),
                make_row(
                    row,
                    service_name="Select Services",
                    offer_content='{"Select Services":"10% off"}',
                    min_unit="1",
                    delivered_unit="1",
                    unit_type="service",
                ),
            ]
        )
        continue

    if source_name in {"Mira Aesthetic Cosmetic Surgery Center", "Younger Look"} and service_name in {"Fillers Membership", "Filler Membership Program"}:
        normalized_rows.extend(
            [
                make_row(row, service_name="Dermal Filler", offer_content='{"Dermal Filler":"10% off"}', discount_percent="10", unit_type="service"),
                make_row(row, service_name="B12 Shot", offer_content='{"B12 Shot":1}', discount_percent="", discount_amount="", unit_type="shot"),
            ]
        )
        continue

    if source_name == "Facial Aesthetics" and service_name == "faMEMBERSHIP Annual":
        normalized_rows.append(
            make_row(
                row,
                service_name="Products & Services",
                offer_content='{"Products & Services":"5% off"}',
                discount_percent="5",
                unit_type="service",
            )
        )
        continue

    if source_name == "RESTOR Medical Spa" and service_name == "Aesthetic Pure Membership":
        normalized_rows.extend(
            [
                make_row(row, service_name="Neurotoxin", offer_content='{"Neurotoxin":"$100 off"}', discount_amount="100", discount_percent="", unit_type="treatment"),
                make_row(row, service_category="Fillers & Other Injectables", service_name="Dermal Filler", offer_content='{"Dermal Filler":"$100 off"}', discount_amount="100", discount_percent="", unit_type="treatment"),
                make_row(row, service_name="Laser/Aesthetics", offer_content='{"Laser/Aesthetics":"20% off"}', discount_amount="", discount_percent="20", unit_type="service"),
                make_row(row, service_name="Product", offer_content='{"Product":"10% off"}', discount_amount="", discount_percent="10", unit_type="product"),
                make_row(row, service_name="Customized Product", offer_content='{"Customized Product":1}', discount_amount="", discount_percent="", unit_type="product"),
            ]
        )
        continue

    if source_name == "Norman Medspa" and service_name == "Gold Skin Membership: Filler & Products Discount":
        normalized_rows.extend(
            [
                make_row(row, service_name="Dermal Filler", offer_content='{"Dermal Filler":"10% off"}', discount_percent="10", unit_type="treatment"),
                make_row(row, service_name="Products & Services", offer_content='{"Products & Services":"10% off"}', discount_percent="10", unit_type="service"),
            ]
        )
        continue

    if source_name == "H-MD Medical Spa" and service_name == "Botox, Dysport, Xeomin":
        normalized_rows.extend(
            [
                make_row(row, service_name="Botox", offer_content='{"Botox":1}', discount_price="9"),
                make_row(row, service_name="Dysport", offer_content='{"Dysport":1}', discount_price="9"),
                make_row(row, service_name="Xeomin", offer_content='{"Xeomin":1}', discount_price="9"),
            ]
        )
        continue

    normalized_rows.append(row)

output_rows = normalized_rows

for row in output_rows:
    row["qa"] = "checked"
    for field in fieldnames:
        row.setdefault(field, "")

with open(OUTPUT_PATH, "w", newline="", encoding="utf-8-sig") as f:
    writer = csv.DictWriter(f, fieldnames=fieldnames)
    writer.writeheader()
    writer.writerows(output_rows)

print(f"Wrote {len(output_rows)} rows to {OUTPUT_PATH}")
