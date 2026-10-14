#!/usr/bin/env python3
"""Self-check for scrape_utils' html.parser fallback (no Firecrawl calls).

Fixture: a visible offer div with "$10.00/unit Botox" inside a <section>, and
an Elementor "hidden on every breakpoint" div elsewhere on the page with
"$8.00/unit Jeuveau". Asserts the hidden $8 never reaches price_text, the
visible $10 does, and the chosen container is the tight offer div — not the
whole #page wrapper.

Usage: python3 scripts/scrape_utils_check.py
"""

from __future__ import annotations

import sys

from scrape_utils import _containers_to_price_text, find_price_containers_html

FIXTURE_HTML = """
<html>
<body>
<div id="page">
  <header><nav>Home About Contact</nav></header>
  <main>
    <div class="elementor-hidden-desktop elementor-hidden-tablet elementor-hidden-mobile">
      <div class="offer-box hidden-offer">
        <h3>Jeuveau</h3>
        <p>First Tox $8.00/unit Jeuveau special</p>
      </div>
    </div>
    <section class="visible-specials">
      <div class="offer-box">
        <h3>Botox</h3>
        <p>$10.00/unit Botox regular price</p>
      </div>
    </section>
  </main>
  <footer>Prices start at $5 for consultations, call us today for more info about all our services and specials.</footer>
</div>
</body>
</html>
"""


def main() -> int:
    containers = find_price_containers_html(FIXTURE_HTML)
    price_text = _containers_to_price_text(containers)

    assert containers, "expected at least one price container, got none"
    assert "$8" not in price_text, f"hidden $8 leaked into price_text: {price_text!r}"
    assert "Jeuveau" not in price_text, f"hidden Jeuveau leaked into price_text: {price_text!r}"
    assert "$10" in price_text, f"visible $10 missing from price_text: {price_text!r}"

    assert len(containers) == 1, f"expected exactly one container, got {len(containers)}: {containers}"
    box = containers[0]
    assert box["className"] == "offer-box", f"expected the tight offer-box div, got class={box['className']!r}"
    assert box["id"] != "page", "container should not be the outer #page wrapper"
    assert "Home About" not in box["text"], "container text should not include nav chrome from #page"
    assert "consultations" not in box["text"], "container text should not include footer noise"

    print("OK:", {"containers": containers, "price_text": price_text})
    return 0


if __name__ == "__main__":
    sys.exit(main())
