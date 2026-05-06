# CostFinder Promo Page Discovery Actor

Apify Actor built with crawl4ai (+ BeautifulSoup post-processing) for discovering pricing, service, membership, and promotion pages and exporting LLM-friendly page content.

## What It Does

- accepts a homepage or a specific subpage as input
- expands sitemap files recursively when the input is a homepage
- scores candidate URLs and seeds the crawl with the strongest pricing/promo/service pages
- optionally enables same-domain discovery when sitemap coverage is weak
- crawls pages with `crawl4ai.AsyncWebCrawler` and normalizes content for extraction
- extracts raw page segments, filters noisy blocks, removes nested duplicates, and exports the final compatible page content payload
- skips blog, about, contact, policy, and learn/article style content pages because they usually do not contain offer information

## Output Fields

The Actor output is intentionally kept compact for downstream compatibility.

- `subpage_url`: crawled page URL
- `page_content`: LLM-ready compressed content built from filtered pricing/promo/service segments
- `domain`: normalized netloc
- `name`: site/page name derived from `og:site_name`, title, or h1

## Input

- `website_url`: homepage or exact subpage URL
- `start_urls`: optional additional seed URLs
- `maxCrawlPages`: max requests for the run
- `needs_ocr`: when `true`, pages marked as OCR targets will be screenshot and OCR text will be used as preprocessing input before the same filtering/export pipeline

`start_urls` also supports item-level OCR marking by passing `needs_ocr: true` in each item object. The crawler will carry that flag to discovered same-domain links from that seed.

## How Discovery Works

1. Normalize the input URL and domain.
2. If the input is a homepage, recursively expand `sitemap.xml`, `sitemap_index.xml`, and `wp-sitemap.xml`.
3. Score candidate URLs with positive keywords like `pricing`, `services`, `membership`, `promotions`, `offers`, `botox`, `filler`.
4. Penalize clearly irrelevant URLs such as `login`, `cart`, `checkout`, `privacy`, `blog`, and image/PDF assets.
5. If there are fewer than 3 high-confidence candidate pages, enable same-domain discovery from the homepage and hub pages.
6. Blog, learn, news, article, about, contact, and policy-style URLs are excluded from both candidate seeding and export.

## How Page Filtering Works

1. Remove non-content elements such as `script`, `style`, `noscript`, `svg`, `nav`, `footer`, `header`, `form`, and `button`.
2. Extract container-level text segments from `main`, `section`, `article`, `div`, `li`, `tr`, and `p`.
3. Score segments based on:
   - prices
   - promotion and membership language
   - service/treatment keywords
   - date and validity language
4. Penalize or drop:
   - review/testimonial text
   - CTA-only blocks
   - login/cart/account blocks
   - social and generic slogan content
5. Remove exact duplicates and child blocks already covered by a higher-value parent block.
6. Skip exporting pages whose filtered content is identical to, or heavily overlaps with, a page that was already exported in the same run.

## Local Development

```bash
pip install -r requirements.txt
python3 -m src
```

If you prefer Apify CLI locally, use:

```bash
apify run --entrypoint src/main.py
```

## Testing

Run the unit tests from the Actor root:

```bash
python3 -m unittest discover -s tests
```
