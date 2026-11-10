from utils.scrape_markdown import (denoise_scrape_markdown, prepare_scrape_markdown, site_banner_blocks,
                                   truncate_scrape_markdown)

LASERAWAY_HEADER = """* [Laser Treatments](/services/laser-hair-removal/)

  The Black Friday Sale

  75% OFF LASER HAIR REMOVAL ǂ

  [Claim Offer](https://www.laseraway.com/book-now/)
* ![](https://res.cloudinary.com/laseraway-com/notification-campaign-d.jpg)

  ### The Black Friday Sale

  75% OFF LASER HAIR REMOVALǂ

  [Claim Offer](https://www.laseraway.com/book-now/)

  ǂ
  Select packages. Subject to
  [Offer Terms](https://www.laseraway.com/offer-terms/#lhr75)

  ![](https://res.cloudinary.com/laseraway-com/promotions-card-1)

  ### Promotions

  OUR FAVORITE WAYS TO SAVE
* ![](https://res.cloudinary.com/laseraway-com/notification-campaign-d.jpg)

  ### The Black Friday Sale

  75% OFF LASER HAIR REMOVALǂ

  [Claim Offer](https://www.laseraway.com/book-now/)

  ǂ
  Select packages. Subject to
  [Offer Terms](https://www.laseraway.com/offer-terms/#lhr75)

# Promotions

### Your First Botox Treatment Is $10 Per Unit
"""


def test_site_banner_blocks_keep_each_discount_banner_from_the_header_once() -> None:
    blocks = site_banner_blocks(LASERAWAY_HEADER)
    # 只取页头区域、以小标题开头且写明折扣的块；图片与没有折扣的「Promotions」卡片不取；重复的通知卡片只保留一次
    assert blocks == ["### The Black Friday Sale\n\n75% OFF LASER HAIR REMOVALǂ\n\n"
                      "[Claim Offer](https://www.laseraway.com/book-now/)\n\nǂ\n\nSelect packages. Subject to\n\n"
                      "[Offer Terms](https://www.laseraway.com/offer-terms/#lhr75)"]
    assert site_banner_blocks("# Promotions\n\n### 20% off Botox\n\nThis week only") == []


def test_denoise_removes_growth99_and_tracking_debug() -> None:
    raw = (
        "# Membership\n\n$295/month\n\n"
        "Growth99+\n\nLead Capture Form\n\n"
        "## Tracking Debug\n\nN/A\n"
    )
    out = denoise_scrape_markdown(raw)
    assert "$295/month" in out
    assert "Growth99" not in out
    assert "Tracking Debug" not in out


def test_denoise_trims_leading_skip_link() -> None:
    raw = "[Skip to content](https://x.com)\n\n- phone\n\n# Pricing\n\nBotox $12/unit"
    out = denoise_scrape_markdown(raw)
    assert out.startswith("# Pricing")
    assert "Skip to content" not in out


def test_truncate_adds_marker() -> None:
    out = truncate_scrape_markdown("a\n" * 20, max_chars=10)
    assert "truncated" in out


def test_denoise_removes_cookie_and_subscribe_noise_without_removing_price() -> None:
    raw = (
        "# Summer Special\n\nBotox $10/unit through August 31.\n\n"
        "## Cookie Preferences\n\nManage Cookie Settings\n\n"
        "## Subscribe to Our Newsletter\n\nEnter your email address\n\n"
        "## Footer\n\nCopyright 2026 Example"
    )

    out = denoise_scrape_markdown(raw)

    assert "Botox $10/unit" in out
    assert "Cookie Preferences" not in out
    assert "Subscribe to Our Newsletter" not in out
    assert "Copyright 2026" not in out
