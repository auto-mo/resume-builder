# Resume Builder

A self-hosted tool that turns a job description into a tailored, one-page PDF
resume. Paste a JD, let it pick the best-fit resume from your library and
suggest surgical bullet edits, then render a clean ATS-friendly PDF. It can also
draft a resume-grounded cover letter.

You bring your own AI key (Anthropic or OpenAI), or point it at a local model.
Nothing is sent anywhere except the provider you choose.

> **Scope:** this is a local / single-user tool. It is not a multi-tenant SaaS —
> resume isolation is keyed off your API key as a convenience, not a hard
> security boundary. Run it for yourself, on your machine or your own server.

## Features

- **JD evaluation** — fit assessment, role match, and keyword gaps.
- **Auto-triage** — picks the best resume from your library for the role.
- **Surgical bullet edits** — suggestions you approve before they land; never a
  full rewrite, never more than one page.
- **One-page PDF** — Times New Roman, ATS-friendly reading order, via headless
  Chromium.
- **Cover letters** — grounded in your own resume content.
- **Bring your own model** — Anthropic, OpenAI, or any OpenAI-compatible local
  server (Ollama, LM Studio, vLLM).

## Quickstart (Docker — recommended)

Docker handles the trickiest dependency: headless Chromium plus fonts.

```bash
cp .env.example .env          # can stay blank for bring-your-own-key use
docker compose up --build
```

Open http://localhost:5001, click the gear, and add your API key (or a local
endpoint). Upload a resume `.docx` in Settings, paste a JD, and build.

## Quickstart (local Python)

```bash
pip install -r requirements.txt
playwright install chromium
python app.py                 # http://localhost:5001
```

## Using a model

Open **Settings** (gear icon) and choose one:

- **Anthropic / OpenAI** — paste your key (`sk-ant-...` or `sk-...`). It is sent
  with each request from your browser and never stored on the server.
- **Local model** — leave the key blank and enter an OpenAI-compatible endpoint,
  e.g. `http://localhost:11434/v1` for Ollama, plus a model name like
  `llama3.1`. Runs fully offline. Larger models give more reliable structured
  output; small ones may occasionally need a retry.

Get a key: [Anthropic Console](https://console.anthropic.com/settings/keys) ·
[OpenAI Platform](https://platform.openai.com/api-keys).

## Your resume data

- Upload resumes through the Settings panel (`.docx`). They are parsed and stored
  in `data/resumes/` as JSON, tagged to your key so only you see them.
- A fictional **sample** resume ships so the app works out of the box. Delete it
  once you've added your own.
- `data/`, `outputs/`, `.env`, and `*.docx` are gitignored — your content stays
  local and is never committed.

## Owner mode (optional, for a personalized deployment)

If you host this for yourself, you can set `OWNER_ACCESS_CODE` in `.env`. Typing
that code into the API-key field swaps in the server's `ANTHROPIC_API_KEY` and
unlocks your private resumes — so the public sees a generic, bring-your-own-key
tool while you get your full setup on your own tokens. Owner mode is
rate-limited (`OWNER_RATE_MAX` / `OWNER_RATE_WINDOW`) so an exposed endpoint
can't drain your tokens. Leave the code blank to disable owner mode entirely.

## How it works

- **Flask** backend (`app.py`), **Playwright + Chromium** for PDF rendering
  (`resume_maker.py`), **python-docx** for parsing uploads (`resume_parser.py`).
- AI is used only for decisions (triage, edit suggestions, summaries, cover-letter
  prose). PDF generation itself makes zero AI calls.
- Cover-letter tone/structure templates live in `cover_letter_kb.yaml` — copy
  `cover_letter_kb.example.yaml` to create your own.

## License

MIT — see [LICENSE](LICENSE).
