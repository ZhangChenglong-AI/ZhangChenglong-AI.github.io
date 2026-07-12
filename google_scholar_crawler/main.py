import json
import os
import time
from datetime import datetime, timezone
from pathlib import Path

from scholarly import scholarly


MAX_ATTEMPTS = 3
RETRY_DELAYS_SECONDS = (20, 60)


def fetch_author(scholar_id: str) -> dict:
    """Fetch an author profile, retrying transient Scholar failures."""
    last_error = None
    for attempt in range(1, MAX_ATTEMPTS + 1):
        try:
            author = scholarly.search_author_id(scholar_id)
            scholarly.fill(
                author,
                sections=["basics", "indices", "counts", "publications"],
            )
            return author
        except Exception as error:  # Scholar may raise several transient errors.
            last_error = error
            if attempt == MAX_ATTEMPTS:
                break
            delay = RETRY_DELAYS_SECONDS[attempt - 1]
            print(
                f"Scholar fetch attempt {attempt}/{MAX_ATTEMPTS} failed: "
                f"{error}. Retrying in {delay} seconds...",
                flush=True,
            )
            time.sleep(delay)
    raise RuntimeError(
        f"Google Scholar fetch failed after {MAX_ATTEMPTS} attempts"
    ) from last_error


def validate_citations(current: int, previous_file: str | None) -> None:
    """Reject invalid values and unexpectedly large citation decreases."""
    if not isinstance(current, int) or current < 0:
        raise ValueError(f"Invalid citation count returned by Scholar: {current!r}")

    if not previous_file:
        return

    path = Path(previous_file)
    if not path.is_file():
        print("Previous citation data is unavailable; skipping decrease check.")
        return

    try:
        previous = json.loads(path.read_text(encoding="utf-8")).get("citedby")
    except (OSError, json.JSONDecodeError) as error:
        print(f"Could not read previous citation data: {error}")
        return

    if not isinstance(previous, int) or previous < 0:
        print("Previous citation count is invalid; skipping decrease check.")
        return

    allowed_drop = max(10, round(previous * 0.10))
    if current < previous - allowed_drop:
        raise ValueError(
            f"Citation count dropped unexpectedly from {previous} to {current}; "
            "keeping the previously published data."
        )


scholar_id = os.environ["GOOGLE_SCHOLAR_ID"].strip().split("&", 1)[0]
if not scholar_id:
    raise ValueError("GOOGLE_SCHOLAR_ID is empty")

author = fetch_author(scholar_id)
citations = author.get("citedby")
validate_citations(citations, os.environ.get("PREVIOUS_STATS_FILE"))

author["updated"] = datetime.now(timezone.utc).isoformat()
author["publications"] = {
    publication["author_pub_id"]: publication
    for publication in author.get("publications", [])
    if publication.get("author_pub_id")
}
print(json.dumps(author, indent=2, ensure_ascii=False))

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
