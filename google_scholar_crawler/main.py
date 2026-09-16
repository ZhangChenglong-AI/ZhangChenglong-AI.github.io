import json
import os
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import parse_qs, urljoin, urlparse

import requests
from bs4 import BeautifulSoup


SCHOLAR_HOSTS = (
    "scholar.google.co.uk",
    "scholar.google.de",
    "scholar.google.com",
)
REQUEST_TIMEOUT_SECONDS = (10, 25)
USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/140.0.0.0 Safari/537.36"
)


def load_previous_data(previous_file: str | None) -> dict:
    if not previous_file:
        return {}

    path = Path(previous_file)
    if not path.is_file():
        print("Previous citation data is unavailable; starting with a fresh profile.")
        return {}

    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as error:
        print(f"Could not read previous citation data: {error}")
        return {}

    return data if isinstance(data, dict) else {}


def parse_count(value: str, label: str) -> int:
    normalized = value.replace(",", "").strip()
    if not normalized.isdigit():
        raise ValueError(f"Invalid {label} returned by Scholar: {value!r}")
    return int(normalized)


def parse_profile(
    html: str,
    scholar_id: str,
    base_url: str,
    previous: dict,
) -> dict:
    soup = BeautifulSoup(html, "html.parser")
    if soup.select_one("#gs_captcha_ccl") or "automated queries" in soup.get_text(
        " ", strip=True
    ).lower():
        raise RuntimeError("Google Scholar returned an automated-query block page")

    metrics = {}
    for row in soup.select("#gsc_rsb_st tr"):
        label_element = row.select_one(".gsc_rsb_sth, .gsc_rsb_sc1")
        values = row.select(".gsc_rsb_std")
        if label_element and values:
            metrics[label_element.get_text(" ", strip=True).lower()] = [
                parse_count(value.get_text(" ", strip=True), label_element.get_text())
                for value in values
            ]

    citation_values = metrics.get("citations")
    if not citation_values:
        raise ValueError("Scholar profile did not contain a citation summary")

    author = dict(previous)
    author.update(
        {
            "container_type": "Author",
            "scholar_id": scholar_id,
            "source": "AUTHOR_PROFILE_PAGE",
            "citedby": citation_values[0],
        }
    )

    name = soup.select_one("#gsc_prf_in")
    if name:
        author["name"] = name.get_text(" ", strip=True)

    profile_lines = soup.select("#gsc_prf_i .gsc_prf_il")
    if profile_lines:
        author["affiliation"] = profile_lines[0].get_text(" ", strip=True)

    interests = [
        item.get_text(" ", strip=True) for item in soup.select("#gsc_prf_int a")
    ]
    if interests:
        author["interests"] = interests

    picture = soup.select_one("#gsc_prf_pup-img")
    if picture and picture.get("src"):
        author["url_picture"] = urljoin(base_url, picture["src"])

    metric_fields = {
        "citations": ("citedby", "citedby5y"),
        "h-index": ("hindex", "hindex5y"),
        "i10-index": ("i10index", "i10index5y"),
    }
    for label, fields in metric_fields.items():
        values = metrics.get(label, [])
        for field, value in zip(fields, values):
            author[field] = value

    publications = dict(previous.get("publications", {}))
    for row in soup.select(".gsc_a_tr"):
        title_link = row.select_one(".gsc_a_at")
        if not title_link or not title_link.get("href"):
            continue

        publication_url = urljoin(base_url, title_link["href"])
        query = parse_qs(urlparse(publication_url).query)
        author_pub_id = query.get("citation_for_view", [""])[0]
        if not author_pub_id:
            continue

        publication = dict(publications.get(author_pub_id, {}))
        bibliography = dict(publication.get("bib", {}))
        bibliography["title"] = title_link.get_text(" ", strip=True)

        details = row.select(".gs_gray")
        if details:
            bibliography["author"] = details[0].get_text(" ", strip=True)
        if len(details) > 1:
            bibliography["citation"] = details[1].get_text(" ", strip=True)

        year = row.select_one(".gsc_a_y span")
        if year and year.get_text(strip=True):
            bibliography["pub_year"] = year.get_text(strip=True)

        citation_link = row.select_one(".gsc_a_ac")
        citation_text = citation_link.get_text(strip=True) if citation_link else ""
        publication.update(
            {
                "container_type": "Publication",
                "source": "AUTHOR_PUBLICATION_ENTRY",
                "bib": bibliography,
                "filled": False,
                "author_pub_id": author_pub_id,
                "num_citations": parse_count(citation_text or "0", "paper citation count"),
                "url_scholarbib": publication_url,
            }
        )
        if citation_link and citation_link.get("href"):
            publication["citedby_url"] = urljoin(base_url, citation_link["href"])
        publications[author_pub_id] = publication

    author["publications"] = publications
    return author


def fetch_author(scholar_id: str, previous: dict) -> dict:
    errors = []
    session = requests.Session()
    session.headers.update({"User-Agent": USER_AGENT, "Accept-Language": "en-US,en;q=0.9"})

    for host in SCHOLAR_HOSTS:
        base_url = f"https://{host}"
        try:
            response = session.get(
                f"{base_url}/citations",
                params={"user": scholar_id, "hl": "en", "pagesize": 100},
                timeout=REQUEST_TIMEOUT_SECONDS,
            )
            response.raise_for_status()
            author = parse_profile(response.text, scholar_id, base_url, previous)
            print(f"Fetched Google Scholar profile from {host}.", flush=True)
            return author
        except (requests.RequestException, RuntimeError, ValueError) as error:
            errors.append(f"{host}: {error}")
            print(f"Scholar host {host} failed: {error}", flush=True)

    raise RuntimeError("Google Scholar fetch failed: " + "; ".join(errors))


def validate_citations(current: int, previous: dict) -> None:
    if not isinstance(current, int) or current < 0:
        raise ValueError(f"Invalid citation count returned by Scholar: {current!r}")

    previous_count = previous.get("citedby")
    if not isinstance(previous_count, int) or previous_count < 0:
        return

    allowed_drop = max(10, round(previous_count * 0.10))
    if current < previous_count - allowed_drop:
        raise ValueError(
            f"Citation count dropped unexpectedly from {previous_count} to {current}; "
            "keeping the previously published data."
        )


scholar_id = os.environ.get("GOOGLE_SCHOLAR_ID", "").strip().split("&", 1)[0]
if not scholar_id:
    raise ValueError(
        "GOOGLE_SCHOLAR_ID secret is missing or empty; "
        "configure it in the repository Actions secrets"
    )

previous_data = load_previous_data(os.environ.get("PREVIOUS_STATS_FILE"))
author = fetch_author(scholar_id, previous_data)
citations = author.get("citedby")
validate_citations(citations, previous_data)

author["updated"] = datetime.now(timezone.utc).isoformat()
print(
    "Prepared Scholar data: "
    f"citations={citations}, "
    f"publications={len(author.get('publications', {}))}, "
    f"updated={author['updated']}",
    flush=True,
)

results_dir = Path("results")
results_dir.mkdir(exist_ok=True)
(results_dir / "gs_data.json").write_text(
    json.dumps(author, ensure_ascii=False), encoding="utf-8"
)

shieldio_data = {
    "schemaVersion": 1,
    "label": "citations",
    "message": str(citations),
}
(results_dir / "gs_data_shieldsio.json").write_text(
    json.dumps(shieldio_data, ensure_ascii=False), encoding="utf-8"
)
