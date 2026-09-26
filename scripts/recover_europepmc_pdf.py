#!/usr/bin/env python3
"""Recover additional open-access PDFs through official Europe PMC endpoints."""
from __future__ import annotations

import csv
import json
import re
import sys
import threading
import unicodedata
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path
from typing import Any

import requests

TIMEOUT = 35
MAX_PDF_BYTES = 150 * 1024 * 1024
EMAIL = "hsh-me@outlook.com"
THREADS = 8
THREAD_LOCAL = threading.local()


def session() -> requests.Session:
    value = getattr(THREAD_LOCAL, "session", None)
    if value is None:
        value = requests.Session()
        value.headers.update({
            "User-Agent": (
                "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
                "(KHTML, like Gecko) Chrome/128.0 Safari/537.36 "
                "PhD-literature-bundler/1.2"
            ),
            "From": EMAIL,
            "Accept-Encoding": "gzip, deflate",
        })
        THREAD_LOCAL.session = value
    return value


def safe_filename(text: str, max_len: int = 105) -> str:
    text = unicodedata.normalize("NFKD", text)
    text = "".join(ch for ch in text if not unicodedata.combining(ch))
    text = re.sub(r"[^A-Za-z0-9._-]+", "_", text).strip("._-")
    text = re.sub(r"_+", "_", text)
    return (text[:max_len] or "paper").rstrip("._-")


def europe_pmc_record(doi: str, title: str) -> dict[str, Any] | None:
    query = f'DOI:"{doi}"' if doi else f'TITLE:"{title}"'
    try:
        response = session().get(
            "https://www.ebi.ac.uk/europepmc/webservices/rest/search",
            params={"query": query, "format": "json", "pageSize": 10, "resultType": "core"},
            timeout=TIMEOUT,
        )
        if response.status_code != 200:
            return None
        records = (((response.json() or {}).get("resultList") or {}).get("result") or [])
        if not records:
            return None
        records.sort(key=lambda item: (str(item.get("isOpenAccess") or "").upper() == "Y", bool(item.get("pmcid"))), reverse=True)
        return records[0]
    except (requests.RequestException, ValueError):
        return None


def try_pdf(url: str, destination: Path) -> tuple[bool, str]:
    try:
        response = session().get(
            url,
            timeout=TIMEOUT,
            stream=True,
            allow_redirects=True,
            headers={"Accept": "application/pdf,*/*;q=0.1"},
        )
        if response.status_code != 200:
            response.close()
            return False, f"HTTP {response.status_code}"
        destination.parent.mkdir(parents=True, exist_ok=True)
        tmp = destination.with_suffix(".part")
        head = b""
        total = 0
        with tmp.open("wb") as handle:
            for chunk in response.iter_content(128 * 1024):
                if not chunk:
                    continue
                if len(head) < 4096:
                    head += chunk[:4096-len(head)]
                total += len(chunk)
                if total > MAX_PDF_BYTES:
                    raise RuntimeError("file exceeded 150 MB")
                handle.write(chunk)
        response.close()
        if b"%PDF-" not in head[:4096] or total < 1024:
            tmp.unlink(missing_ok=True)
            return False, "response was not a valid PDF"
        tmp.replace(destination)
        return True, ""
    except Exception as exc:  # noqa: BLE001
        destination.with_suffix(".part").unlink(missing_ok=True)
        return False, str(exc)


def recover(row: dict[str, str], root: Path) -> tuple[int, dict[str, str] | None, str]:
    ref_id = int(row["id"])
    doi = (row.get("doi") or "").strip()
    title = row.get("resolved_title") or row.get("requested_title") or ""
    record = europe_pmc_record(doi, title)
    pmcid = str((record or {}).get("pmcid") or "")
    is_oa = str((record or {}).get("isOpenAccess") or "").upper() == "Y"
    if not pmcid or not is_oa:
        return ref_id, None, "no open-access PMCID"

    year = row.get("resolved_year") or row.get("requested_year") or "n.d."
    author = safe_filename(row.get("first_author") or "Unknown", 25)
    short_title = safe_filename(title, 78)
    filename = f"{ref_id:03d}_{author}_{year}_{short_title}.pdf"
    destination = root / "pdfs" / row.get("category", "99_other") / filename
    candidates = [
        f"https://europepmc.org/api/getPdf?pmcid={pmcid}",
        f"https://www.ebi.ac.uk/europepmc/webservices/rest/{pmcid}/fullTextPDF",
    ]
    errors = []
    for url in candidates:
        ok, error = try_pdf(url, destination)
        if ok:
            updated = dict(row)
            updated["status"] = "downloaded"
            updated["open_access"] = "True"
            updated["oa_status"] = updated.get("oa_status") or "repository"
            updated["pdf_url"] = url
            updated["pdf_filename"] = str(destination.relative_to(root))
            updated["landing_url"] = f"https://europepmc.org/article/PMC/{pmcid.replace('PMC', '')}"
            updated["note"] = ((updated.get("note") or "") + "; recovered through official Europe PMC PDF endpoint").strip("; ")
            return ref_id, updated, f"recovered {pmcid}"
        errors.append(error)
    return ref_id, None, f"{pmcid}: " + "; ".join(errors)


def rewrite(root: Path, rows: list[dict[str, str]], log: list[str]) -> None:
    fieldnames = list(rows[0].keys()) if rows else []
    with (root / "all_references_resolved.csv").open("w", encoding="utf-8-sig", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=fieldnames)
        writer.writeheader(); writer.writerows(rows)

    unavailable = [row for row in rows if row.get("status") != "downloaded"]
    with (root / "unavailable_title_doi.csv").open("w", encoding="utf-8-sig", newline="") as handle:
        writer = csv.writer(handle)
        writer.writerow(["id", "title", "doi", "status", "reason", "landing_url"])
        for row in unavailable:
            writer.writerow([
                row.get("id", ""), row.get("resolved_title") or row.get("requested_title", ""),
                row.get("doi") or "DOI not resolved", row.get("status", ""),
                row.get("note", ""), row.get("landing_url", ""),
            ])

    metadata_path = root / "resolved_metadata.json"
    if metadata_path.exists():
        metadata = json.loads(metadata_path.read_text(encoding="utf-8"))
        by_id = {str(row["id"]): row for row in rows}
        for item in metadata:
            row = by_id.get(str(item.get("id")))
            if row:
                for key in ("status", "pdf_url", "pdf_filename", "note", "landing_url", "open_access", "oa_status", "doi", "resolved_title", "resolved_year", "first_author", "journal", "match_score", "metadata_source"):
                    item[key] = row.get(key, item.get(key))
        metadata_path.write_text(json.dumps(metadata, ensure_ascii=False, indent=2), encoding="utf-8")

    (root / "europepmc_recovery_log.txt").write_text("\n".join(log) + "\n", encoding="utf-8")

    downloaded = [row for row in rows if row.get("status") == "downloaded"]
    lines = [
        "# PhD Part I literature bundle", "",
        f"- Requested references: **{len(rows)}**",
        f"- Open-access PDFs downloaded: **{len(downloaded)}**",
        f"- Not downloaded: **{len(unavailable)}**", "",
        "Only openly accessible copies were downloaded. The workflow did not bypass paywalls, authentication, robots controls, or publisher access restrictions.", "",
        "## Contents", "",
        "- `pdfs/`: downloaded PDFs grouped by thesis topic.",
        "- `all_references_resolved.csv`: complete metadata and retrieval status.",
        "- `unavailable_title_doi.csv`: title and DOI for every item without a downloaded PDF.",
        "- `resolved_metadata.json`: machine-readable metadata.",
        "- `download_log.txt`, `recovery_log.txt`, `europepmc_recovery_log.txt`: retrieval logs.",
        "- `requested_references.json`: the requested 211-item bibliography.", "",
        "## Downloaded papers", "",
    ]
    for row in downloaded:
        lines.append(f"- {row['id']}. {row.get('resolved_title') or row.get('requested_title')} — DOI: {row.get('doi') or 'not resolved'} — `{row.get('pdf_filename')}`")
    lines.extend(["", "## Not downloaded", ""])
    for row in unavailable:
        lines.append(f"- {row['id']}. {row.get('resolved_title') or row.get('requested_title')} — DOI: {row.get('doi') or 'not resolved'} — {row.get('status')}")
    (root / "README.md").write_text("\n".join(lines) + "\n", encoding="utf-8")


def main() -> int:
    if len(sys.argv) != 2:
        raise SystemExit("Usage: recover_europepmc_pdf.py BUNDLE_DIRECTORY")
    root = Path(sys.argv[1])
    with (root / "all_references_resolved.csv").open(encoding="utf-8-sig", newline="") as handle:
        rows = list(csv.DictReader(handle))
    targets = [dict(row) for row in rows if row.get("status") != "downloaded"]
    updates: dict[int, dict[str, str]] = {}
    log: list[str] = []
    with ThreadPoolExecutor(max_workers=THREADS) as pool:
        futures = {pool.submit(recover, row, root): int(row["id"]) for row in targets}
        for future in as_completed(futures):
            ref_id = futures[future]
            try:
                rid, updated, message = future.result()
                if updated:
                    updates[rid] = updated
                log.append(f"{rid}: {message}")
            except Exception as exc:  # noqa: BLE001
                log.append(f"{ref_id}: worker error: {exc}")
    rows = [updates.get(int(row["id"]), row) for row in rows]
    rows.sort(key=lambda row: int(row["id"]))
    rewrite(root, rows, sorted(log, key=lambda value: int(value.split(':', 1)[0])))
    print(f"Europe PMC recovery: {len(updates)} additional PDFs; total {sum(row.get('status') == 'downloaded' for row in rows)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
