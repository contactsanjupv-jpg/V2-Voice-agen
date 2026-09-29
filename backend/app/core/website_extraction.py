"""
Pure HTML parsing — no network calls here (those go through
app/core/ssrf_safe_fetch.py). Splits into two tiers deliberately:

  1. STRUCTURED data (schema.org JSON-LD `LocalBusiness`/`Organization`
     blocks, <title>, meta description) — reliable when present, used
     as-is, no LLM guessing needed for these fields.
  2. Free text — handed to the LLM extraction step
     (app/core/llm_extraction.py) for the genuinely semantic fields
     (services, FAQs, policy summaries) that don't have a structured
     source most small-business sites bother to mark up.
"""
import json
import re
from dataclasses import dataclass, field
from urllib.parse import urljoin, urlparse

from bs4 import BeautifulSoup

MAX_TEXT_CHARS = 15_000  # keeps the LLM extraction prompt bounded and cheap


@dataclass
class ExtractedPage:
    url: str
    title: str | None
    meta_description: str | None
    text: str
    json_ld_business: dict | None
    internal_links: list[str] = field(default_factory=list)


def parse_html(html: str, page_url: str) -> ExtractedPage:
    soup = BeautifulSoup(html, "lxml")

    for tag in soup(["script", "style", "noscript", "svg"]):
        tag.decompose()

    title = soup.title.string.strip() if soup.title and soup.title.string else None

    meta_desc_tag = soup.find("meta", attrs={"name": "description"})
    meta_description = meta_desc_tag.get("content", "").strip() if meta_desc_tag else None

    json_ld_business = _find_local_business_json_ld(soup)

    text = _visible_text(soup)[:MAX_TEXT_CHARS]

    parsed_page_url = urlparse(page_url)
    internal_links = _same_domain_links(soup, page_url, parsed_page_url.netloc)

    return ExtractedPage(
        url=page_url,
        title=title,
        meta_description=meta_description,
        text=text,
        json_ld_business=json_ld_business,
        internal_links=internal_links,
    )


def _visible_text(soup: BeautifulSoup) -> str:
    raw = soup.get_text(separator="\n")
    lines = [line.strip() for line in raw.splitlines()]
    return "\n".join(line for line in lines if line)


def _find_local_business_json_ld(soup: BeautifulSoup) -> dict | None:
    """Looks for schema.org structured data — a real, common convention for
    small-business sites (address/hours/phone). Not every site has it;
    None is a normal, expected result."""
    business_types = {
        "LocalBusiness", "Organization", "Restaurant", "Dentist", "MedicalBusiness",
        "AutoRepair", "BeautySalon", "HealthAndBeautyBusiness", "HomeAndConstructionBusiness",
        "LegalService", "ProfessionalService",
    }
    for script in soup.find_all("script", attrs={"type": "application/ld+json"}):
        try:
            data = json.loads(script.string or "")
        except (json.JSONDecodeError, TypeError):
            continue
        candidates = data if isinstance(data, list) else [data]
        for candidate in candidates:
            if not isinstance(candidate, dict):
                continue
            at_type = candidate.get("@type")
            types = at_type if isinstance(at_type, list) else [at_type]
            if business_types.intersection(types):
                return candidate
    return None


_SKIP_LINK_PATTERNS = re.compile(r"\.(pdf|jpg|jpeg|png|gif|svg|css|js|zip|mp4)$", re.IGNORECASE)


def _same_domain_links(soup: BeautifulSoup, page_url: str, domain: str) -> list[str]:
    links: list[str] = []
    seen: set[str] = set()
    for a in soup.find_all("a", href=True):
        href = a["href"].strip()
        if not href or href.startswith(("#", "mailto:", "tel:", "javascript:")):
            continue
        absolute = urljoin(page_url, href)
        parsed = urlparse(absolute)
        if parsed.netloc != domain:
            continue
        if _SKIP_LINK_PATTERNS.search(parsed.path):
            continue
        clean = absolute.split("#")[0]
        if clean not in seen:
            seen.add(clean)
            links.append(clean)
    return links


# Pages worth prioritizing when picking which internal links to crawl next,
# within the importer's crawl-page limit — a small business's useful
# content is disproportionately likely to live at these paths.
PRIORITY_PATH_HINTS = ("about", "contact", "service", "faq", "hours", "location", "pricing", "booking")


def rank_links_by_relevance(links: list[str]) -> list[str]:
    def score(url: str) -> int:
        path = urlparse(url).path.lower()
        return sum(1 for hint in PRIORITY_PATH_HINTS if hint in path)

    return sorted(links, key=score, reverse=True)
