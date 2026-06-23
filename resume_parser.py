"""
resume_parser.py
Parse a base resume .docx into structured content for the generation pipeline.

The parser is heuristic, keyed on the consistent structure of the base resumes:
  Name (centered, bold)
  Contact (centered, non-bold, contains '|' and an email)
  SECTION HEADERS (centered, bold): EDUCATION / EXPERIENCE / SKILLS...
  Within EXPERIENCE:
    Company  = bold, no ' | ', no trailing date
    Role line = bold, has ' | ' and a trailing date (after tab or 2+ spaces)
    Bullet    = non-bold (justified)
  Within EDUCATION:
    Institution = bold (often with trailing date)
    sub-lines   = non-bold (location, degree, coursework)
  Within SKILLS:
    'Category: items' lines

Public API:
    parse_resume(path, version=None) -> dict
    {
      "version": "701",
      "candidate": {"name", "contact_raw", "email", "phone", "location", "linkedin"},
      "education": [{"institution", "date", "lines": [...]}],
      "experience": [{"company", "title", "location", "dates", "bullets": [...]}],
      "skills": [{"category", "items"}],   # items is raw string
      "skills_section_title": "SKILLS & PROJECTS",
    }
"""

import os
import re
import json
import argparse

try:
    import docx
except ImportError:
    raise SystemExit("python-docx is required: pip3 install python-docx")

# ── Detection helpers ──────────────────────────────────────────────────────────

MONTHS = r"Jan|Feb|Mar|Apr|May|Jun|Jul|Aug|Sep|Oct|Nov|Dec"
# A trailing date chunk: starts after tab or 2+ spaces, contains a month or 4-digit year or 'Present'
DATE_TAIL_RE = re.compile(
    r"(?:\t|\s{2,})\s*((?:" + MONTHS + r")?[\w .,–\-/]*?(?:\d{4}|Present))\s*$"
)
SECTION_WORDS = ("EDUCATION", "EXPERIENCE", "SKILLS", "PROJECTS", "CERTIFICATIONS")
EMAIL_RE = re.compile(r"[\w.+-]+@[\w-]+\.[\w.-]+")
PHONE_RE = re.compile(r"\+?\d?\s*\(?\d{3}\)?[\d\s().\-]{6,}\d")


def _is_bold(p) -> bool:
    return any(r.bold for r in p.runs if r.text.strip())


def _align(p):
    return {0: "L", 1: "C", 2: "R", 3: "J"}.get(
        p.alignment.real if p.alignment is not None else None
    )


def _split_trailing_date(text: str):
    """Return (main, date_or_None). Date is detected after tab or 2+ spaces."""
    m = DATE_TAIL_RE.search(text)
    if m:
        date = m.group(1).strip()
        main = text[: m.start()].strip()
        return main, date
    return text.strip(), None


def _section_of(text: str):
    """If this line is a section header, return its canonical key, else None."""
    up = text.strip().upper()
    # Header lines are short and consist mostly of section words
    if len(up) <= 40 and any(w in up for w in SECTION_WORDS):
        if "EDUCATION" in up:
            return "EDUCATION"
        if "EXPERIENCE" in up:
            return "EXPERIENCE"
        if "SKILL" in up or "PROJECT" in up or "CERTIFICATION" in up:
            return "SKILLS"
    return None


# ── Main parse ─────────────────────────────────────────────────────────────────

def parse_resume(path: str, version: str = None) -> dict:
    d = docx.Document(path)

    if version is None:
        m = re.search(r"(301|303|701)", os.path.basename(path))
        version = m.group(1) if m else None

    paras = [p for p in d.paragraphs if p.text.strip()]

    result = {
        "version": version,
        "candidate": {},
        "education": [],
        "experience": [],
        "skills": [],
        "skills_section_title": None,
    }

    # 1. Name = first paragraph
    result["candidate"]["name"] = paras[0].text.strip()
    # 2. Contact = first line containing '@' or ' | '
    contact_idx = None
    for i, p in enumerate(paras[1:4], start=1):
        if "@" in p.text or " | " in p.text:
            contact_idx = i
            break
    if contact_idx is not None:
        raw = paras[contact_idx].text.strip()
        result["candidate"]["contact_raw"] = raw
        email = EMAIL_RE.search(raw)
        phone = PHONE_RE.search(raw)
        parts = [s.strip() for s in raw.split("|")]
        result["candidate"]["email"] = email.group(0) if email else None
        result["candidate"]["phone"] = phone.group(0) if phone else None
        result["candidate"]["location"] = parts[0] if parts else None
        result["candidate"]["linkedin"] = next(
            (s for s in parts if "linkedin" in s.lower()), None
        )
        # linkedin_url is not in the docx — must be set separately by the user
        result["candidate"]["linkedin_url"] = None

    # 3. Walk sections
    section = None
    current_role = None
    for p in paras:
        text = p.text.strip()
        # Section headers are centered + bold; this prevents justified sub-labels
        # like "Projects:" from being mistaken for a new section.
        sec = _section_of(text) if _align(p) == "C" else None
        if sec:
            section = sec
            if sec == "SKILLS":
                result["skills_section_title"] = text.upper()
            current_role = None
            continue

        if section == "EDUCATION":
            main, date = _split_trailing_date(text)
            if _is_bold(p):
                result["education"].append({"institution": main, "date": date, "lines": []})
            else:
                if result["education"]:
                    result["education"][-1]["lines"].append(text)
                else:
                    result["education"].append({"institution": main, "date": date, "lines": []})

        elif section == "EXPERIENCE":
            main, date = _split_trailing_date(text)
            bold = _is_bold(p)
            if bold and (" | " in main) and date:
                # Role line: "Title | Location   <date>"
                title, _, location = main.partition(" | ")
                role_fields = {
                    "title": title.strip(),
                    "location": location.strip(),
                    "dates": date,
                }
                # Attach to the most recent company stub, preserving its company name
                if result["experience"] and result["experience"][-1].get("title") is None:
                    current_role = result["experience"][-1]
                    current_role.update(role_fields)
                else:
                    current_role = {"company": None, "bullets": [], **role_fields}
                    result["experience"].append(current_role)
            elif bold and (" | " not in main):
                # Company line
                result["experience"].append(
                    {"company": main, "title": None, "location": None, "dates": None, "bullets": []}
                )
                current_role = None
            else:
                # Bullet
                if result["experience"]:
                    result["experience"][-1]["bullets"].append(text)

        elif section == "SKILLS":
            if ":" in text:
                cat, _, items = text.partition(":")
                result["skills"].append({"category": cat.strip(), "content": items.strip()})
            else:
                # Continuation / project bullet
                if result["skills"]:
                    result["skills"][-1]["content"] += " " + text

    # Drop any company stubs that never got a role/bullets
    result["experience"] = [
        r for r in result["experience"] if r.get("bullets") or r.get("title")
    ]
    return result


# ── CLI / test ─────────────────────────────────────────────────────────────────

if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("path")
    ap.add_argument("--version", default=None)
    ap.add_argument("--json", action="store_true", help="dump full JSON")
    args = ap.parse_args()

    parsed = parse_resume(args.path, args.version)

    if args.json:
        print(json.dumps(parsed, indent=2, ensure_ascii=False))
    else:
        c = parsed["candidate"]
        print(f"VERSION: {parsed['version']}")
        print(f"NAME: {c.get('name')}")
        print(f"CONTACT: {c.get('location')} | {c.get('phone')} | {c.get('email')} | {c.get('linkedin')}")
        print(f"\nEDUCATION ({len(parsed['education'])}):")
        for e in parsed["education"]:
            print(f"  - {e['institution']}  [{e['date']}]")
            for ln in e["lines"]:
                print(f"      {ln}")
        print(f"\nEXPERIENCE ({len(parsed['experience'])}):")
        for r in parsed["experience"]:
            print(f"  - {r['company']} | {r['title']} | {r['location']} | {r['dates']} ({len(r['bullets'])} bullets)")
        print(f"\nSKILLS [{parsed['skills_section_title']}] ({len(parsed['skills'])}):")
        for s in parsed["skills"]:
            print(f"  - {s['category']}: {s['content'][:70]}")
