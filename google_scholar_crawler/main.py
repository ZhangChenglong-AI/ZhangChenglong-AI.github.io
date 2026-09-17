import json
import os
import re
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
SERPAPI_ENDPOINT = "https://serpapi.com/search"


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


def parse_serpapi_number(value, label: str) -> int:
    if isinstance(value, bool):
        raise ValueError(f"Invalid {label} returned by SerpApi: {value!r}")
    if isinstance(value, int):
        if value < 0:
            raise ValueError(f"Invalid {label} returned by SerpApi: {value!r}")
        return value

    text = str(value).replace(",", "").strip()
    match = re.fullmatch(r"\d+", text)
    if not match:
        raise ValueError(f"Invalid {label} returned by SerpApi: {value!r}")
    return int(text)


def parse_serpapi_metrics(payload: dict) -> dict:
    metrics = {}
    for item in payload.get("cited_by", {}).get("table", []):
        if not isinstance(item, dict):
            continue
        for metric_name, values in item.items():
            if not isinstance(values, dict):
                continue
            metrics[metric_name] = values

    field_map = {
        "citations": ("citedby", "citedby5y"),
        "h_index": ("hindex", "hindex5y"),
        "i10_index": ("i10index", "i10index5y"),
    }
    result = {}
    for metric_name, fields in field_map.items():
        values = metrics.get(metric_name, {})
        for key, field in zip(("all", "since_2016"), fields):
            if key in values:
                result[field] = parse_serpapi_number(
                    values[key], f"{metric_name} {key}"
                )

    if "citedby" not in result:
        raise ValueError("SerpApi response did not contain an all-time citation count")
    return result


def parse_serpapi_interests(value):
    if not isinstance(value, list):
        return []

    interests = []
    for item in value:
        if isinstance(item, str):
            interests.append(item)
        elif isinstance(item, dict) and item.get("title"):
            interests.append(str(item["title"]))
    return interests


def parse_serpapi_article_count(value) -> int:
    if isinstance(value, dict):
        value = value.get("total", 0)
    if isinstance(value, str):
        match = re.search(r"\d[\d,]*", value)
        value = match.group(0) if match else "0"
    return parse_serpapi_number(value or 0, "paper citation count")


def parse_serpapi_author(payload: dict, scholar_id: str, previous: dict) -> dict:
    if not isinstance(payload, dict):
        raise ValueError("SerpApi returned an invalid JSON object")
    if payload.get("error"):
        raise RuntimeError("SerpApi returned an API error")

    author_info = payload.get("author", {})
    if not isinstance(author_info, dict):
        author_info = {}

    author = dict(previous)
    author.update(
        {
            "container_type": "Author",
            "scholar_id": scholar_id,
            "source": "SERPAPI_GOOGLE_SCHOLAR_AUTHOR_API",
            **parse_serpapi_metrics(payload),
        }
    )

    for source_key, target_key in (
        ("name", "name"),
        ("affiliations", "affiliation"),
        ("homepage", "homepage"),
        ("email", "email"),
    ):
        value = author_info.get(source_key)
        if value:
            if source_key == "affiliations" and isinstance(value, list):
                value = ", ".join(str(item) for item in value)
            author[target_key] = value

    interests = parse_serpapi_interests(author_info.get("interests"))
    if interests:
        author["interests"] = interests

    publications = dict(previous.get("publications", {}))
    articles = payload.get("articles", [])
    if not isinstance(articles, list):
        articles = []

    for index, article in enumerate(articles):
        if not isinstance(article, dict):
            continue

        author_pub_id = (
            article.get("citation_id")
            or article.get("result_id")
            or article.get("link")
            or f"serpapi-{index}"
        )
        author_pub_id = str(author_pub_id)
        publication = dict(publications.get(author_pub_id, {}))
        bibliography = dict(publication.get("bib", {}))

        for source_key, target_key in (
            ("title", "title"),
            ("authors", "author"),
            ("publication", "citation"),
            ("year", "pub_year"),
        ):
            value = article.get(source_key)
            if value:
                bibliography[target_key] = str(value)

        publication.update(
            {
                "container_type": "Publication",
                "source": "AUTHOR_PUBLICATION_ENTRY",
                "bib": bibliography,
                "filled": False,
                "author_pub_id": author_pub_id,
                "num_citations": parse_serpapi_article_count(
                    article.get("cited_by", 0)
                ),
                "url_scholarbib": article.get("link", ""),
            }
        )
        cited_by = article.get("cited_by")
        if isinstance(cited_by, dict) and cited_by.get("link"):
            publication["citedby_url"] = cited_by["link"]
        publications[author_pub_id] = publication

    author["publications"] = publications
    return author


def fetch_author_via_serpapi(
    scholar_id: str, serpapi_key: str, previous: dict
) -> dict:
    try:
        response = requests.get(
            SERPAPI_ENDPOINT,
            params={
                "engine": "google_scholar_author",
                "author_id": scholar_id,
                "hl": "en",
                "num": 100,
                "api_key": serpapi_key,
            },
            headers={"User-Agent": USER_AGENT},
            timeout=REQUEST_TIMEOUT_SECONDS,
        )
    except requests.RequestException as error:
        raise RuntimeError(
            f"SerpApi request failed: {error.__class__.__name__}"
        ) from error

    if response.status_code >= 400:
        raise RuntimeError(f"SerpApi request failed with HTTP {response.status_code}")

    try:
        payload = response.json()
    except ValueError as error:
        raise RuntimeError("SerpApi returned invalid JSON") from error

    author = parse_serpapi_author(payload, scholar_id, previous)
    print("Fetched Google Scholar profile through SerpApi.", flush=True)
    return author


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


def main() -> None:
    scholar_id = os.environ.get("GOOGLE_SCHOLAR_ID", "").strip().split("&", 1)[0]
    if not scholar_id:
        raise ValueError(
            "GOOGLE_SCHOLAR_ID secret is missing or empty; "
            "configure it in the repository Actions secrets"
        )

    previous_data = load_previous_data(os.environ.get("PREVIOUS_STATS_FILE"))
    serpapi_key = os.environ.get("SERPAPI_KEY", "").strip()
    if serpapi_key:
        author = fetch_author_via_serpapi(scholar_id, serpapi_key, previous_data)
    else:
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


if __name__ == "__main__":
    main()
