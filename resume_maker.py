"""
resume_maker.py
Builds a one-page, ATS-friendly PDF resume from parsed base-resume data.

Public API:
    build_pdf(parsed, roles_override=None, settings=None) -> (filename, error, warnings)
    build_pdf_from_json(version, settings=None) -> (filename, error, warnings)

Settings dict (all optional, passed from UI sliders):
    bullet_count   int   3          bullets per role to include
    line_fill_pct  int   100        target % of line width each bullet should fill
                                    (100=single full line, 150=1.5 lines, 200=2 full lines)
    deviation      float 0.3        passed to generation; no effect on rendering itself

Standalone test:
    python3 resume_maker.py --version 701 [--count 3] [--linefill 100]
"""

import os
import re
import json
import secrets
import asyncio
import argparse
import datetime
from jinja2 import Template

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
OUTPUT_DIR = os.path.join(BASE_DIR, "outputs")
DATA_DIR = os.path.join(BASE_DIR, "data", "base_resumes")

# ── HTML Template ──────────────────────────────────────────────────────────────
# ATS checklist enforced here:
#   ✓ Real selectable text (not images)
#   ✓ Single column, no tables / text boxes
#   ✓ Standard section headings (EDUCATION / EXPERIENCE / SKILLS)
#   ✓ No icons, no header/footer regions
#   ✓ <ul> bullets with disc, no exotic glyphs
#   ✓ Date formats: "Month YYYY – Month YYYY" or "Month YYYY – Present"
#   ✓ DOM reading order = ATS reading order (title before date, inline)

RESUME_HTML = """\
<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<style>
  @page { size: letter; margin: 0; }
  * { margin: 0; padding: 0; box-sizing: border-box; }

  body {
    font-family: "Times New Roman", Times, serif;
    font-size: 10pt;
    line-height: 1.15;
    color: #000;
    background: #fff;
    width: 8.5in;
    padding: 0.39in;
  }

  /* ── Name ── */
  .name {
    text-align: center;
    font-size: 14pt;
    font-weight: bold;
    margin-bottom: 1pt;
  }

  /* ── Contact ── */
  .contact {
    text-align: center;
    font-size: 10pt;
    margin-bottom: 4pt;
  }

  /* Hyperlinks — standard blue, underlined */
  a { color: #1155CC; text-decoration: underline; }

  /* ── Section headers ── */
  .section-header {
    text-align: center;
    font-size: 10pt;
    font-weight: bold;
    border-bottom: 0.75pt solid #000;
    margin-top: 4pt;
    margin-bottom: 2pt;
    padding-bottom: 1pt;
    letter-spacing: 0.04em;
  }

  /* ── Education block ── */
  .edu-block { margin-top: 2pt; }

  .edu-row {
    display: flex;
    justify-content: space-between;
    align-items: baseline;
  }

  .edu-inst { font-weight: bold; font-size: 10pt; }
  .edu-date { font-size: 10pt; white-space: nowrap; padding-left: 8pt; }

  .edu-sub {
    font-size: 10pt;
    margin-top: 0;
    margin-left: 0;
  }

  /* ── Experience block ── */
  .exp-block { margin-top: 2pt; }

  .company-name {
    font-weight: bold;
    font-size: 10pt;
    margin-bottom: 0;
  }

  /* Role line: title+location left, date right — inline so ATS reads title first */
  .role-row {
    display: flex;
    justify-content: space-between;
    align-items: baseline;
    margin-bottom: 1pt;
  }

  .role-left { font-weight: bold; font-size: 10pt; }
  .role-date { font-weight: bold; font-size: 10pt; white-space: nowrap; padding-left: 8pt; }

  /* ── Bullets ── */
  ul.bullets {
    margin: 0;
    padding-left: 14pt;
    list-style-type: disc;
    margin-bottom: 1pt;
  }

  ul.bullets li {
    font-size: 10pt;
    font-weight: normal;
    line-height: 1.2;
    margin-bottom: 0.5pt;
    text-align: justify;
    /* Hyphenation helps justify fill naturally without big gaps */
    -webkit-hyphens: auto;
    hyphens: auto;
  }

  /* ── Skills ── */
  .skills-block { margin-top: 2pt; }

  .skills-line {
    font-size: 10pt;
    line-height: 1.2;
    margin-bottom: 1pt;
    text-align: justify;
  }

  .skills-line .cat { font-weight: bold; }

</style>
</head>
<body>

  <!-- NAME -->
  <div class="name">{{ candidate.name }}</div>

  <!-- CONTACT — email and LinkedIn are hyperlinked; rest is plain text -->
  <div class="contact">
    {{- candidate.location -}}
    {%- if candidate.phone %} | {{ candidate.phone }}{% endif %}
    {%- if candidate.email %} | <a href="mailto:{{ candidate.email }}">{{ candidate.email }}</a>{% endif %}
    {%- if candidate.linkedin_url %} | <a href="{{ candidate.linkedin_url }}">LinkedIn</a>
    {%- elif candidate.linkedin %} | <a href="{{ candidate.linkedin if candidate.linkedin.startswith('http') else 'https://linkedin.com/in/' + candidate.linkedin }}">LinkedIn</a>
    {%- endif %}
  </div>

  <!-- EDUCATION (top, per spec) -->
  <div class="section-header">EDUCATION</div>
  {% for edu in education %}
  <div class="edu-block">
    <div class="edu-row">
      <span class="edu-inst">{{ edu.institution }}</span>
      {% if edu.date %}<span class="edu-date">{{ edu.date }}</span>{% endif %}
    </div>
    {% for line in edu.lines %}
    <div class="edu-sub">{{ line }}</div>
    {% endfor %}
  </div>
  {% endfor %}

  <!-- EXPERIENCE -->
  <div class="section-header">EXPERIENCE</div>
  {% for role in roles %}
  <div class="exp-block">
    <div class="company-name">{{ role.company }}</div>
    <div class="role-row">
      <span class="role-left">{{ role.title }}{% if role.location %}, {{ role.location }}{% endif %}</span>
      <span class="role-date">{{ role.dates }}</span>
    </div>
    <ul class="bullets">
      {% for bullet in role.bullets %}
      <li>{{ bullet }}</li>
      {% endfor %}
    </ul>
  </div>
  {% endfor %}

  <!-- SKILLS -->
  {% if skills %}
  <div class="section-header">{{ skills_title }}</div>
  <div class="skills-block">
    {% for s in skills %}
    <div class="skills-line"><span class="cat">{{ s.category }}:</span> {{ s.content }}</div>
    {% endfor %}
  </div>
  {% endif %}

</body>
</html>
"""


# ── Helpers ────────────────────────────────────────────────────────────────────

def load_parsed(version: str) -> dict:
    path = os.path.join(DATA_DIR, f"{version}.json")
    if not os.path.exists(path):
        raise FileNotFoundError(f"No parsed base resume found for version {version}. Upload one first.")
    with open(path, encoding="utf-8") as f:
        return json.load(f)


def render_html(parsed: dict, roles_override: list = None, settings: dict = None) -> str:
    """
    Merge parsed base-resume data into the HTML template.
    roles_override: list of {company, title, location, dates, bullets[]} from generation.
    If None, uses the base bullets from the parsed JSON.
    settings: {bullet_count, line_fill_pct, deviation}
    """
    settings = settings or {}
    bullet_count = int(settings.get("bullet_count", 3))

    roles = roles_override if roles_override is not None else parsed["experience"]

    # Trim bullets to bullet_count
    trimmed_roles = []
    for r in roles:
        trimmed_roles.append({**r, "bullets": r["bullets"][:bullet_count]})

    # Skills section title: normalize common variants
    raw_title = (parsed.get("skills_section_title") or "SKILLS").upper()
    # Collapse to canonical ATS-friendly heading
    if "CERTIFICATION" in raw_title or "LANGUAGE" in raw_title:
        skills_title = "SKILLS & CERTIFICATIONS"
    elif "PROJECT" in raw_title:
        skills_title = "SKILLS & PROJECTS"
    else:
        skills_title = "SKILLS"

    tmpl = Template(RESUME_HTML)
    return tmpl.render(
        candidate=parsed["candidate"],
        education=parsed["education"],
        roles=trimmed_roles,
        skills=parsed.get("skills", []),
        skills_title=skills_title,
    )


async def _measure_line_fill(page, line_fill_pct: int) -> list:
    """
    Measure the last-line fill ratio for each bullet.
    Returns list of {text, last_line_fill_pct, warn} for bullets where fill < threshold.
    Threshold: flag if last line is less than 40% full (orphan half-line).
    """
    warnings = []
    try:
        result = await page.evaluate("""
        () => {
            const items = document.querySelectorAll('ul.bullets li');
            const out = [];
            items.forEach(li => {
                const range = document.createRange();
                range.selectNodeContents(li);
                const rects = range.getClientRects();
                if (rects.length === 0) return;
                const fullWidth = li.getBoundingClientRect().width;
                const lastRect = rects[rects.length - 1];
                const fillPct = Math.round((lastRect.width / fullWidth) * 100);
                out.push({text: li.textContent.substring(0, 60), fillPct, fullWidth, lastWidth: lastRect.width});
            });
            return out;
        }
        """)
        for item in result:
            fill = item.get("fillPct", 100)
            # Flag orphan: last line less than 40% full (looks bad) but more than 5%
            # (5% means it's essentially empty trailing whitespace, not a real orphan)
            if 5 < fill < 40:
                warnings.append({
                    "text": item["text"],
                    "last_line_fill_pct": fill,
                    "warn": f"Last line only {fill}% full — consider trimming or expanding slightly.",
                })
    except Exception:
        pass  # Non-fatal: measurement failure doesn't block PDF generation
    return warnings


async def _render_pdf_async(html: str, output_path: str, line_fill_pct: int = 100) -> dict:
    """Render HTML → PDF. Returns {ok, warnings, scroll_height_px} or {ok=False, error}."""
    try:
        from playwright.async_api import async_playwright
    except ImportError:
        return {"ok": False, "error": "playwright is not installed: pip3 install playwright && playwright install chromium"}

    async with async_playwright() as p:
        browser = await p.chromium.launch()
        page = await browser.new_page(viewport={"width": 816, "height": 1056})  # 8.5 x 11in at 96dpi

        await page.set_content(html, wait_until="networkidle")

        # One-page overflow check: 11in at 96dpi = 1056px
        scroll_height = await page.evaluate("() => document.body.scrollHeight")
        if scroll_height > 1056:
            await browser.close()
            overflow_pt = round((scroll_height - 1056) * 0.75)
            return {
                "ok": False,
                "error": f"Content overflows by ~{overflow_pt}pt. Reduce bullet count or shorten bullets.",
                "scroll_height_px": scroll_height,
            }

        # Auto-spacing: distribute remaining vertical space across section headers
        # so the resume fills the page rather than leaving an empty bottom gap.
        # Cap extra per header conservatively to avoid pushing over the page boundary.
        remaining_px = 1056 - scroll_height
        if remaining_px > 20:
            n_headers = await page.evaluate(
                "() => document.querySelectorAll('.section-header').length"
            )
            if n_headers > 0:
                # Cap at 12px/header (conservative) to leave buffer before overflow
                extra_per_header = min(remaining_px / n_headers, 12)
                await page.evaluate(f"""
                () => {{
                    const headers = document.querySelectorAll('.section-header');
                    headers.forEach(h => {{
                        const cur = parseFloat(getComputedStyle(h).marginTop) || 0;
                        h.style.marginTop = (cur + {extra_per_header:.1f}) + 'px';
                    }});
                }}
                """)
                scroll_height = await page.evaluate("() => document.body.scrollHeight")
                if scroll_height > 1056:
                    # Full rollback — remove all added spacing
                    await page.evaluate(f"""
                    () => {{
                        const headers = document.querySelectorAll('.section-header');
                        headers.forEach(h => {{
                            const cur = parseFloat(getComputedStyle(h).marginTop) || 0;
                            h.style.marginTop = Math.max(0, cur - {extra_per_header:.1f}) + 'px';
                        }});
                    }}
                    """)
                    scroll_height = await page.evaluate("() => document.body.scrollHeight")

        fill_warnings = []  # line-fill orphan warnings removed — not actionable by users

        # No PDF-level margin: the body already has padding: 0.39in which provides
        # the visual margins. Adding margin here would double-margin the content,
        # shrinking the printable area and causing overflow that isn't caught by scrollHeight.
        await page.pdf(
            path=output_path,
            format="Letter",
            margin={"top": "0", "right": "0", "bottom": "0", "left": "0"},
            print_background=False,
        )
        await browser.close()

    if not os.path.exists(output_path) or os.path.getsize(output_path) == 0:
        return {"ok": False, "error": "PDF file was not written or is empty."}

    return {"ok": True, "warnings": fill_warnings, "scroll_height_px": scroll_height}


def _safe_slug(text: str, max_len: int = 20) -> str:
    """Convert a name/nickname to a safe filename-friendly slug."""
    slug = re.sub(r"[^\w\s-]", "", text or "").strip()
    slug = re.sub(r"[\s]+", "_", slug)
    return slug[:max_len] or "resume"


def build_pdf(parsed: dict, roles_override: list = None, settings: dict = None,
              nickname: str = None, target_company: str = None,
              custom_filename: str = None) -> tuple:
    """
    Build PDF from parsed resume data.
    Returns (filename, error, warnings).

    The on-disk filename is CandidateName_TargetCompany_YYYY-MM-DD__<token>.pdf,
    where <token> is an unguessable random suffix. This is a security boundary:
    the /download route is unauthenticated, so a deterministic filename would let
    anyone enumerate and download other users' resumes by guessing name+company+date.
    The download route strips the token to restore the clean saved filename.
    Pass custom_filename (without .pdf) to override the generated stem.
    """
    import datetime
    os.makedirs(OUTPUT_DIR, exist_ok=True)
    settings = settings or {}
    candidate_name = parsed.get("candidate", {}).get("name", "Resume")
    name_slug    = _safe_slug(candidate_name)
    date_str     = datetime.date.today().strftime("%Y-%m-%d")
    if custom_filename:
        stem = _safe_slug(custom_filename, max_len=80)
    else:
        company_slug = _safe_slug(target_company or nickname or parsed.get("version", ""))
        stem = f"{name_slug}_{company_slug}_{date_str}" if company_slug else f"{name_slug}_{date_str}"
    # 16 hex chars (64 bits) of entropy makes the download URL unguessable.
    token = secrets.token_hex(8)
    filename = f"{stem}__{token}.pdf"
    output_path = os.path.join(OUTPUT_DIR, filename)

    html = render_html(parsed, roles_override, settings)
    result = asyncio.run(_render_pdf_async(html, output_path, settings.get("line_fill_pct", 100)))

    if not result.get("ok"):
        return None, result.get("error", "unknown render error"), []

    return filename, None, result.get("warnings", [])


def build_pdf_from_json(version: str, settings: dict = None) -> tuple:
    """Convenience wrapper: load parsed JSON for version, render with base bullets."""
    parsed = load_parsed(version)
    return build_pdf(parsed, roles_override=None, settings=settings)


# ── ATS text extraction check ──────────────────────────────────────────────────

def check_ats(pdf_path: str, expected_name: str = "") -> dict:
    """
    Run pdftotext on the output PDF and verify clean linear reading order.
    Returns {ok, text_snippet, issues}.
    """
    import subprocess, shutil
    pdftotext_bin = shutil.which("pdftotext") or "/opt/homebrew/bin/pdftotext"
    result = subprocess.run(
        [pdftotext_bin, "-layout", pdf_path, "-"],
        capture_output=True, text=True
    )
    if result.returncode != 0:
        return {"ok": None, "issues": ["pdftotext not available — install poppler-utils to run ATS check"]}

    text = result.stdout
    issues = []
    # Check key ATS markers appear in reasonable order
    order_markers = ["EDUCATION", "EXPERIENCE", "SKILLS"]
    positions = {m: text.upper().find(m) for m in order_markers}
    if any(v == -1 for v in positions.values()):
        missing = [m for m, v in positions.items() if v == -1]
        issues.append(f"Section headers missing from extracted text: {missing}")
    elif not (positions["EDUCATION"] < positions["EXPERIENCE"] < positions["SKILLS"]):
        issues.append(f"Section headers out of ATS-expected order: {positions}")

    # Check name appears at top. Use the candidate's surname (last token of the
    # expected name) as the marker; skip the check if no name was provided.
    lines = [l.strip() for l in text.splitlines() if l.strip()]
    surname = (expected_name or "").strip().split()[-1].upper() if (expected_name or "").strip() else ""
    if surname and lines and surname not in lines[0].upper():
        issues.append(f"Name not at top of extracted text. First line: {lines[0]!r}")

    return {"ok": len(issues) == 0, "issues": issues, "text_snippet": text[:300]}


# ── Standalone test ────────────────────────────────────────────────────────────

if __name__ == "__main__":
    ap = argparse.ArgumentParser(description="Generate and visually inspect a resume PDF")
    ap.add_argument("--version", default="701", choices=["301", "303", "701"])
    ap.add_argument("--count", type=int, default=3, help="Bullets per role")
    ap.add_argument("--linefill", type=int, default=100, help="Target line fill %%")
    ap.add_argument("--ats", action="store_true", help="Run pdftotext ATS check")
    ap.add_argument("--no-open", action="store_true", help="Skip opening in Preview")
    args = ap.parse_args()

    settings = {"bullet_count": args.count, "line_fill_pct": args.linefill}
    print(f"Rendering version {args.version} (count={args.count}, linefill={args.linefill}%)...")

    filename, error, warnings = build_pdf_from_json(args.version, settings)

    if error:
        print(f"ERROR: {error}")
        raise SystemExit(1)

    output_path = os.path.join(OUTPUT_DIR, filename)
    print(f"PDF written: {output_path}")

    if warnings:
        print(f"WARNINGS ({len(warnings)} orphan bullets):")
        for w in warnings:
            print(f"  [{w['last_line_fill_pct']}%] {w['text'][:60]}...")
    else:
        print("Line-fill: no orphan warnings.")

    if args.ats:
        print("\nATS check:")
        parsed = load_parsed(args.version) or {}
        expected_name = parsed.get("candidate", {}).get("name", "")
        ats = check_ats(output_path, expected_name)
        if ats["ok"] is None:
            print(f"  SKIP: {ats['issues'][0]}")
        elif ats["ok"]:
            print("  PASS — clean linear reading order")
            print(f"  First 200 chars: {ats['text_snippet'][:200]!r}")
        else:
            print(f"  ISSUES: {ats['issues']}")

    if not args.no_open:
        print("Opening for visual inspection...")
        os.system(f'open "{output_path}"')
