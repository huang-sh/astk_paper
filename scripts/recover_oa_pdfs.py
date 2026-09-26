#!/usr/bin/env python3
"""Recover additional legally open-access PDFs after the primary literature pass.

The script queries Unpaywall, Europe PMC, and the NCBI PMC Open Access package
service. It downloads only URLs identified as open-access locations and does not
bypass paywalls, authentication, access controls, or publisher restrictions.
"""
from __future__ import annotations

import csv
import json
import re
import sys
import time
import unicodedata
import xml.etree.ElementTree as ET
from pathlib import Path
from typing import Any, Iterable
from urllib.parse import quote

import requests

TIMEOUT = 30
MAX_PDF_BYTES = 150 * 1024 * 1024
EMAIL = "hsh-me@outlook.com"
HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/128.0 Safari/537.36 "
        "PhD-literature-bundler/1.1"
    ),
    "From": EMAIL,
    "Accept-Encoding": "gzip, deflate",
}


def safe_filename(text: str, max_len: int = 105) -> str:
    text = unicodedata.normalize("NFKD", text)
    text = "".join(ch for ch in text if not unicodedata.combining(ch))
    text = re.sub(r"[^A-Za-z0-9._-]+", "_", text).strip("._-")
    text = re.sub(r"_+", "_", text)
    return (text[:max_len] or "paper").rstrip("._-")


def request(session: requests.Session, url: str, *, params: dict[str, Any] | None = None,
            stream: bool = False, accept: str | None = None, attempts: int = 3) -> requests.Response | None:
    headers = {"Accept": accept} if accept else None
    for attempt in range(attempts):
        try:
            response = session.get(
                url,
                params=params,
                headers=headers,
                timeout=TIMEOUT,
                stream=stream,
                allow_redirects=True,
            )
            if response.status_code == 429 or 500 <= response.status_code < 600:
                response.close()
                time.sleep(min(12, 1.5 * (2 ** attempt)))
                continue
            return response
        except requests.RequestException:
            if attempt + 1 == attempts:
                return None
            time.sleep(min(12, 1.5 * (2 ** attempt)))
    return None


def get_json(session: requests.Session, url: str, *, params: dict[str, Any] | None = None) -> dict[str, Any] | None:
    response = request(session, url, params=params, accept="application/json")
    if response is None:
        return None
    try:
        if response.status_code != 200:
            return None
        return response.json()
    except (ValueError, requests.RequestException):
        return None
    finally:
        response.close()


def add_candidate(candidates: list[tuple[str, str]], url: str | None, source: str) -> None:
    if not url or not url.startswith(("http://", "https://")):
        return
    if url.startswith("http://"):
        url = "https://" + url[len("http://"):]
    if all(existing != url for existing, _ in candidates):
        candidates.append((url, source))


def unpaywall_candidates(session: requests.Session, doi: str) -> tuple[list[tuple[str, str]], str]:
    candidates: list[tuple[str, str]] = []
    if not doi:
        return candidates, ""
    data = get_json(
        session,
        f"https://api.unpaywall.org/v2/{quote(doi, safe='/')}",
        params={"email": EMAIL},
    )
    if not data or not data.get("is_oa"):
        return candidates, ""
    landing = ""
    locations: list[dict[str, Any]] = []
    if isinstance(data.get("best_oa_location"), dict):
        locations.append(data["best_oa_location"])
    locations.extend(loc for loc in (data.get("oa_locations") or []) if isinstance(loc, dict))
    for loc in locations:
        if not landing and loc.get("url"):
            landing = str(loc["url"])
        add_candidate(candidates, loc.get("url_for_pdf"), "Unpaywall OA location")
    return candidates, landing


def europe_pmc_record(session: requests.Session, doi: str, title: str) -> dict[str, Any] | None:
    query = f'DOI:"{doi}"' if doi else f'TITLE:"{title}"'
    data = get_json(
        session,
        "https://www.ebi.ac.uk/europepmc/webservices/rest/search",
        params={"query": query, "format": "json", "pageSize": 10, "resultType": "core"},
    )
    records = (((data or {}).get("resultList") or {}).get("result") or [])
    if not records:
        return None
    # DOI searches are normally unique. Prefer a PMCID-bearing record.
    records.sort(key=lambda item: bool(item.get("pmcid")), reverse=True)
    return records[0]


def pmc_oa_pdf(session: requests.Session, pmcid: str) -> str:
    if not pmcid:
        return ""
    response = request(
        session,
        "https://www.ncbi.nlm.nih.gov/pmc/utils/oa/oa.fcgi",
        params={"id": pmcid},
        accept="application/xml,text/xml;q=0.9,*/*;q=0.1",
    )
    if response is None:
        return ""
    try:
        if response.status_code != 200:
            return ""
        root = ET.fromstring(response.content)
        for link in root.findall(".//link"):
            if str(link.attrib.get("format") or "").lower() != "pdf":
                continue
            href = str(link.attrib.get("href") or "")
            prefix = "ftp://ftp.ncbi.nlm.nih.gov/"
            if href.startswith(prefix):
                href = "https://ftp.ncbi.nlm.nih.gov/" + href[len(prefix):]
            if href.startswith(("http://", "https://")):
                return href
    except (ET.ParseError, requests.RequestException):
        return ""
    finally:
        response.close()
    return ""


def deterministic_oa_candidates(doi: str) -> list[tuple[str, str]]:
    candidates: list[tuple[str, str]] = []
    lower = doi.lower()
    plos_map = {
        "pcbi": "ploscompbiol",
        "pone": "plosone",
        "pgen": "plosgenetics",
        "pbio": "plosbiology",
        "pmed": "plosmedicine",
        "ppat": "plospathogens",
        "pntd": "plosntds",
        "pcbi": "ploscompbiol",
    }
    match = re.match(r"10\.1371/journal\.([a-z]+)\.", lower)
    if match and match.group(1) in plos_map:
        journal = plos_map[match.group(1)]
        add_candidate(
            candidates,
            f"https://journals.plos.org/{journal}/article/file?id={doi}&type=printable",
            "PLOS OA printable PDF",
        )
    match = re.match(r"10\.7554/elife\.(\d+)", lower)
    if match:
        add_candidate(candidates, f"https://elifesciences.org/articles/{match.group(1)}.pdf", "eLife OA PDF")
    match = re.match(r"10\.7717/peerj\.(\d+)", lower)
    if match:
        add_candidate(candidates, f"https://peerj.com/articles/{match.group(1)}.pdf", "PeerJ OA PDF")
    return candidates


def download_pdf(session: requests.Session, candidates: Iterable[tuple[str, str]], destination: Path) -> tuple[bool, str, str]:
    destination.parent.mkdir(parents=True, exist_ok=True)
    errors: list[str] = []
    for url, source in candidates:
        response = request(
            session,
            url,
            stream=True,
            accept="application/pdf,text/html;q=0.4,*/*;q=0.1",
        )
        if response is None:
            errors.append(f"{source}: request failed")
            continue
        try:
            if response.status_code != 200:
                errors.append(f"{source}: HTTP {response.status_code}")
                continue
            tmp = destination.with_suffix(".part")
            total = 0
            head = b""
            with tmp.open("wb") as handle:
                for chunk in response.iter_content(chunk_size=128 * 1024):
                    if not chunk:
                        continue
                    if len(head) < 4096:
                        head += chunk[: 4096 - len(head)]
                    total += len(chunk)
                    if total > MAX_PDF_BYTES:
                        raise RuntimeError("file exceeded 150 MB limit")
                    handle.write(chunk)
            if b"%PDF-" not in head[:4096]:
                tmp.unlink(missing_ok=True)
                errors.append(f"{source}: not a PDF ({response.headers.get('Content-Type', '')})")
                continue
            if total < 1024:
                tmp.unlink(missing_ok=True)
                errors.append(f"{source}: PDF too small")
                continue
            tmp.replace(destination)
            return True, url, source
        except Exception as exc:  # noqa: BLE001
            destination.with_suffix(".part").unlink(missing_ok=True)
            errors.append(f"{source}: {exc}")
        finally:
            response.close()
    return False, "", "; ".join(errors[-6:])


def rewrite_outputs(root: Path, rows: list[dict[str, str]], recovery_log: list[str]) -> None:
    fieldnames = list(rows[0].keys()) if rows else []
    with (root / "all_references_resolved.csv").open("w", encoding="utf-8-sig", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=fieldnames)
        writer.writeheader()
        writer.writerows(rows)

    unavailable = [row for row in rows if row.get("status") != "downloaded"]
    with (root / "unavailable_title_doi.csv").open("w", encoding="utf-8-sig", newline="") as handle:
        writer = csv.writer(handle)
        writer.writerow(["id", "title", "doi", "status", "reason", "landing_url"])
        for row in unavailable:
            writer.writerow([
                row.get("id", ""),
                row.get("resolved_title") or row.get("requested_title", ""),
                row.get("doi") or "DOI not resolved",
                row.get("status", ""),
                row.get("note", ""),
                row.get("landing_url", ""),
            ])

    metadata_path = root / "resolved_metadata.json"
    if metadata_path.exists():
        metadata = json.loads(metadata_path.read_text(encoding="utf-8"))
        by_id = {str(row["id"]): row for row in rows}
        for item in metadata:
            row = by_id.get(str(item.get("id")))
            if row:
                for key in ("status", "pdf_url", "pdf_filename", "note", "landing_url", "open_access", "oa_status"):
                    item[key] = row.get(key, item.get(key))
        metadata_path.write_text(json.dumps(metadata, ensure_ascii=False, indent=2), encoding="utf-8")

    downloaded = [row for row in rows if row.get("status") == "downloaded"]
    lines = [
        "# PhD Part I literature bundle",
        "",
        f"- Requested references: **{len(rows)}**",
        f"- Open-access PDFs downloaded: **{len(downloaded)}**",
        f"- Not downloaded: **{len(unavailable)}**",
        "",
        "Only openly accessible copies were downloaded. The workflow did not bypass paywalls, authentication, robots controls, or publisher access restrictions.",
        "",
        "## Contents",
        "",
        "- `pdfs/`: downloaded PDFs grouped by thesis topic.",
        "- `all_references_resolved.csv`: complete metadata and retrieval status.",
        "- `unavailable_title_doi.csv`: title and DOI (when resolved) for every item without a downloaded PDF.",
        "- `resolved_metadata.json`: machine-readable metadata.",
        "- `download_log.txt`: primary retrieval log.",
        "- `recovery_log.txt`: Unpaywall/PMC recovery log.",
        "- `requested_references.json`: the requested 211-item bibliography.",
        "",
        "## Downloaded papers",
        "",
    ]
    for row in downloaded:
        lines.append(
            f"- {row['id']}. {row.get('resolved_title') or row.get('requested_title')} — "
            f"DOI: {row.get('doi') or 'not resolved'} — `{row.get('pdf_filename')}`"
        )
    lines.extend(["", "## Not downloaded", ""])
    for row in unavailable:
        lines.append(
            f"- {row['id']}. {row.get('resolved_title') or row.get('requested_title')} — "
            f"DOI: {row.get('doi') or 'not resolved'} — {row.get('status')}"
        )
    (root / "README.md").write_text("\n".join(lines) + "\n", encoding="utf-8")
    (root / "recovery_log.txt").write_text("\n".join(recovery_log) + "\n", encoding="utf-8")


def main() -> int:
    if len(sys.argv) != 2:
        raise SystemExit("Usage: recover_oa_pdfs.py BUNDLE_DIRECTORY")
    root = Path(sys.argv[1])
    table = root / "all_references_resolved.csv"
    with table.open(encoding="utf-8-sig", newline="") as handle:
        rows = list(csv.DictReader(handle))

    session = requests.Session()
    session.headers.update(HEADERS)
    recovered = 0
    log: list[str] = []

    for index, row in enumerate(rows, 1):
        if row.get("status") == "downloaded":
            continue
        ref_id = int(row["id"])
        doi = (row.get("doi") or "").strip().lower()
        title = row.get("resolved_title") or row.get("requested_title") or ""
        candidates: list[tuple[str, str]] = []
        landing = ""

        upw, upw_landing = unpaywall_candidates(session, doi)
        candidates.extend(upw)
        landing = upw_landing or landing

        record = europe_pmc_record(session, doi, title)
        pmcid = str((record or {}).get("pmcid") or "")
        is_pmc_oa = str((record or {}).get("isOpenAccess") or "").upper() == "Y"
        if pmcid and is_pmc_oa:
            add_candidate(candidates, pmc_oa_pdf(session, pmcid), "PMC Open Access PDF package")
            if not landing:
                landing = f"https://europepmc.org/article/PMC/{pmcid.replace('PMC', '')}"

        for candidate in deterministic_oa_candidates(doi):
            if candidate not in candidates:
                candidates.append(candidate)

        if not candidates:
            log.append(f"{ref_id}: no additional verified OA PDF location")
            continue

        year = row.get("resolved_year") or row.get("requested_year") or "n.d."
        author = safe_filename(row.get("first_author") or "Unknown", 25)
        short_title = safe_filename(title, 78)
        filename = f"{ref_id:03d}_{author}_{year}_{short_title}.pdf"
        destination = root / "pdfs" / row.get("category", "99_other") / filename
        ok, used_url, source_or_error = download_pdf(session, candidates, destination)
        if ok:
            recovered += 1
            row["status"] = "downloaded"
            row["open_access"] = "True"
            row["pdf_url"] = used_url
            row["pdf_filename"] = str(destination.relative_to(root))
            row["note"] = source_or_error + " (recovery pass)"
            if landing:
                row["landing_url"] = landing
            log.append(f"{ref_id}: recovered via {source_or_error}")
        else:
            prior = row.get("note") or ""
            row["note"] = (prior + "; recovery: " + source_or_error).strip("; ")
            log.append(f"{ref_id}: recovery failed: {source_or_error}")
        time.sleep(0.05)

    rows.sort(key=lambda row: int(row["id"]))
    rewrite_outputs(root, rows, log)
    print(f"Recovery completed: {recovered} additional PDFs; total {sum(r.get('status') == 'downloaded' for r in rows)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
