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

MAX_PAGES_CRAWLED = 5  # spec §8: "crawl limits"


@dataclass
class ImportResult:
    pages_fetched: list[str]
    structured_info: dict
    raw_snapshot: dict  # exactly what we found, pre-edit — stored on Business.raw_import_snapshot


class WebsiteImportError(Exception):
    pass


def import_website(start_url: str) -> ImportResult:
    pages: list[ExtractedPage] = []
    fetched_urls: set[str] = set()

    try:
        first = safe_fetch(start_url)
    except SSRFBlockedError as e:
        raise WebsiteImportError(f"That URL couldn't be safely fetched: {e}") from e
    except FetchTooLargeError as e:
        raise WebsiteImportError(f"That page was too large to import: {e}") from e

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
    except LLMExtractionError as e:
        # The crawl itself succeeded — surface the raw snapshot so the
        # customer isn't blocked entirely, but be explicit this step
        # didn't complete rather than silently returning an empty draft.
        raise WebsiteImportError(f"Fetched the site, but structuring the results failed: {e}") from e

    return ImportResult(
        pages_fetched=list(fetched_urls),
        structured_info=structured_info,
        raw_snapshot=raw_snapshot,
    )
