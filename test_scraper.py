#!/usr/bin/env python3
"""
Test script to verify the eBay scraper fixes.
Tests URL validation, seller verification, and parsing logic.
"""

import sys
import os
import json
from bs4 import BeautifulSoup

# Add parent directory to path
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from ebay_scraper import (
    is_valid_product_url,
    extract_item_id,
    clean_url,
    DetailScraper,
    ListingScraper,
)


def test_is_valid_product_url():
    """Test URL validation function."""
    print("=" * 60)
    print("TEST: is_valid_product_url()")
    print("=" * 60)

    # Valid product URLs
    valid_urls = [
        "https://www.ebay.com/itm/123456789012",
        "https://www.ebay.com/itm/123456789012?hash=item123",
        "https://www.ebay.com/itm/123456789012?_trkparms=abc",
        "https://www.ebay.com/itm/987654321098",
        "https://www.ebay.com/itm/123456789012/extra/path",
        "https://www.ebay.com/itm/123456789012?hash=item123#anchor",
    ]

    # Invalid URLs
    invalid_urls = [
        "",
        "https://www.ebay.com/itm/abc123",
        "https://www.ebay.com/itm/123",
        "https://www.ebay.com/itm/",
        "https://www.ebay.com/usr/seller123",
        "https://www.ebay.com/str/storename",
        "https://www.google.com/itm/123456789012",
        "not-a-url",
    ]

    all_passed = True

    for url in valid_urls:
        result = is_valid_product_url(url)
        status = "PASS" if result else "FAIL"
        if not result:
            all_passed = False
        print(f"  [{status}] Valid: {url}")

    for url in invalid_urls:
        result = is_valid_product_url(url)
        status = "PASS" if not result else "FAIL"
        if result:
            all_passed = False
        print(f"  [{status}] Invalid: {url}")

    print(f"\n  Result: {'ALL PASSED' if all_passed else 'SOME FAILED'}")
    return all_passed


def test_extract_item_id():
    """Test item ID extraction."""
    print("\n" + "=" * 60)
    print("TEST: extract_item_id()")
    print("=" * 60)

    test_cases = [
        ("https://www.ebay.com/itm/123456789012", "123456789012"),
        ("https://www.ebay.com/itm/123456789012?hash=item123", "123456789012"),
        ("https://www.ebay.com/itm/987654321098", "987654321098"),
        ("https://www.ebay.com/usr/seller123", ""),
        ("https://www.ebay.com/str/storename", ""),
        ("", ""),
    ]

    all_passed = True
    for url, expected in test_cases:
        result = extract_item_id(url)
        status = "PASS" if result == expected else "FAIL"
        if result != expected:
            all_passed = False
        print(f"  [{status}] {url} -> {result} (expected: {expected})")

    print(f"\n  Result: {'ALL PASSED' if all_passed else 'SOME FAILED'}")
    return all_passed


def test_clean_url():
    """Test URL cleaning."""
    print("\n" + "=" * 60)
    print("TEST: clean_url()")
    print("=" * 60)

    test_cases = [
        ("https://www.ebay.com/itm/123456789012?hash=item123&_trkparms=abc", "https://www.ebay.com/itm/123456789012"),
        ("https://www.ebay.com/itm/123456789012#anchor", "https://www.ebay.com/itm/123456789012"),
        ("https://www.ebay.com/itm/123456789012", "https://www.ebay.com/itm/123456789012"),
        ("", ""),
    ]

    all_passed = True
    for url, expected in test_cases:
        result = clean_url(url)
        status = "PASS" if result == expected else "FAIL"
        if result != expected:
            all_passed = False
        print(f"  [{status}] {url} -> {result} (expected: {expected})")

    print(f"\n  Result: {'ALL PASSED' if all_passed else 'SOME FAILED'}")
    return all_passed


def test_seller_verification():
    """Test seller verification logic."""
    print("\n" + "=" * 60)
    print("TEST: Seller Verification")
    print("=" * 60)

    # Test HTML with matching seller
    html_match = """
    <html>
    <head>
        <script type="application/ld+json">
        {
            "@context": "https://schema.org",
            "@type": "Product",
            "name": "Test Product",
            "offers": {
                "@type": "Offer",
                "seller": {
                    "@type": "Organization",
                    "name": "ninjanodedc"
                }
            }
        }
        </script>
    </head>
    <body>
        <div class="seller-name">ninjanodedc</div>
    </body>
    </html>
    """

    # Test HTML with non-matching seller
    html_mismatch = """
    <html>
    <head>
        <script type="application/ld+json">
        {
            "@context": "https://schema.org",
            "@type": "Product",
            "name": "Test Product",
            "offers": {
                "@type": "Offer",
                "seller": {
                    "@type": "Organization",
                    "name": "other_seller"
                }
            }
        }
        </script>
    </head>
    <body>
        <div class="seller-name">other_seller</div>
    </body>
    </html>
    """

    # Test HTML with no seller info
    html_no_seller = """
    <html>
    <head><title>Test Product</title></head>
    <body><div>No seller info here</div></body>
    </html>
    """

    all_passed = True

    # Test matching seller
    scraper = DetailScraper(expected_seller="ninjanodedc")
    soup = BeautifulSoup(html_match, "lxml")
    result = scraper._verify_seller(soup)
    status = "PASS" if result else "FAIL"
    if not result:
        all_passed = False
    print(f"  [{status}] Matching seller (ninjanodedc) -> {result} (expected: True)")

    # Test non-matching seller
    scraper = DetailScraper(expected_seller="ninjanodedc")
    soup = BeautifulSoup(html_mismatch, "lxml")
    result = scraper._verify_seller(soup)
    status = "PASS" if not result else "FAIL"
    if result:
        all_passed = False
    print(f"  [{status}] Non-matching seller (other_seller) -> {result} (expected: False)")

    # Test no seller info (should pass)
    scraper = DetailScraper(expected_seller="ninjanodedc")
    soup = BeautifulSoup(html_no_seller, "lxml")
    result = scraper._verify_seller(soup)
    status = "PASS" if result else "FAIL"
    if not result:
        all_passed = False
    print(f"  [{status}] No seller info -> {result} (expected: True)")

    # Test no expected seller (should pass)
    scraper = DetailScraper(expected_seller=None)
    soup = BeautifulSoup(html_mismatch, "lxml")
    result = scraper._verify_seller(soup)
    status = "PASS" if result else "FAIL"
    if not result:
        all_passed = False
    print(f"  [{status}] No expected seller -> {result} (expected: True)")

    # Test seller name normalization
    scraper = DetailScraper(expected_seller="ninja_node_dc")
    soup = BeautifulSoup(html_match, "lxml")
    result = scraper._verify_seller(soup)
    status = "PASS" if result else "FAIL"
    if not result:
        all_passed = False
    print(f"  [{status}] Normalized seller (ninja_node_dc vs ninjanodedc) -> {result} (expected: True)")

    print(f"\n  Result: {'ALL PASSED' if all_passed else 'SOME FAILED'}")
    return all_passed


def test_seller_name_extraction():
    """Test seller name extraction from product pages."""
    print("\n" + "=" * 60)
    print("TEST: Seller Name Extraction")
    print("=" * 60)

    # Test JSON-LD extraction
    html_jsonld = """
    <html>
    <head>
        <script type="application/ld+json">
        {
            "@context": "https://schema.org",
            "@type": "Product",
            "offers": {
                "seller": {
                    "name": "test_seller"
                }
            }
        }
        </script>
    </head>
    <body></body>
    </html>
    """

    # Test CSS selector extraction
    html_css = """
    <html>
    <body>
        <div class="si-cnt">
            <a href="/usr/test_seller">test_seller</a>
        </div>
    </body>
    </html>
    """

    # Test spec table extraction
    html_table = """
    <html>
    <body>
        <table>
            <tr><th>Seller</th><td>table_seller</td></tr>
        </table>
    </body>
    </html>
    """

    all_passed = True

    scraper = DetailScraper()

    # Test JSON-LD
    soup = BeautifulSoup(html_jsonld, "lxml")
    result = scraper._extract_seller_name(soup)
    status = "PASS" if result == "test_seller" else "FAIL"
    if result != "test_seller":
        all_passed = False
    print(f"  [{status}] JSON-LD extraction -> {result} (expected: test_seller)")

    # Test CSS selector
    soup = BeautifulSoup(html_css, "lxml")
    result = scraper._extract_seller_name(soup)
    status = "PASS" if result == "test_seller" else "FAIL"
    if result != "test_seller":
        all_passed = False
    print(f"  [{status}] CSS selector extraction -> {result} (expected: test_seller)")

    # Test spec table
    soup = BeautifulSoup(html_table, "lxml")
    result = scraper._extract_seller_name(soup)
    status = "PASS" if result == "table_seller" else "FAIL"
    if result != "table_seller":
        all_passed = False
    print(f"  [{status}] Spec table extraction -> {result} (expected: table_seller)")

    print(f"\n  Result: {'ALL PASSED' if all_passed else 'SOME FAILED'}")
    return all_passed


def test_parse_page_logic():
    """Test that _parse_page no longer has the catch-all strategy."""
    print("\n" + "=" * 60)
    print("TEST: _parse_page() Logic (Catch-all Removed)")
    print("=" * 60)

    # Read the source code and verify the catch-all is removed
    import inspect
    source = inspect.getsource(ListingScraper._parse_page)

    all_passed = True

    # Check that catch-all is removed
    if "all_links" in source or "catch-all" in source.lower():
        print("  [FAIL] Catch-all strategy still present in _parse_page()")
        all_passed = False
    else:
        print("  [PASS] Catch-all strategy removed from _parse_page()")

    # Check that search selector is only used as fallback
    if "if not items:" in source:
        print("  [PASS] Search selector only used as fallback when store cards find nothing")
    else:
        print("  [FAIL] Search selector fallback logic not found")
        all_passed = False

    # Check that _is_in_main_content is used in extraction methods
    card_source = inspect.getsource(ListingScraper._extract_from_card)
    search_source = inspect.getsource(ListingScraper._extract_from_search_item)
    if "_is_in_main_content" in card_source and "_is_in_main_content" in search_source:
        print("  [PASS] Main content area check is used in extraction methods")
    else:
        print("  [FAIL] Main content area check not found in extraction methods")
        all_passed = False

    print(f"\n  Result: {'ALL PASSED' if all_passed else 'SOME FAILED'}")
    return all_passed


def test_extract_from_card_validation():
    """Test that _extract_from_card uses URL validation."""
    print("\n" + "=" * 60)
    print("TEST: _extract_from_card() URL Validation")
    print("=" * 60)

    import inspect
    source = inspect.getsource(ListingScraper._extract_from_card)

    all_passed = True

    if "is_valid_product_url" in source:
        print("  [PASS] URL validation is used in _extract_from_card()")
    else:
        print("  [FAIL] URL validation not found in _extract_from_card()")
        all_passed = False

    if "_is_in_main_content" in source:
        print("  [PASS] Main content check is used in _extract_from_card()")
    else:
        print("  [FAIL] Main content check not found in _extract_from_card()")
        all_passed = False

    print(f"\n  Result: {'ALL PASSED' if all_passed else 'SOME FAILED'}")
    return all_passed


def test_extract_from_search_item_validation():
    """Test that _extract_from_search_item uses URL validation."""
    print("\n" + "=" * 60)
    print("TEST: _extract_from_search_item() URL Validation")
    print("=" * 60)

    import inspect
    source = inspect.getsource(ListingScraper._extract_from_search_item)

    all_passed = True

    if "is_valid_product_url" in source:
        print("  [PASS] URL validation is used in _extract_from_search_item()")
    else:
        print("  [FAIL] URL validation not found in _extract_from_search_item()")
        all_passed = False

    if "_is_in_main_content" in source:
        print("  [PASS] Main content check is used in _extract_from_search_item()")
    else:
        print("  [FAIL] Main content check not found in _extract_from_search_item()")
        all_passed = False

    print(f"\n  Result: {'ALL PASSED' if all_passed else 'SOME FAILED'}")
    return all_passed


def test_detail_scraper_seller_filter():
    """Test that DetailScraper filters out products from other sellers."""
    print("\n" + "=" * 60)
    print("TEST: DetailScraper Seller Filter")
    print("=" * 60)

    import inspect
    source = inspect.getsource(DetailScraper.scrape)

    all_passed = True

    if "_verify_seller" in source:
        print("  [PASS] Seller verification is used in scrape()")
    else:
        print("  [FAIL] Seller verification not found in scrape()")
        all_passed = False

    if "_seller_mismatch" in source:
        print("  [PASS] Seller mismatch flag is set")
    else:
        print("  [FAIL] Seller mismatch flag not found")
        all_passed = False

    print(f"\n  Result: {'ALL PASSED' if all_passed else 'SOME FAILED'}")
    return all_passed


def main():
    """Run all tests."""
    print("\n" + "=" * 60)
    print("eBAY SCRAPER FIX VERIFICATION TESTS")
    print("=" * 60 + "\n")

    results = []

    results.append(("URL Validation", test_is_valid_product_url()))
    results.append(("Item ID Extraction", test_extract_item_id()))
    results.append(("URL Cleaning", test_clean_url()))
    results.append(("Seller Verification", test_seller_verification()))
    results.append(("Seller Name Extraction", test_seller_name_extraction()))
    results.append(("_parse_page() Logic", test_parse_page_logic()))
    results.append(("_extract_from_card() Validation", test_extract_from_card_validation()))
    results.append(("_extract_from_search_item() Validation", test_extract_from_search_item_validation()))
    results.append(("DetailScraper Seller Filter", test_detail_scraper_seller_filter()))

    print("\n" + "=" * 60)
    print("SUMMARY")
    print("=" * 60)

    all_passed = True
    for name, passed in results:
        status = "PASS" if passed else "FAIL"
        if not passed:
            all_passed = False
        print(f"  [{status}] {name}")

    print("\n" + "=" * 60)
    if all_passed:
        print("ALL TESTS PASSED!")
    else:
        print("SOME TESTS FAILED!")
    print("=" * 60)

    return 0 if all_passed else 1


if __name__ == "__main__":
    sys.exit(main())
