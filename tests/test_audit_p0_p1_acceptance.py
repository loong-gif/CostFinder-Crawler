from scripts.audit_p0_p1_acceptance import build_report


class FakeClient:
    def fetch_rows(self, table, select, **kwargs):
        if table == "promo_offer_master":
            return [
                {
                    "id": 1,
                    "promotion_id": 2,
                    "is_active": True,
                    "price_model": "per_unit",
                    "discount_price": 10,
                    "discount_amount": None,
                    "regular_price": None,
                    "promo_offer_items": [
                        {"item_name": "Botox", "service_id": 3, "unit_price": 10}
                    ],
                    "clinic_promotions": {"promotion_id": 2, "is_active": True},
                }
            ]
        if table == "clinic_services":
            return [{"service_id": 3, "service_name": "Botox"}]
        raise AssertionError(table)


def test_live_acceptance_report_does_not_claim_security_gate_is_verified():
    report = build_report(FakeClient())

    assert report["data_gates_pass"] is True
    assert report["security_definer_without_internal_authorization"] is None
    assert report["final_acceptance"] is False
