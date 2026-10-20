# StockPilot AI - Local-First Investment Command Center

Originally built for **AI Builder Cup 2026 (Google Cloud JAPAC, BFSI theme)**. Now being extended for the **Meta Global AI Developer Hackathon** with **Meta Muse** models as an additional AI backend.

Live demo: https://stockpilot-ai-1058522544353.asia-east1.run.app/

StockPilot helps Taiwan retail investors who juggle several broker apps keep one clean ledger, without uploading their private holdings to a server. The ledger lives in the browser. Gemini is called statelessly for OCR, briefings, and a three-brain majority vote.

## What it does

- **Gemini multimodal OCR**: read a broker fill/confirmation screenshot (ticker, side, price, quantity, fees). The user confirms before anything is written.
- **FIFO P&L**: realized and unrealized P&L tracked per lot in the browser-local ledger.
- **AI briefing**: structured insights, concentration risk, and actionable reminders for the current holdings.
- **MAGI majority vote**: three role-brains (Melchior: data/fundamentals, Balthasar: risk/protection, Casper: discipline/behavior) vote independently. The app shows the tally, the majority decision, dissent, and suggested actions, so the reasoning is inspectable instead of a single black-box answer.
- **Privacy**: private ledgers start empty, judge demo holdings are kept separate, and one tap masks all amounts.

## Architecture

- Backend: Python FastAPI on **Google Cloud Run** (`asia-east1`)
- AI: **Gemini Flash** family with multi-model fallback; a deterministic demo fallback runs when no API key is configured
- Frontend: single-page mobile web app (`mobile.html`) with a browser-local ledger

### API

| Method | Path | Purpose |
|---|---|---|
| GET | `/` | Mobile web app |
| GET | `/api/stocks/portfolio` | Demo portfolio |
| POST | `/api/stocks/ocr` | Gemini OCR of a broker screenshot |
| POST | `/api/stocks/ai-brief` | Structured AI briefing |
| POST | `/api/stocks/magi` | MAGI three-brain majority vote |
| GET | `/healthz` | Health check |

## 30-second demo path

1. Open the live URL and load the judge demo holdings.
2. Open the AI briefing.
3. Tap MAGI Vote to see the 2:1 banner, three brain cards, dissent, and actions.
4. Tap the eye icon to mask amounts.

## Run locally

```bash
pip install -r requirements.txt
export GEMINI_API_KEY=your_key   # optional; demo fallback runs without it
uvicorn app:app --host 0.0.0.0 --port 8080
```

## Deploy

```bash
gcloud run deploy stockpilot-ai --source . --region=asia-east1 --allow-unauthenticated
```

Set `GEMINI_API_KEY` as a Cloud Run environment variable or Secret Manager secret. Never commit keys.

## Roadmap

- Meta Global AI Developer Hackathon: add Meta Muse models alongside Gemini (pluggable model backend for OCR, briefings, and MAGI).

## Disclaimer

For research and demonstration only. Not investment advice.
