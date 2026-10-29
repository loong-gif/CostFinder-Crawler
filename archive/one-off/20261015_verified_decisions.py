from copy import deepcopy


def build(database, samples):
    decisions = {}
    operations = {}
    additions = {}

    def decide(sid, status, reason, offer_ids=()):
        assert sid in samples and sid not in decisions, sid
        decisions[sid] = {"status": status, "reason": reason, "offer_ids": list(offer_ids)}

    def update(pid, table, row_id, **values):
        operations.setdefault(pid, []).append({"table": table, "id": row_id, "values": values})

    def new(pid, sid, text, items, **values):
        additions.setdefault(pid, []).append({"sample_id": sid, "offer_raw_text": text, "items": items,
                                            "values": values})

    def item(name, quantity=None, service_id=None):
        return {"item_name": name, "service_id": service_id, "delivered_units": quantity, "unit_price": None}

    def from_fact(pid, sid, fact=None, name=None, **overrides):
        fact = fact if fact is not None else samples[sid]["facts"]
        name = name or fact.get("item_name") or fact.get("name")
        assert name, sid
        scope = fact.get("scope") or fact.get("area")
        label = name + (" — " + scope if scope else "")
        quantity = fact.get("fixed_delivered_quantity", fact.get("fixed_quantity", fact.get("delivered_quantity")))
        conditions = list(fact.get("eligibility", [])) + list(fact.get("source_common_eligibility", []))
        if fact.get("campaign_end_date"):
            conditions.append("Valid through " + fact["campaign_end_date"] + ".")
        if fact.get("campaign_end_month_day"):
            conditions.append("Valid through October 31; year not stated.")
        sale = fact.get("sale_total", fact.get("starting_price"))
        regular = fact.get("regular_total")
        rate = fact.get("sale_rate", fact.get("unit_rate"))
        model = "from" if fact.get("price_is_from") else "total"
        if sale is None and rate is not None:
            sale, regular, model = rate, fact.get("regular_rate"), "per_unit"
        components = fact.get("components")
        package = bool(fact.get("explicit_package"))
        parts = []
        if components:
            for component in components:
                if isinstance(component, str):
                    parts.append(item(component))
                else:
                    parts.append(item(component["name"], component.get("quantity")))
        else:
            parts = [item(label, quantity)]
        if quantity is not None:
            conditions.append("Fixed delivered quantity: " + str(quantity) + (" " + fact["unit"] if fact.get("unit") else "") + ".")
        for key in ("minimum_purchase_quantity", "minimum_tox_quantity", "minimum_paid_quantity"):
            if fact.get(key) is not None:
                conditions.append("Minimum purchase: " + str(fact[key]) + " " + fact.get("unit", "units") + ".")
        if fact.get("minimum_spend") is not None:
            conditions.append("Minimum spend: $" + str(fact["minimum_spend"]) + ".")
        if fact.get("duration_minutes"):
            conditions.append(str(fact["duration_minutes"]) + " minutes.")
        if fact.get("first_treatment_only"):
            conditions.append("First treatment only.")
        values = {"discount_price": sale, "regular_price": regular, "discount_amount": fact.get("discount_amount"),
                  "discount_percent": fact.get("discount_percent"), "is_package": package, "price_model": model,
                  "conditions": list(dict.fromkeys(conditions)),
                  "is_new_customer_required": fact.get("new_customer_only", fact.get("is_new_customer_required")),
                  "is_membership_required": fact.get("member_only", fact.get("is_membership_required"))}
        values.update(overrides)
        raw = label
        if sale is not None:
            raw += " $" + str(sale) + (" per " + fact.get("unit", "unit") if model == "per_unit" else "")
        if regular is not None:
            raw += " (regular $" + str(regular) + ")"
        if fact.get("discount_amount") is not None:
            raw += "; save $" + str(fact["discount_amount"])
        if quantity is not None:
            raw += "; quantity " + str(quantity) + (" " + fact["unit"] if fact.get("unit") else "")
        new(pid, sid, raw, parts, **values)

    # 保护记录所在父活动的日期仍为历史日期，整笔来源保留审查。
    for sid in samples:
        if sid.startswith("VR001_"):
            decide(sid, "review", "父活动日期需要调整，来源包含人工保护 Offer 8590。", [8590])
        elif sid.startswith("VR033_"):
            decide(sid, "review", "同一品牌页面对应三个商家地点，需要确认每项优惠的适用地点。")
        elif sid.startswith("VR037_"):
            decide(sid, "review", "共用资格条款在已核验截图中被遮挡，需要完整条款证据。")

    # 相同来源和价格层级唯一匹配，保留现有 Offer 与 Item。
    luxe = {
        "VR006_001": [8653, 8654], "VR006_003": [8648, 8650, 8649],
        "VR006_004": [8655, 8656], "VR006_005": [8651, 8652], "VR006_006": [10749],
    }
    for sid, offer_ids in luxe.items():
        if sid == "VR006_006":
            decide(sid, "noop", "现有 20% 优惠及 NULL 服务关联与证据一致。", offer_ids)
            continue
        decide(sid, "update", "清除缺少来源依据的原价，补齐套餐和适用条件。", offer_ids)
        for oid in offer_ids:
            old = next(m for m in database["offers"] if m["id"] == oid)
            conditions = list(old["conditions"] or [])
            if sid == "VR006_001":
                conditions.append("Available at all locations including Fort Collins.")
            if sid == "VR006_004":
                conditions.append("Not available in RiNo currently; subject to change.")
            percent = {8651: 15, 8652: 25}.get(oid)
            update(8, "offers", oid, regular_price=None, discount_percent=percent,
                   discount_amount=old["discount_amount"], is_package=True, conditions=conditions)
            for existing_item in database["items"]:
                if existing_item["offer_id"] == oid:
                    update(8, "items", existing_item["offer_item_id"], unit_price=None)
    decide("VR006_002", "review", "Fort Collins 专属套餐需要核对地点对应的 Business。")

    for prefix, pid, location in [("VR015", 3604, "Costa Mesa"), ("VR017", 3605, "Rancho"), ("VR019", 3606, "Whittier")]:
        for suffix, text, items in [
            ("001", "Ultimate Microneedling + Get the Neck FREE! Includes Skin Booster! $599 (reg. $799).",
             [("Ultimate Microneedling", None), ("Neck treatment", None), ("Skin Booster", None)]),
            ("002", "VI Peel Original + 2 Areas of Toxin $699 (reg. $920).",
             [("VI Peel Original", None), ("Toxin", 2)]),
        ]:
            sid = prefix + "_" + suffix
            fact = samples[sid]["facts"]
            decide(sid, "insert", "数据库缺少已核验套餐，价格及地点明确。")
            new(pid, sid, text, [{"item_name": name, "service_id": None, "delivered_units": quantity,
                                 "unit_price": None} for name, quantity in items],
                discount_price=fact["sale_total"], regular_price=fact["regular_total"], is_package=True,
                conditions=["Promotions cannot be combined with any other offers.",
                            f"Valid through 10/31/26 at Skin Perfect {location} only."])
        decide(prefix + "_003", "review", "20% 标识的适用范围仍存在疑点，保留当前优惠。")

    for sid, oid in [("VR005_001", 8686), ("VR005_002", 8690), ("VR005_003", 8687)]:
        decide(sid, "noop", "价格、单位和 2026-10-31 有效期已同步。", [oid])
    for sid, name, sale, quantity, package in [
        ("VR005_004", "Versa Filler", 650, None, False),
        ("VR005_005", "Juvéderm Filler", 750, None, False),
        ("VR005_006", "Morpheus8, Face & Neck", 2499, 3, True),
    ]:
        decide(sid, "insert", "当前优惠明确，缺少对应 active Offer；历史记录保持原状。")
        new(168, sid, (f"{name} $2,499 for 3 treatments." if package else f"{name} ${sale}/syringe."),
            [{"item_name": name, "service_id": None, "delivered_units": quantity,
              "unit_price": None if package else sale}], discount_price=sale,
            price_model="total" if package else "per_unit", is_package=package,
            conditions=["Available through October 31, 2026.", "Cannot be combined with other offers."])

    # 同一商家按当前可见资格区分普通优惠及附加优惠。
    allure_existing = {1: 8127, 2: 8128, 3: 8129, 4: 8130, 10: 8136, 13: 8139,
                       15: 10721, 16: 8126, 17: 8140, 20: 8123}
    for index, oid in allure_existing.items():
        sid = f"VR007_{index:03d}"
        if index in (1, 15, 16, 17, 20):
            decide(sid, "noop", "现有价格及对应计价口径与已核验证据一致。", [oid])
            continue
        decide(sid, "update", "套餐包含六次治疗，补齐每个治疗部位的数量。", [oid])
        update(217, "offers", oid, conditions=["Package includes 6 treatments."])
        for old_item in database["items"]:
            if old_item["offer_id"] == oid:
                update(217, "items", old_item["offer_item_id"], delivered_units=6, unit_price=None)
    for index in (5, 6, 7, 8, 9, 11, 12):
        sid = f"VR007_{index:03d}"
        f = samples[sid]["facts"]
        decide(sid, "insert", "缺少该部位六次治疗套餐。")
        from_fact(217, sid, name="Laser Hair Removal", conditions=["Package includes 6 treatments.", "Area: " + f["scope"]])
    decide("VR007_014", "insert", "Jeuveau 优惠独立于已停用 Letybo，保留最低购买量和同次预约要求。")
    from_fact(217, "VR007_014", name="Jeuveau", conditions=samples["VR007_014"]["facts"]["eligibility"])
    for index in (18, 19):
        sid = f"VR007_{index:03d}"
        f = samples[sid]["facts"]
        decide(sid, "insert", "新增明确的 Radiesse 抵扣优惠，保留 Xperience+ 资格。")
        from_fact(217, sid, dict(f, regular_total=f["regular_rate"]), name="Radiesse — " + f["unit"])
    decide("VR007_021", "review", "截图中的原价、售价与节省金额互相矛盾。", [8124])

    decisions["VR007_020"].update(status="review", reason="当前截图中的相关图片区域未完整显示，保留旧 CO2 优惠。")

    for sid, oid in [("VR002_001", 10746), ("VR002_002", 10747)]:
        old = next(m for m in database["offers"] if m["id"] == oid)
        decide(sid, "update", "补齐不可叠加条件，保留 Blanket Offer 和 NULL 服务关联。", [oid])
        update(207, "offers", oid, conditions=old["conditions"] + ["Cannot be combined with other offers."])
    decide("VR002_003", "noop", "套餐、价格及提供者条件已同步。", [10392])
    decide("VR002_004", "review", "现有 10393 与 10748 表达重复优惠，需要确定保留记录。", [10393, 10748])
    decide("VR002_005", "noop", "三种神经调节剂的价格、单位和 Liz 限定条件一致。", [10394, 10395, 10396])

    decide("VR003_001", "review", "现有 10434 与 10720 表达重复优惠，需要确定保留记录。", [10434, 10720])
    decide("VR003_002", "noop", "新客 Botox $10/unit 已同步。", [8376])
    decide("VR004_001", "noop", "同一优惠已由当前主活动 871 的 Offer 8376 表达，保留历史活动停用状态。", [8376])
    decide("VR008_001", "review", "同一页面对应两条 active 活动及两条不同资格的 Botox Offer，需要明确活动归属。", [8222, 10719])

    for sid, oid in [("VR041_001", 8519), ("VR041_002", 10424), ("VR041_004", 8522),
                     ("VR041_005", 8020), ("VR041_006", 8021), ("VR041_008", 8022)]:
        decide(sid, "noop", "已有优惠价格与已核验事实一致；页面第二页未纳入移除证据。", [oid])
    for index in (3, 9, 10):
        decide(f"VR041_{index:03d}", "review", "套餐数量或金额角色不完整，保留当前值。")
    decide("VR041_011", "update", "来源没有原价依据，清空旧原价和派生折扣。", [8024])
    update(216, "offers", 8024, regular_price=None, discount_amount=None, discount_percent=None)
    decide("VR041_007", "insert", "新增明确血液检查优惠；缺少服务映射保存 NULL。")
    from_fact(216, "VR041_007")

    # 当前优惠单独归入新活动，历史九月组合保留原正文并停用。
    current_ageless = -210
    old_current = {"VR056_002": 8558, "VR056_003": 8573, "VR056_004": 8570, "VR056_005": 8583}
    for sid, oid in old_current.items():
        decide(sid, "update", "保留 Offer ID，补齐前十名和同次预约资格，并归入当前优惠活动。", [oid])
        update(210, "offers", oid, promotion_id=current_ageless, conditions=samples[sid]["facts"]["eligibility"])
    decide("VR056_001", "update", "更新 Botox 买四十送二十优惠，记录六十单位交付及明确截止日期。", [8580])
    update(210, "offers", 8580, promotion_id=current_ageless,
           offer_raw_text="Buy 40 Units of Botox, Get 20 Units FREE ($13/unit: 60 Units Total).",
           regular_price=780, discount_price=520, discount_amount=None, discount_percent=None,
           conditions=samples["VR056_001"]["facts"]["eligibility"])
    update(210, "items", 2427, delivered_units=60)
    for sid in ("VR056_owner_018", "VR056_owner_019"):
        decide(sid, "noop", "该组合明确于 2026-09-30 截止，不新增或激活历史优惠。")
    for sid in ("VR056_006", "VR056_owner_009", "VR056_owner_011", "VR056_owner_040", "VR056_owner_041"):
        decide(sid, "noop", "常规价格说明或长期会员权益不作为促销新增依据。")
    for sid in samples:
        if not sid.startswith("VR056_owner_") or sid in decisions:
            continue
        f = deepcopy(samples[sid]["facts"])
        decide(sid, "insert", "新增已核验的独立优惠，按来源记录价格、数量和资格。")
        if sid == "VR056_owner_024":
            f.update(discount_percent=None, unit="area")
            f["eligibility"] += ["50% off the regular price of the second treatment area only."]
        if f.get("regular_rate") is not None and f.get("sale_total") is not None:
            f["eligibility"] += [f"Regular rate: ${f['regular_rate']} per {f.get('unit', 'unit')}."]
        if f.get("paid_quantity") is not None:
            f["eligibility"] += [f"Buy {f['paid_quantity']}, receive {f['free_quantity']} free."]
        from_fact(current_ageless, sid, f)
    update(210, "promotions", 210, is_active=False, is_offer_extracted=False)

    decide("VR042_001", "review", "Offer 10030 受到管理员操作保护，保留其字段和版本。", [10030])
    decide("VR042_002", "update", "最低购买一百二十单位并额外赠送三十单位，清除固定一百五十交付量及无来源折扣。", [10031])
    update(215, "offers", 10031, discount_percent=None,
           conditions=["First neuromodulator treatment at Viva.", "Minimum purchase 120 units of Dysport; receive 30 additional free units.", "Valid through October 31, 2026."])
    update(215, "items", 4238, delivered_units=None, unit_price=None)
    update(215, "terms", 10031, min_quantity=120, min_unit="unit")
    for sid, entries in [
        ("VR042_003", [(10035, None)]), ("VR042_004", [(10036, None)]),
        ("VR042_005", [(10037, 350)]), ("VR042_006", [(10040, 200)]),
        ("VR042_007", [(10041, 60), (10042, 85), (10043, 85), (10044, 110)]),
        ("VR042_008", [(8207, 150), (8208, 150), (10045, 500), (10046, 500), (10047, 1050), (10048, 1050)]),
    ]:
        decide(sid, "update", "补齐优惠资格，清除反推原价；明确套餐的 Item 单价置空。", [x[0] for x in entries])
        for oid, saving in entries:
            old = next(m for m in database["offers"] if m["id"] == oid)
            conditions = list(old["conditions"] or [])
            values = {}
            if sid == "VR042_008":
                conditions += samples[sid]["facts"]["eligibility"]
            else:
                conditions.append("Book by October 31, 2026 at Viva Austin, Dallas or Round Rock med spas.")
            if saving is not None:
                values.update(regular_price=None, discount_amount=saving, discount_percent=None)
            if oid in (10037, 10040, 10041, 10042, 10043, 10044):
                values["is_package"] = True
                for old_item in database["items"]:
                    if old_item["offer_id"] == oid:
                        update(215, "items", old_item["offer_item_id"], unit_price=None)
            update(215, "offers", oid, conditions=conditions, **values)
    from_fact(215, "VR042_003", {"item_name": "Sculptra + Microneedling", "explicit_package": True,
        "components": ["Sculptra", "Microneedling"], "fixed_quantity": 3,
        "eligibility": ["Save up to $500 on a series of 3; savings is an upper bound.", "Book by October 31, 2026."]})
    for f in samples["VR042_005"]["facts"]["alternatives"][1:]:
        from_fact(215, "VR042_005", dict(f, explicit_package=True,
                  eligibility=["Face; series of 3.", "Book by October 31, 2026."]))
    for n, name in [(1, "Palo Santo Massage Package"), (2, "Moor Mud Body Polishing Treatment"),
                    (3, "Deep Renewal Hydrafacial Package"), (4, "Grounding Retreat Massage + Hydrafacial Package"),
                    (9, "Microneedling face"), (12, "Votiva")]:
        sid = f"VR042_discovered_{n:03d}"
        decide(sid, "insert", "新增页面明确展示的优惠，保留总价、数量和限定条件。")
        from_fact(215, sid, name=name)
    for n in (5, 6):
        sid = f"VR042_discovered_{n:03d}"
        f = samples[sid]["facts"]
        decide(sid, "insert", "新增广域优惠，保留 NULL 服务关联和适用条件。")
        from_fact(215, sid, name=f["scope"])
    decide("VR042_discovered_007", "insert", "推荐人与新客分别获得信用额度，金额保留在原文和条件中。")
    new(215, "VR042_discovered_007", "Receive $25 in Viva credit for every friend referred, plus give $25 to them for their first visit.",
        [item("Referral credit")], conditions=["Friend referral", "Friend first visit", "$25 credit for referrer and $25 for referred friend"])
    decide("VR042_discovered_008", "insert", "新增 Allē 首次加入优惠并保留资格要求。")
    from_fact(215, "VR042_discovered_008", name="Botox", conditions=["New to Alle rewards; qualification required.", "Can combine with Viva new client Botox offer."])
    decide("VR042_discovered_010", "review", "买二送一与三次 IPL 套餐存在范围重合，需要确认具体部位和适用价。")
    decide("VR042_discovered_011", "insert", "七百五十美元减免仅适用于第二套 Evolve 或第二个部位。")
    new(215, "VR042_discovered_011", "$750 off a 2nd Evolve Series or 2nd body area when purchasing an Evolve Body Contouring Series.",
        [item("Evolve Body Contouring Series", 6)],
        conditions=["Purchase an Evolve Body Contouring Series.", "$750 off second series or second body area only.", "Each series includes six treatments."])
    for n in (13, 14):
        sid = f"VR042_discovered_{n:03d}"
        decide(sid, "insert", "新增六次及十二次系列套餐，保留服务时长和永不过期条款。")
        for f in samples[sid]["facts"]["alternatives"]:
            from_fact(215, sid, dict(f, explicit_package=True, eligibility=["60 minutes per session.", "Series credits roll over and never expire."]))

    for index, oid in [(1, 9797), (2, 10730)]:
        decide(f"VR022_{index:03d}", "noop", "现有金额、套餐或 Blanket 服务关联与当前证据一致。", [oid])
    decide("VR022_003", "update", "将 VAMP 与赠送 SkinBetter Peel 分为原子 Item，并保留原 Offer ID。", [9799])
    update(176, "items", 3449, item_name="SkinPen Microneedling", service_id=3259, delivered_units=3, unit_price=None)
    update(176, "items", 5467, item_name="VAMP", service_id=None, delivered_units=3, unit_price=None)
    operations[176].append({"table": "items", "id": -17601, "insert": True,
                           "values": dict(item("SkinBetter Peel", 1), offer_id=9799)})
    decide("VR022_004", "insert", "新增八次 Venus Legacy 加三次 Emsculpt 手臂治疗套餐。")
    from_fact(176, "VR022_004", name="Venus Legacy + Emsculpt", conditions=["8 Venus Legacy treatments for one area.", "Receive 3 free Emsculpt arm treatments."])

    for sid, oid in [("VR024_001", 9220), ("VR024_002", 9221)]:
        decide(sid, "update", "补齐首次到访资格，保留已有套餐和 Item ID。", [oid])
        update(819, "offers", oid, is_new_customer_required=True, conditions=["First-visit clients only."])
    decide("VR024_003", "review", "HIFU 正文部位与图片部位冲突，保留历史停用记录。", [9222])
    decide("VR024_004", "update", "补齐 Morpheus8 首次治疗条件，保留已有价格。", [9223])
    update(819, "offers", 9223, conditions=["Full face.", "First treatment only."])
    for sid in ("VR024_005", "VR025_001"):
        decide(sid, "review", "同价优惠已有 inactive 记录 9225，当前活动归属需要确认，禁止自动恢复。", [9225])
    decide("VR024_006", "noop", "Microneedling + Exosome 套餐价格及首次治疗条件一致。", [9224])
    decide("VR024_007", "insert", "当前小部位 XERF 优惠区别于历史全脸报价，单独保存。")
    from_fact(819, "VR024_007")
    decide("VR027_001", "review", "相同一年无限次优惠已有 inactive 记录 9822，保持历史状态并审查。", [9822])

    for sid, oid, iid in [("VR029_001", 9813, 3463), ("VR029_002", 9814, 3464)]:
        decide(sid, "update", "修正身体塑形入门套餐的服务范围，清除错误激光脱毛关联并补齐排除条件。", [oid])
        f = samples[sid]["facts"]
        conditions = f["eligibility"] + ["Includes consultation, first full treatment and a personalized technology plan."]
        if oid == 9814:
            conditions += ["Includes an extra MDS Contour session and extended treatment time.", "10% off retail products on the visit day."]
        update(763, "offers", oid, is_package=True, conditions=conditions, is_new_customer_required=True)
        update(763, "items", iid, item_name="Introductory body sculpting treatment", service_id=None, delivered_units=1, unit_price=None)
    operations[763].append({"table": "items", "id": -76301, "insert": True,
                           "values": dict(item("MDS Contour", 1), offer_id=9814)})

    for sid, sample in samples.items():
        if sid in decisions:
            continue
        status = sample["status"]
        if status in ("context_only", "heading_only") or sample["facts"].get("heading_not_independent_offer"):
            decide(sid, "noop", "共用说明或标题随对应优惠处理，不创建独立优惠。")
        elif sid.startswith("VR034_"):
            decide(sid, "noop", "活动 626 及全部关联优惠已 inactive，保留历史字段。")
        elif not status.startswith(("clear", "partial", "contradictory")):
            decide(sid, "noop", "核验内容属于非促销信息、无当前优惠页面或历史停用来源，保持业务记录。")

    return decisions, operations, additions
