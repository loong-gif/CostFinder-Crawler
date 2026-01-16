Social Media Finder is an intelligent crawler tool for extracting social media information and detecting pricing pages from websites.[1]

## Project Overview

Social Media Finder focuses on two main capabilities: social media link extraction and pricing page discovery across target websites.  It is designed for batch processing of multiple domains and generating structured reports for downstream analysis.[1]

## Features

### Social media discovery

- ✅ Crawl target websites to collect social media links (e.g. footer, contact, embedded sections).[1]
- ✅ Focus on extracting Instagram (IG) and Facebook (FB) profiles with high accuracy.[1]
- ✅ Support recognition of multiple URL formats and patterns.[1]
- ✅ Automatically extract account names and profile URLs.[1]
- ✅ Filter irrelevant or noisy links to improve data quality.[1]

### Pricing page finder 🆕

- ✅ Intelligently identify pricing-related pages within a site.[1]
- ✅ Automatically detect price information in page content.[1]
- ✅ Support batch processing for multiple domains.[1]
- ✅ Provide multi-level confidence scores (high / medium / low).[1]
- ✅ Generate reports in JSON, CSV, and TXT formats.[1]

## Tech Stack

- **Python** 3.8+ as the core runtime.[1]
- **requests** for HTTP requests and page downloading.[1]
- **BeautifulSoup4** for HTML parsing.[1]
- **lxml** as a high-performance HTML/XML parser.[1]
- **validators** for URL validation and normalization.[1]

## Project Structure

```bash
Social_Media_Finder/
├── README.md                      # Project documentation
├── BATCH_PROCESS_README.md        # Social media batch processing guide
├── PRICING_PAGES_README.md        # Pricing page finder usage guide 🆕
├── requirements.txt               # Project dependencies
├── config.py                      # Configuration file
├── main.py                        # Main entry for single-website run
├── batch_process.py               # Batch social media processing script
├── find_pricing_pages.py          # Pricing page finder script 🆕
├── input_website_list.txt         # Raw input URL list
├── input_website_list_cleaned.txt # Cleaned domain list
├── crawler/                       # Core crawler modules
│   ├── __init__.py
│   ├── base_crawler.py            # Base crawler class
│   ├── social_media_finder.py     # Social media finder
│   ├── pricing_page_finder.py     # Pricing page finder 🆕
│   └── parsers.py                 # Link parsers
├── utils/                         # Utility modules
│   ├── __init__.py
│   ├── url_validator.py           # URL validation utilities
```


## Usage (Quick Start)

- Install dependencies with `pip install -r requirements.txt`.[1]
- For a single website, configure `config.py` and run `python main.py`.[1]
- For batch social media extraction, prepare `input_website_list.txt` and run `python batch_process.py`.[1]
- For pricing page detection, use `find_pricing_pages.py` with the cleaned domain list.[1]

[1](https://github.com/loong-gif/CostFinder-Crawler/edit/main/README.md)
