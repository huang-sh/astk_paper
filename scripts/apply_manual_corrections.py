#!/usr/bin/env python3
"""Apply manually verified bibliographic corrections before OA recovery.

A small number of short or duplicate titles were ambiguously resolved by fuzzy
metadata matching (for example, an erratum, a preprint, or a commentary rather
than the requested journal article). This script replaces those records with
verified journal metadata and removes any previously downloaded mismatched or
preprint PDF so the open-access recovery pass can fetch the correct version.
"""
from __future__ import annotations

import csv
import json
import sys
from pathlib import Path

CORRECTIONS = {
    20: {
        "title": "GenBank",
        "year": "2013",
        "doi": "10.1093/nar/gks1195",
        "first_author": "Benson",
        "journal": "Nucleic Acids Research",
        "replace_pdf": False,
    },
    21: {
        "title": "The international nucleotide sequence database collaboration",
        "year": "2021",
        "doi": "10.1093/nar/gkaa967",
        "first_author": "Arita",
        "journal": "Nucleic Acids Research",
        "replace_pdf": False,
    },
    25: {
        "title": "UniProt: the Universal Protein Knowledgebase in 2023",
        "year": "2023",
        "doi": "10.1093/nar/gkac1052",
        "first_author": "UniProt Consortium",
        "journal": "Nucleic Acids Research",
        "replace_pdf": False,
    },
    42: {
        "title": "Taking a fresh look at FAIR for research software",
        "year": "2021",
        "doi": "10.1016/j.patter.2021.100222",
        "first_author": "Katz",
        "journal": "Patterns",
        "replace_pdf": False,
    },
    43: {
        "title": "Introducing the FAIR Principles for research software",
        "year": "2022",
        "doi": "10.1038/s41597-022-01710-x",
        "first_author": "Barker",
        "journal": "Scientific Data",
        "replace_pdf": False,
    },
    49: {
        "title": "Basic local alignment search tool",
        "year": "1990",
        "doi": "10.1016/S0022-2836(05)80360-2",
        "first_author": "Altschul",
        "journal": "Journal of Molecular Biology",
        "replace_pdf": True,
    },
    60: {
        "title": "Moderated estimation of fold change and dispersion for RNA-seq data with DESeq2",
        "year": "2014",
        "doi": "10.1186/s13059-014-0550-8",
        "first_author": "Love",
        "journal": "Genome Biology",
        "replace_pdf": True,
    },
    68: {
        "title": "Bioconductor: open software development for computational biology and bioinformatics",
        "year": "2004",
        "doi": "10.1186/gb-2004-5-10-r80",
        "first_author": "Gentleman",
        "journal": "Genome Biology",
        "replace_pdf": False,
    },
    70: {
        "title": "Orchestrating single-cell analysis with Bioconductor",
        "year": "2020",
        "doi": "10.1038/s41592-019-0654-x",
        "first_author": "Amezquita",
        "journal": "Nature Methods",
        "replace_pdf": True,
    },
    74: {
        "title": "Integrated analysis of multimodal single-cell data",
        "year": "2021",
        "doi": "10.1016/j.cell.2021.04.048",
        "first_author": "Hao",
        "journal": "Cell",
        "replace_pdf": True,
    },
    82: {
        "title": "Array programming with NumPy",
        "year": "2020",
        "doi": "10.1038/s41586-020-2649-2",
        "first_author": "Harris",
        "journal": "Nature",
        "replace_pdf": False,
    },
    83: {
        "title": "SciPy 1.0: fundamental algorithms for scientific computing in Python",
        "year": "2020",
        "doi": "10.1038/s41592-019-0686-2",
        "first_author": "Virtanen",
        "journal": "Nature Methods",
        "replace_pdf": False,
    },
    163: {
        "title": "Highly accurate protein structure prediction with AlphaFold",
        "year": "2021",
        "doi": "10.1038/s41586-021-03819-2",
        "first_author": "Jumper",
        "journal": "Nature",
        "replace_pdf": False,
    },
    164: {
        "title": "Accurate structure prediction of biomolecular interactions with AlphaFold 3",
        "year": "2024",
        "doi": "10.1038/s41586-024-07487-w",
        "first_author": "Abramson",
        "journal": "Nature",
        "replace_pdf": False,
    },
    169: {
        "title": "Effective gene expression prediction from sequence by integrating long-range interactions",
        "year": "2021",
        "doi": "10.1038/s41592-021-01252-x",
        "first_author": "Avsec",
        "journal": "Nature Methods",
        "replace_pdf": True,
    },
    199: {
        "title": "Distributed cognition: toward a new foundation for human-computer interaction research",
        "year": "2000",
        "doi": "10.1145/353485.353487",
        "first_author": "Hollan",
        "journal": "ACM Transactions on Computer-Human Interaction",
        "replace_pdf": False,
    },
    210: {
        "title": "From here to autonomy: lessons learned from human–automation research",
        "year": "2017",
        "doi": "10.1177/0018720816681350",
        "first_author": "Endsley",
        "journal": "Human Factors",
        "replace_pdf": False,
    },
}


def main() -> int:
    if len(sys.argv) != 2:
        raise SystemExit("Usage: apply_manual_corrections.py BUNDLE_DIRECTORY")
    root = Path(sys.argv[1])
    csv_path = root / "all_references_resolved.csv"
    with csv_path.open(encoding="utf-8-sig", newline="") as handle:
        rows = list(csv.DictReader(handle))
        fieldnames = handle.seek(0) or None
    # Re-read the header explicitly because DictReader's fieldnames remain useful
    # after the context manager closes.
    with csv_path.open(encoding="utf-8-sig", newline="") as handle:
        reader = csv.DictReader(handle)
        fieldnames = reader.fieldnames or []

    changed = []
    for row in rows:
        ref_id = int(row["id"])
        correction = CORRECTIONS.get(ref_id)
        if not correction:
            continue
        old_doi = (row.get("doi") or "").lower()
        new_doi = correction["doi"].lower()
        replace_pdf = bool(correction.get("replace_pdf")) or (old_doi and old_doi != new_doi and row.get("status") == "downloaded")
        if replace_pdf and row.get("pdf_filename"):
            target = root / row["pdf_filename"]
            target.unlink(missing_ok=True)
            row["status"] = "not_open_access_or_no_pdf_found"
            row["pdf_url"] = ""
            row["pdf_filename"] = ""
            row["open_access"] = "False"
            row["oa_status"] = ""
        row["requested_title"] = correction["title"]
        row["resolved_title"] = correction["title"]
        row["requested_year"] = correction["year"]
        row["resolved_year"] = correction["year"]
        row["doi"] = correction["doi"]
        row["first_author"] = correction["first_author"]
        row["journal"] = correction["journal"]
        row["match_score"] = "1.0"
        row["metadata_source"] = "manual verification"
        row["landing_url"] = f"https://doi.org/{correction['doi']}"
        note = row.get("note") or ""
        row["note"] = (note + "; bibliographic metadata manually verified").strip("; ")
        changed.append(ref_id)

    rows.sort(key=lambda row: int(row["id"]))
    with csv_path.open("w", encoding="utf-8-sig", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=fieldnames)
        writer.writeheader()
        writer.writerows(rows)

    metadata_path = root / "resolved_metadata.json"
    if metadata_path.exists():
        metadata = json.loads(metadata_path.read_text(encoding="utf-8"))
        by_id = {str(row["id"]): row for row in rows}
        for item in metadata:
            row = by_id.get(str(item.get("id")))
            if row:
                for key in row:
                    if key in item:
                        item[key] = row[key]
        metadata_path.write_text(json.dumps(metadata, ensure_ascii=False, indent=2), encoding="utf-8")

    print(f"Applied {len(changed)} verified corrections: {changed}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
