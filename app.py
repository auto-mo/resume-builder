import os
import re
import glob
import json
import uuid
import time
import hmac
import hashlib
import threading
from collections import deque
from datetime import datetime, timezone

import yaml
import anthropic
import openai
from flask import Flask, request, jsonify, render_template, send_from_directory
from dotenv import load_dotenv
from resume_maker import build_pdf
from resume_parser import parse_resume

load_dotenv(os.path.join(os.path.dirname(os.path.abspath(__file__)), ".env"), override=True)

app = Flask(__name__)
# Cap request bodies (resume .docx uploads + JD text) to a sane size. A normal
# resume docx is well under 1 MB; 12 MB leaves generous headroom while blocking
# memory-exhaustion attempts on the public endpoint.
app.config["MAX_CONTENT_LENGTH"] = 12 * 1024 * 1024
_BASE = os.path.dirname(os.path.abspath(__file__))
app.config["OUTPUT_DIR"] = os.path.join(_BASE, "outputs")
# Legacy fixed-slot store (301/303/701) — retained only for reference; new code uses RESUMES_DIR.
app.config["DATA_DIR"] = os.path.join(_BASE, "data", "base_resumes")
# Flexible resume registry: one {id}.docx + {id}.json per uploaded resume.
app.config["RESUMES_DIR"] = os.path.join(_BASE, "data", "resumes")

# ── Cover letter KB ───────────────────────────────────────────────────────────
_CL_KB_PATH = os.path.join(_BASE, "cover_letter_kb.yaml")
def _load_cl_kb() -> dict:
    if not os.path.exists(_CL_KB_PATH):
        return {}
    with open(_CL_KB_PATH, encoding="utf-8") as f:
        return yaml.safe_load(f) or {}

COVER_LETTER_KB = _load_cl_kb()


# ── Provider constants ────────────────────────────────────────────────────────

# Curated model lists per provider — UI populates dropdowns from these.
PROVIDER_MODELS = {
    "anthropic": [
        {"id": "claude-opus-4-8",           "label": "Claude Opus 4.8 (most capable)"},
        {"id": "claude-sonnet-4-6",         "label": "Claude Sonnet 4.6 (recommended)"},
        {"id": "claude-haiku-4-5-20251001", "label": "Claude Haiku 4.5 (fastest)"},
    ],
    "openai": [
        {"id": "gpt-4o",       "label": "GPT-4o (recommended)"},
        {"id": "gpt-4o-mini",  "label": "GPT-4o Mini (fastest)"},
        {"id": "o3-mini",      "label": "o3-mini (reasoning)"},
        {"id": "gpt-4-turbo",  "label": "GPT-4 Turbo"},
    ],
}


def detect_provider(api_key: str) -> str:
    """Detect AI provider from key prefix. Returns 'anthropic' or 'openai'."""
    if (api_key or "").startswith("sk-ant-"):
        return "anthropic"
    return "openai"


def ai_call(provider: str, model: str, system: str, user: str, api_key: str,
            base_url: str = None) -> str:
    """Make a single AI call and return the raw text response.

    Providers:
      - anthropic: prompt caching (ephemeral) on the system block.
      - openai:    json_object response_format for structured output.
      - local:     any OpenAI-compatible server (Ollama, LM Studio, vLLM, the
                   ai-gateway) via base_url. response_format is omitted because
                   many local servers reject it; callers rely on the lenient
                   parse_json_response fallback instead.
    Raises the provider SDK's exception on auth/API errors — callers catch these.
    """
    if provider == "anthropic":
        client = anthropic.Anthropic(api_key=api_key)
        response = client.messages.create(
            model=model,
            max_tokens=2048,
            messages=[{
                "role": "user",
                "content": [
                    {"type": "text", "text": system, "cache_control": {"type": "ephemeral"}},
                    {"type": "text", "text": user},
                ],
            }],
        )
        return response.content[0].text
    else:
        # openai and local both speak the OpenAI Chat Completions API.
        client = openai.OpenAI(api_key=api_key or "not-needed", base_url=base_url or None)
        kwargs = {
            "model": model,
            "messages": [
                {"role": "system", "content": system},
                {"role": "user",   "content": user},
            ],
        }
        if provider == "openai":
            kwargs["response_format"] = {"type": "json_object"}
        response = client.chat.completions.create(**kwargs)
        return response.choices[0].message.content


# ── Helpers ───────────────────────────────────────────────────────────────────

def get_api_key(body: dict) -> str | None:
    """Return API key from request body only. No server-side fallback."""
    return body.get("api_key") or None


# ── Credential resolution: owner unlock, local LLM, or BYO key ─────────────────
#
# The "api key" the UI submits can be one of three things:
#   1. OWNER_ACCESS_CODE  — a secret string set in the server .env. Typed into the
#      API-key field, it swaps in the server-side ANTHROPIC_API_KEY (owner's tokens)
#      and unlocks the owner's private resumes. Never stored, compared constant-time.
#   2. (with a base_url)  — a local OpenAI-compatible endpoint; no real key needed.
#   3. a normal provider key (sk-ant-... / sk-...).
#
# This keeps the hosted app fully generic for the public while letting the owner
# unlock a personalized experience on their own deployment.

OWNER_ACCESS_CODE = os.environ.get("OWNER_ACCESS_CODE", "").strip()

# Owner-mode rate limiter — an exposed endpoint must not let a leaked/guessed code
# drain the owner's tokens. Sliding window over the last OWNER_RATE_WINDOW seconds.
OWNER_RATE_MAX = int(os.environ.get("OWNER_RATE_MAX", "30"))
OWNER_RATE_WINDOW = int(os.environ.get("OWNER_RATE_WINDOW", "3600"))
_owner_calls = deque()
_owner_lock = threading.Lock()


def _owner_rate_ok() -> bool:
    """True if an owner-mode AI call is allowed under the sliding-window limit."""
    now = time.monotonic()
    with _owner_lock:
        while _owner_calls and now - _owner_calls[0] > OWNER_RATE_WINDOW:
            _owner_calls.popleft()
        if len(_owner_calls) >= OWNER_RATE_MAX:
            return False
        _owner_calls.append(now)
        return True


def resolve_credentials(submitted_key: str, base_url: str = "") -> dict | None:
    """Map a submitted key (+ optional local base URL) to effective credentials.

    Returns a dict {key, provider, base_url, is_owner, namespace} or None if no
    usable credential was supplied. `namespace` is the string hashed into key_hash
    so a user's resumes stay tied to them across sessions.
    """
    submitted_key = (submitted_key or "").strip()
    base_url = (base_url or "").strip()

    # 1. Owner unlock — constant-time compare; requires a server-side real key.
    owner_key = os.environ.get("ANTHROPIC_API_KEY", "").strip()
    if OWNER_ACCESS_CODE and owner_key and hmac.compare_digest(submitted_key, OWNER_ACCESS_CODE):
        return {"key": owner_key, "provider": "anthropic", "base_url": None,
                "is_owner": True, "namespace": "owner"}

    # 2. Local LLM — base URL points at an OpenAI-compatible server.
    if base_url:
        return {"key": submitted_key or "local", "provider": "local", "base_url": base_url,
                "is_owner": False, "namespace": "local:" + base_url}

    # 3. Bring-your-own provider key.
    if submitted_key:
        return {"key": submitted_key, "provider": detect_provider(submitted_key),
                "base_url": None, "is_owner": False, "namespace": submitted_key}

    return None


# ── Resume registry (flexible, nickname-keyed) ─────────────────────────────────

_ID_RE = re.compile(r"^[0-9a-f]{8}$")


def _resume_path(resume_id: str, ext: str) -> str:
    return os.path.join(app.config["RESUMES_DIR"], f"{resume_id}.{ext}")


def new_resume_id() -> str:
    return uuid.uuid4().hex[:8]


def load_resume(resume_id: str) -> dict | None:
    """Load a single resume record: {meta:{...}, parsed:{...}}. None if missing."""
    if not _ID_RE.match(resume_id or ""):
        return None
    path = _resume_path(resume_id, "json")
    if not os.path.exists(path):
        return None
    with open(path, encoding="utf-8") as f:
        return json.load(f)


def save_resume(record: dict) -> None:
    """Persist a resume record to data/resumes/{id}.json."""
    os.makedirs(app.config["RESUMES_DIR"], exist_ok=True)
    path = _resume_path(record["meta"]["id"], "json")
    with open(path, "w", encoding="utf-8") as f:
        json.dump(record, f, indent=2, ensure_ascii=False)


def _key_hash(api_key: str) -> str:
    return hashlib.sha256(api_key.encode()).hexdigest()


def list_resumes(namespace: str = "", is_owner: bool = False) -> list:
    """Return resume records visible to this caller, sorted by upload time.

    Visibility:
      - owner mode  -> every resume on disk (the owner's private instance).
      - public flag -> resumes marked meta.public are visible to everyone (the sample).
      - otherwise   -> only resumes whose key_hash matches this namespace.

    This closes the prior leak where a keyless request listed every resume.
    """
    target_hash = _key_hash(namespace) if namespace else None
    records = []
    for path in glob.glob(os.path.join(app.config["RESUMES_DIR"], "*.json")):
        try:
            with open(path, encoding="utf-8") as f:
                record = json.load(f)
        except (json.JSONDecodeError, OSError):
            continue
        meta = record.get("meta", {})
        if is_owner or meta.get("public") or (
            target_hash is not None and meta.get("key_hash") == target_hash
        ):
            records.append(record)
    records.sort(key=lambda r: r.get("meta", {}).get("uploaded_at", ""))
    return records


def delete_resume(resume_id: str) -> bool:
    """Remove both the .docx and .json for a resume. True if anything was deleted."""
    if not _ID_RE.match(resume_id or ""):
        return False
    removed = False
    for ext in ("docx", "json"):
        path = _resume_path(resume_id, ext)
        if os.path.exists(path):
            os.remove(path)
            removed = True
    return removed


def load_parsed(version: str) -> dict | None:
    """Load parsed base-resume JSON. Returns None if not uploaded yet."""
    path = os.path.join(app.config["DATA_DIR"], f"{version}.json")
    if not os.path.exists(path):
        return None
    with open(path, encoding="utf-8") as f:
        return json.load(f)


def parse_json_response(raw: str) -> dict:
    """Strip accidental markdown fences and parse JSON.

    Falls back to extracting the first {...} block, which makes local models that
    wrap their JSON in explanatory prose usable without a strict response_format.
    """
    text = raw.strip()
    text = re.sub(r"^```(?:json)?\s*", "", text)
    text = re.sub(r"\s*```$", "", text.strip())
    text = text.strip()
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        match = re.search(r"\{.*\}", text, re.DOTALL)
        if match:
            return json.loads(match.group(0))
        raise


def build_resume_bullets_context(record: dict) -> str:
    """Serialize full bullets from a resume record into a prompt string for Tailor."""
    parsed = record.get("parsed", {})
    meta = record.get("meta", {})
    lines = [f"Resume: {meta.get('nickname', 'unknown')} — {meta.get('description', '')}"]
    for role in parsed.get("experience", []):
        lines.append(f"\n{role['company']} | {role['title']} | {role['dates']}")
        for i, bullet in enumerate(role["bullets"]):
            lines.append(f"  [{i}] {bullet}")
    return "\n".join(lines)


def apply_edits(record: dict, approved_edits: list, bullet_count: int) -> list:
    """Pure-code bullet application: substitute approved edits, trim to bullet_count.

    Returns roles_override list compatible with build_pdf().
    approved_edits items: {role, bullet_index, original, suggested}
    """
    edit_map = {}
    for edit in approved_edits:
        key = (edit["role"], int(edit["bullet_index"]))
        edit_map[key] = edit["suggested"]

    roles_override = []
    for role in record["parsed"]["experience"]:
        company = role["company"]
        base_bullets = role["bullets"]
        count = min(bullet_count, len(base_bullets))
        bullets = []
        for i, bullet in enumerate(base_bullets[:count]):
            bullets.append(edit_map.get((company, i), bullet))
        roles_override.append({
            "company": company,
            "title": role["title"],
            "location": role.get("location", ""),
            "dates": role["dates"],
            "bullets": bullets,
        })
    return roles_override


# ── System prompts ─────────────────────────────────────────────────────────────

SUMMARY_SYSTEM_PROMPT = """\
You are a resume analyst. Given a resume's bullets and metadata, write a 2-3 sentence
summary capturing the candidate's functional domain, seniority level, and the types of
roles this resume is optimized for. Be specific — this summary will be used to match the
resume against job descriptions.

Return ONLY valid JSON:
{"summary": "<2-3 sentences>"}"""

DIGEST_SYSTEM_PROMPT = """\
You are a resume distiller. Given a resume's bullets and metadata, produce a compact
fact sheet that another model will use as the ONLY source of truth when writing a cover
letter. Keep it tight — aim for roughly 120-180 words.

Capture, per employer (most recent first):
- Employer name, role title, and dates (copy exactly).
- The 2-4 most important achievements, preserving every metric.
- Core skills / domains demonstrated.

Hard rules:
- Copy all numbers, percentages, dollar amounts, and employer names EXACTLY as written.
- Never invent, round, paraphrase, or estimate a metric. If a fact is not in the resume,
  do not include it.
- Plain prose or short lines — no markdown headers.

Return ONLY valid JSON:
{"digest": "<the fact sheet>"}"""

TRIAGE_SYSTEM_PROMPT = """\
You are a resume strategy expert. You will receive a job description and a list of the
candidate's resumes (each with a nickname, description, and summary). Pick the single
best-fit resume for the JD and determine whether it needs bullet edits.

Rules:
- Pick based on domain alignment: match the JD's function/industry to the resume best
  suited for it, using the summaries as your signal.
- verdict = "use_as_is" if the resume already aligns well (fit_score >= 75 and no critical
  missing keywords). Otherwise verdict = "needs_edits".
- fit_score: 0-100 integer reflecting how well the chosen resume matches the JD as-is.
- target_keywords: 5-8 keywords from the JD that matter most for this role.
- missing_keywords: keywords from target_keywords NOT already present in the resume summary.

Return ONLY valid JSON — no markdown, no explanation:
{
  "recommended_resume_id": "<id>",
  "verdict": "use_as_is" | "needs_edits",
  "role_fit": "high" | "medium" | "low",
  "fit_score": <integer 0-100>,
  "rationale": "<one sentence explaining the pick>",
  "target_keywords": ["<keyword>", ...],
  "missing_keywords": ["<keyword>", ...]
}"""

TAILOR_SYSTEM_PROMPT = """\
You are a resume editor. Given a candidate's resume bullets and a job description, suggest
specific, surgical bullet edits to improve keyword alignment.

{resume_context}

Rules:
- Suggest edits only where a JD keyword integrates naturally without changing the meaning.
- Never alter a metric (number, percentage, dollar amount).
- Never change the opening action verb unless strictly necessary.
- No em dashes. No AI-pattern language: "leveraged," "spearheaded," "robust," "synergies."
- Suggest a maximum of 4 bullet edits — prioritize highest-impact changes.
- If a bullet already covers the keyword well, do not suggest an edit.
- Deviation level {deviation}: 0.0 = near-verbatim; 1.0 = fuller JD-aligned reframe.

Return ONLY valid JSON — no markdown, no explanation:
{{
  "company_name": "<hiring company name extracted from the JD, or null if unclear>",
  "bullet_edits": [
    {{
      "role": "<company name matching resume exactly>",
      "bullet_index": <0-based int>,
      "original": "<exact original bullet text>",
      "suggested": "<rewritten bullet>",
      "rationale": "<one sentence>"
    }}
  ]
}}\
"""


# ── Routes ─────────────────────────────────────────────────────────────────────

@app.route("/")
def index():
    return render_template("index.html")


@app.route("/download/<filename>")
def download(filename):
    if not re.match(r'^[\w][\w_-]{0,80}\.pdf$', filename):
        return jsonify({"error": "invalid filename"}), 400
    return send_from_directory(app.config["OUTPUT_DIR"], filename, as_attachment=True)


@app.route("/api/key-status")
def key_status():
    return jsonify({"has_key": False, "provider": None})


@app.route("/api/models")
def get_models():
    """Return the curated model lists for all providers."""
    return jsonify(PROVIDER_MODELS)


@app.route("/api/detect-provider", methods=["POST"])
def detect_provider_route():
    """Detect provider from a submitted API key without storing it."""
    body = request.get_json(force=True)
    key = body.get("api_key", "")
    if not key:
        return jsonify({"error": "api_key required"}), 400
    return jsonify({"provider": detect_provider(key)})


@app.route("/api/parsed/<version>")
def get_parsed(version):
    """Return the parsed structure for a given resume version (for UI preview)."""
    if version not in ("301", "303", "701"):
        return jsonify({"error": "invalid version"}), 400
    parsed = load_parsed(version)
    if not parsed:
        return jsonify({"error": f"No base resume uploaded for version {version}"}), 404
    # Return a summary (not full bullets) for UI display
    summary = {
        "version": version,
        "candidate": parsed["candidate"],
        "education": parsed["education"],
        "experience": [
            {"company": r["company"], "title": r["title"], "dates": r["dates"], "bullet_count": len(r["bullets"])}
            for r in parsed["experience"]
        ],
        "skills_categories": [s["category"] for s in parsed.get("skills", [])],
    }
    return jsonify(summary)


@app.route("/api/candidate", methods=["PATCH"])
def update_candidate():
    """
    Update candidate identity fields (email, phone, location, linkedin_url) across
    all uploaded resume versions. These fields are user-provided and not extracted
    from the docx (especially linkedin_url).
    Body: {linkedin_url?, email?, phone?, location?}
    """
    body = request.get_json(force=True)
    allowed = {"linkedin_url", "email", "phone", "location"}
    updates = {k: v for k, v in body.items() if k in allowed}
    if not updates:
        return jsonify({"error": "No valid fields provided"}), 400

    updated = []
    for version in ("301", "303", "701"):
        parsed = load_parsed(version)
        if not parsed:
            continue
        parsed["candidate"].update(updates)
        path = os.path.join(app.config["DATA_DIR"], f"{version}.json")
        with open(path, "w", encoding="utf-8") as f:
            json.dump(parsed, f, indent=2, ensure_ascii=False)
        updated.append(version)

    return jsonify({"status": "ok", "updated_versions": updated, "fields": updates})


@app.route("/api/resumes")
def get_resumes():
    """List resumes visible to the requesting credential (metadata only, no bullets)."""
    creds = resolve_credentials(request.args.get("api_key", ""), request.args.get("base_url", ""))
    if not creds:
        # No credential: still surface public samples so the UI isn't empty.
        return jsonify({"resumes": [
            {
                "id": m.get("id"), "nickname": m.get("nickname"),
                "description": m.get("description"), "has_summary": bool(m.get("summary")),
                "roles": len(r.get("parsed", {}).get("experience", [])),
                "uploaded_at": m.get("uploaded_at"),
            }
            for r in list_resumes() for m in [r.get("meta", {})]
        ]})
    out = []
    for r in list_resumes(creds["namespace"], creds["is_owner"]):
        m = r.get("meta", {})
        exp = r.get("parsed", {}).get("experience", [])
        out.append({
            "id": m.get("id"),
            "nickname": m.get("nickname"),
            "description": m.get("description"),
            "has_summary": bool(m.get("summary")),
            "roles": len(exp),
            "uploaded_at": m.get("uploaded_at"),
        })
    return jsonify({"resumes": out})


@app.route("/api/resumes/<resume_id>", methods=["DELETE"])
def remove_resume(resume_id):
    creds = resolve_credentials(request.args.get("api_key", ""), request.args.get("base_url", ""))
    record = load_resume(resume_id)
    if not record:
        return jsonify({"error": "resume not found"}), 404
    if not creds:
        return jsonify({"error": "No API key available."}), 401
    # Owner can delete any resume; everyone else only their own (never the public sample).
    if not creds["is_owner"] and record.get("meta", {}).get("key_hash") != _key_hash(creds["namespace"]):
        return jsonify({"error": "not authorized"}), 403
    if delete_resume(resume_id):
        return jsonify({"status": "ok", "deleted": resume_id})
    return jsonify({"error": "resume not found"}), 404


@app.route("/upload-resume", methods=["POST"])
def upload_resume():
    """
    Accept a .docx upload with nickname + one-liner description, parse it, and store
    it in the flexible resume registry. If api_key + summary_model are provided,
    generates a summary immediately (one AI call). Otherwise stores summary=null and
    generates it lazily on first /evaluate.
    Form fields: nickname, description, file (.docx), api_key (optional), summary_model (optional).
    """
    nickname = request.form.get("nickname", "").strip()
    description = request.form.get("description", "").strip()
    creds = resolve_credentials(request.form.get("api_key", ""), request.form.get("base_url", ""))
    summary_model = request.form.get("summary_model", "claude-haiku-4-5-20251001").strip()

    if not nickname:
        return jsonify({"error": "nickname is required"}), 400
    if "file" not in request.files:
        return jsonify({"error": "No file provided"}), 400
    file = request.files["file"]
    if not file.filename.endswith(".docx"):
        return jsonify({"error": "Only .docx files are accepted"}), 400

    resume_id = new_resume_id()
    os.makedirs(app.config["RESUMES_DIR"], exist_ok=True)
    docx_path = _resume_path(resume_id, "docx")
    file.save(docx_path)

    try:
        parsed = parse_resume(docx_path)
    except Exception as e:
        os.remove(docx_path)
        return jsonify({"error": f"Parse failed: {str(e)}"}), 422

    # Generate summary if a credential is available.
    summary = None
    if creds:
        try:
            bullets_text = build_resume_bullets_context({
                "meta": {"nickname": nickname, "description": description},
                "parsed": parsed,
            })
            user_msg = f"Nickname: {nickname}\nDescription: {description}\n\n{bullets_text}"
            raw = ai_call(creds["provider"], summary_model, SUMMARY_SYSTEM_PROMPT, user_msg,
                          creds["key"], creds["base_url"])
            summary = parse_json_response(raw).get("summary")
        except Exception:
            summary = None  # Non-fatal — summary generated lazily on evaluate

    record = {
        "meta": {
            "id": resume_id,
            "nickname": nickname,
            "description": description,
            "summary": summary,
            "key_hash": _key_hash(creds["namespace"]) if creds else None,
            "uploaded_at": datetime.now(timezone.utc).isoformat(),
        },
        "parsed": parsed,
    }
    save_resume(record)

    return jsonify({
        "status": "ok",
        "id": resume_id,
        "nickname": nickname,
        "summary_generated": summary is not None,
        "roles": len(parsed["experience"]),
        "education": len(parsed["education"]),
        "skills_categories": [s["category"] for s in parsed.get("skills", [])],
    })


def _ensure_summaries(records: list, creds: dict, summary_model: str) -> None:
    """Lazily generate and persist summaries for any records missing one."""
    if not creds:
        return
    for record in records:
        if record["meta"].get("summary"):
            continue
        try:
            meta = record["meta"]
            bullets_text = build_resume_bullets_context(record)
            user_msg = f"Nickname: {meta['nickname']}\nDescription: {meta.get('description','')}\n\n{bullets_text}"
            raw = ai_call(creds["provider"], summary_model, SUMMARY_SYSTEM_PROMPT, user_msg,
                          creds["key"], creds["base_url"])
            record["meta"]["summary"] = parse_json_response(raw).get("summary")
            save_resume(record)
        except Exception:
            pass  # Missing summary is non-fatal; triage will use description instead


def _ensure_digests(records: list, creds: dict, digest_model: str) -> None:
    """Lazily generate and persist a compact candidate digest for any record missing one.

    The digest is the token-efficient fact sheet used to ground cover-letter generation.
    One AI call per resume, once — cached in record["meta"]["digest"].
    """
    if not creds:
        return
    for record in records:
        if record["meta"].get("digest"):
            continue
        try:
            meta = record["meta"]
            bullets_text = build_resume_bullets_context(record)
            user_msg = f"Nickname: {meta['nickname']}\nDescription: {meta.get('description','')}\n\n{bullets_text}"
            raw = ai_call(creds["provider"], digest_model, DIGEST_SYSTEM_PROMPT, user_msg,
                          creds["key"], creds["base_url"])
            record["meta"]["digest"] = parse_json_response(raw).get("digest")
            save_resume(record)
        except Exception:
            pass  # Missing digest is non-fatal; cover-letter falls back to full bullets


@app.route("/evaluate", methods=["POST"])
def evaluate():
    """
    Three evaluation paths based on what's uploaded and what's requested:

    1. Override re-evaluate (resume_id provided): skip triage, run Tailor on that resume.
    2. Single resume: skip triage, run Tailor on the only resume.
    3. Multiple resumes: run Triage (pick best), then Tailor if verdict = needs_edits.

    Returns merged fit assessment + bullet_edits (empty list if use_as_is).
    """
    body = request.get_json(force=True)
    jd_text = body.get("jd_text", "").strip()
    creds = resolve_credentials(get_api_key(body), body.get("base_url", ""))
    eval_model  = body.get("eval_model",  "claude-sonnet-4-6").strip()
    edit_model  = body.get("edit_model",  "claude-haiku-4-5-20251001").strip()
    deviation   = float(body.get("deviation", 0.3))
    resume_id   = body.get("resume_id", "").strip() or None  # explicit override

    if not jd_text:
        return jsonify({"error": "jd_text is required"}), 400
    if not creds:
        return jsonify({"error": "No API key available."}), 401
    if creds["is_owner"] and not _owner_rate_ok():
        return jsonify({"error": "Owner-mode rate limit reached. Try again later."}), 429

    provider = creds["provider"]
    records = list_resumes(creds["namespace"], creds["is_owner"])
    if not records:
        return jsonify({"error": "No resumes uploaded. Add at least one resume in Settings."}), 404

    # ── Lazy summary + digest generation ───────────────────────────────────────
    _ensure_summaries(records, creds, edit_model)
    _ensure_digests(records, creds, edit_model)
    records = list_resumes(creds["namespace"], creds["is_owner"])  # reload after potential updates

    try:
        # ── Path A: override — caller specifies which resume ─────────────────
        if resume_id:
            record = next((r for r in records if r["meta"]["id"] == resume_id), None)
            if not record:
                return jsonify({"error": f"Resume {resume_id} not found."}), 404
            triage = {
                "recommended_resume_id": resume_id,
                "verdict": "needs_edits",
                "role_fit": None,
                "fit_score": None,
                "rationale": "Manual override — re-evaluating for selected resume.",
                "target_keywords": [],
                "missing_keywords": [],
            }

        # ── Path B: single resume — skip triage ──────────────────────────────
        elif len(records) == 1:
            record = records[0]
            triage = {
                "recommended_resume_id": record["meta"]["id"],
                "verdict": "needs_edits",
                "role_fit": None,
                "fit_score": None,
                "rationale": "Only one resume uploaded — skipping triage.",
                "target_keywords": [],
                "missing_keywords": [],
            }

        # ── Path C: multiple resumes — run Triage ────────────────────────────
        else:
            resume_list = [
                {
                    "id": r["meta"]["id"],
                    "nickname": r["meta"]["nickname"],
                    "description": r["meta"].get("description", ""),
                    "summary": r["meta"].get("summary") or r["meta"].get("description", ""),
                }
                for r in records
            ]
            triage_user = (
                f"Available resumes:\n{json.dumps(resume_list, indent=2)}\n\n"
                f"Job description:\n{jd_text}"
            )
            raw = ai_call(provider, eval_model, TRIAGE_SYSTEM_PROMPT, triage_user,
                          creds["key"], creds["base_url"])
            triage = parse_json_response(raw)
            record = next(
                (r for r in records if r["meta"]["id"] == triage["recommended_resume_id"]),
                records[0],
            )

        # ── Tailor if needed ──────────────────────────────────────────────────
        bullet_edits = []
        if triage.get("verdict") != "use_as_is":
            resume_context = build_resume_bullets_context(record)
            tailor_system = TAILOR_SYSTEM_PROMPT.format(
                resume_context=resume_context,
                deviation=deviation,
            )
            raw = ai_call(provider, edit_model, tailor_system, f"Job description:\n{jd_text}",
                          creds["key"], creds["base_url"])
            tailor_result = parse_json_response(raw)
            bullet_edits = tailor_result.get("bullet_edits", [])
            company_name = tailor_result.get("company_name") or None
        else:
            company_name = None

        return jsonify({
            "recommended_resume_id": triage["recommended_resume_id"],
            "recommended_nickname": record["meta"]["nickname"],
            "verdict": triage.get("verdict"),
            "role_fit": triage.get("role_fit"),
            "fit_score": triage.get("fit_score"),
            "rationale": triage.get("rationale"),
            "target_keywords": triage.get("target_keywords", []),
            "missing_keywords": triage.get("missing_keywords", []),
            "bullet_edits": bullet_edits,
            "company_name": company_name,
        })

    except (anthropic.AuthenticationError, openai.AuthenticationError):
        return jsonify({"error": "Invalid API key."}), 401
    except json.JSONDecodeError as e:
        return jsonify({"error": "evaluation_parse_error", "detail": str(e)}), 502
    except (anthropic.APIError, openai.APIError) as e:
        return jsonify({"error": "api_error", "detail": str(e)}), 502


@app.route("/generate", methods=["POST"])
def generate():
    """
    Pure-code PDF generation — zero AI calls.
    Body: {resume_id, approved_edits, bullet_count, line_fill_pct, deviation,
           target_company?, custom_filename?}
    Filename: CandidateName_TargetCompany_YYYY-MM-DD.pdf (falls back to resume nickname).
    Pass custom_filename (stem without .pdf) to override the generated name entirely.
    """
    try:
        body = request.get_json(force=True)
        resume_id       = body.get("resume_id", "").strip()
        approved_edits  = body.get("approved_edits", [])
        target_company  = body.get("target_company", "").strip() or None
        custom_filename = body.get("custom_filename", "").strip() or None
        settings = {
            "bullet_count": int(body.get("bullet_count", 3)),
            "line_fill_pct": int(body.get("line_fill_pct", 100)),
            "deviation": float(body.get("deviation", 0.3)),
        }

        if not resume_id:
            return jsonify({"error": "resume_id is required"}), 400

        record = load_resume(resume_id)
        if not record:
            return jsonify({"error": f"Resume {resume_id} not found."}), 404

        roles_override = apply_edits(record, approved_edits, settings["bullet_count"])

        filename, pdf_error, raw_warnings = build_pdf(
            record["parsed"], roles_override, settings,
            nickname=record["meta"]["nickname"],
            target_company=target_company,
            custom_filename=custom_filename,
        )
        if pdf_error:
            return jsonify({"error": "pdf_render_error", "detail": pdf_error}), 502

        warnings = [
            w["warn"] if isinstance(w, dict) else str(w)
            for w in (raw_warnings or [])
        ]

        return jsonify({
            "filename": filename,
            "resume_id": resume_id,
            "nickname": record["meta"]["nickname"],
            "edits_applied": len(approved_edits),
            "warnings": warnings,
        })
    except Exception as e:
        return jsonify({"error": str(e)}), 500


COVER_LETTER_SYSTEM_PROMPT = """\
You are a professional cover letter writer. You write a tailored cover letter body for a
candidate applying to a specific role, grounded ONLY in the candidate fact sheet provided.

The candidate fact sheet (the ONLY source of truth about the candidate):
{candidate_digest}

Your task:
1. Read the job description. Extract the hiring company name (firm) and the role title.
2. Write a cover letter body (3-4 short paragraphs) that connects the candidate's real
   experience to what this role needs.

Hard rules:
- Use ONLY facts present in the candidate fact sheet. Never invent employers, metrics,
  dates, or achievements. Copy any number, percentage, or dollar amount exactly.
- No em dashes anywhere.
- No AI-pattern language: "leveraged," "spearheaded," "robust," "synergies."
- Never include commentary, caveats, meta-discussion, or notes to the reader about fit.
  Never refuse. Always produce a usable letter, even if the role is an imperfect match —
  emphasize the most transferable experience.
- No salutation and no signature; the user adds "Dear [Hiring Manager]," and their name.

Return ONLY valid JSON in exactly this shape, nothing else:
{{"firm": "<hiring company from JD>", "role": "<role title from JD>", "body": "<letter body>"}}"""


@app.route("/cover-letter", methods=["POST"])
def cover_letter():
    """
    Generate a resume-grounded cover letter. The AI extracts firm + role from the JD and
    writes the body using only the candidate's cached digest (or full bullets as fallback).
    Body: {jd_text, api_key, model?, resume_id?}
    Returns: {cover_letter: "<body>", firm: "<...>", role: "<...>", resume_id, nickname}
    """
    body = request.get_json(force=True)
    jd_text   = body.get("jd_text", "").strip()
    creds     = resolve_credentials(get_api_key(body), body.get("base_url", ""))
    model     = body.get("model", "claude-haiku-4-5-20251001").strip()
    resume_id = body.get("resume_id", "").strip() or None

    if not jd_text:
        return jsonify({"error": "jd_text is required"}), 400
    if not creds:
        return jsonify({"error": "No API key available."}), 401
    if creds["is_owner"] and not _owner_rate_ok():
        return jsonify({"error": "Owner-mode rate limit reached. Try again later."}), 429

    provider = creds["provider"]
    records = list_resumes(creds["namespace"], creds["is_owner"])
    if not records:
        return jsonify({"error": "No resumes uploaded. Add at least one resume in Settings."}), 404

    try:
        # ── Resume selection (mirrors /evaluate) ─────────────────────────────
        if resume_id:
            record = next((r for r in records if r["meta"]["id"] == resume_id), None)
            if not record:
                return jsonify({"error": f"Resume {resume_id} not found."}), 404
        elif len(records) == 1:
            record = records[0]
        else:
            # Multiple resumes, no explicit pick — triage to choose the best one.
            _ensure_summaries(records, creds, model)
            records = list_resumes(creds["namespace"], creds["is_owner"])
            resume_list = [
                {
                    "id": r["meta"]["id"],
                    "nickname": r["meta"]["nickname"],
                    "description": r["meta"].get("description", ""),
                    "summary": r["meta"].get("summary") or r["meta"].get("description", ""),
                }
                for r in records
            ]
            triage_user = (
                f"Available resumes:\n{json.dumps(resume_list, indent=2)}\n\n"
                f"Job description:\n{jd_text}"
            )
            raw = ai_call(provider, model, TRIAGE_SYSTEM_PROMPT, triage_user,
                          creds["key"], creds["base_url"])
            triage = parse_json_response(raw)
            record = next(
                (r for r in records if r["meta"]["id"] == triage.get("recommended_resume_id")),
                records[0],
            )

        # ── Ground on the cached digest (fall back to full bullets) ──────────
        _ensure_digests([record], creds, model)
        record = load_resume(record["meta"]["id"]) or record
        candidate_digest = record["meta"].get("digest") or build_resume_bullets_context(record)

        system   = COVER_LETTER_SYSTEM_PROMPT.replace("{candidate_digest}", candidate_digest)
        user_msg = f"Job description:\n{jd_text}\n\nWrite the cover letter now. Return only the JSON."

        raw    = ai_call(provider, model, system, user_msg, creds["key"], creds["base_url"])
        result = parse_json_response(raw)

        return jsonify({
            "cover_letter": (result.get("body") or "").strip(),
            "firm": (result.get("firm") or "").strip(),
            "role": (result.get("role") or "").strip(),
            "resume_id": record["meta"]["id"],
            "nickname": record["meta"]["nickname"],
        })

    except (anthropic.AuthenticationError, openai.AuthenticationError):
        return jsonify({"error": "Invalid API key."}), 401
    except json.JSONDecodeError as e:
        return jsonify({"error": "cover_letter_parse_error", "detail": str(e)}), 502
    except (anthropic.APIError, openai.APIError) as e:
        return jsonify({"error": "api_error", "detail": str(e)}), 502


if __name__ == "__main__":
    # debug defaults off; set FLASK_DEBUG=1 in .env for local development only.
    debug = os.environ.get("FLASK_DEBUG", "").strip() in ("1", "true", "True")
    port = int(os.environ.get("PORT", "5001"))
    app.run(host="0.0.0.0", port=port, debug=debug)
