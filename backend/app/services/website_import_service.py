import logging
"""
Orchestrates spec §7-8's full importer flow:
  URL -> SSRF-safe fetch -> relevant page discovery -> extraction ->
  structured business info -> stored as a DRAFT for customer review
  (never auto-approved into knowledge_items).
"""
from dataclasses import dataclass

from app.core.llm_extraction import LLMExtractionError, extract_business_info
from app.core.ssrf_safe_fetch import FetchTooLargeError, SSRFBlockedError, safe_fetch
from app.core.website_extraction import ExtractedPage, parse_html, rank_links_by_relevance

logger = logging.getLogger("atla.import")

MAX_PAGES_CRAWLED = 5  # spec §8: "crawl limits"


@dataclass
class ImportResult:
    pages_fetched: list[str]
    structured_info: dict
    raw_snapshot: dict  # exactly what we found, pre-edit — stored on Business.raw_import_snapshot


class WebsiteImportError(Exception):
    """str(e) is ALWAYS customer-safe. Internal detail is logged, never put in the message."""


MSG_UNREACHABLE = "We couldn't reach that website. Check the address, or enter your details manually instead."
MSG_TOO_LARGE = "That page is too large to import automatically. You can enter your details manually instead."
MSG_UNREADABLE = "We reached your website but couldn't read it automatically. Please try again, or enter your details manually."


def import_website(start_url: str) -> ImportResult:
    pages: list[ExtractedPage] = []
    fetched_urls: set[str] = set()

    try:
        first = safe_fetch(start_url)
    except SSRFBlockedError as e:
        logger.warning("Import blocked for %s: %s", start_url, e)
        raise WebsiteImportError(MSG_UNREACHABLE) from e
    except FetchTooLargeError as e:
        logger.warning("Import too large for %s: %s", start_url, e)
        raise WebsiteImportError(MSG_TOO_LARGE) from e

    first_page = parse_html(first.text, first.final_url)
    pages.append(first_page)
    fetched_urls.add(first.final_url)

    # Crawl a small number of the most relevant same-domain pages
    # (about/contact/services/faq/hours) — bounded by MAX_PAGES_CRAWLED,
    # each individually still going through safe_fetch (so a linked page
    # redirecting somewhere private is caught exactly like the first one).
    candidates = rank_links_by_relevance(first_page.internal_links)
    for link in candidates:
        if len(pages) >= MAX_PAGES_CRAWLED:
            break
        if link in fetched_urls:
            continue
        try:
            result = safe_fetch(link)
        except (SSRFBlockedError, FetchTooLargeError):
            continue  # a single bad linked page doesn't fail the whole import
        pages.append(parse_html(result.text, result.final_url))
        fetched_urls.add(result.final_url)

    combined_text = "\n\n---PAGE BREAK---\n\n".join(f"URL: {p.url}\n{p.text}" for p in pages)
    json_ld_hints = [p.json_ld_business for p in pages if p.json_ld_business]

    raw_snapshot = {
        "pages": [
            {"url": p.url, "title": p.title, "meta_description": p.meta_description, "json_ld": p.json_ld_business}
            for p in pages
        ],
    }

    try:
        structured_info = extract_business_info(combined_text, json_ld_hints)
    except Exception as e:  # noqa: BLE001 — LLMExtractionError, network, timeouts: all internal
        logger.error("Business extraction failed for %s: %s", start_url, e)
        raise WebsiteImportError(MSG_UNREADABLE) from e

    return ImportResult(
        pages_fetched=list(fetched_urls),
        structured_info=structured_info,
        raw_snapshot=raw_snapshot,
    )
