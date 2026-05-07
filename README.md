# VeraCarbon Backend

**AI-powered carbon credit verification engine combining satellite remote sensing with large language models.**

Live API: `https://veracarbon-backend.onrender.com`  
Frontend: [veracarbon.com](https://veracarbon.com)

---

## What It Does

VeraCarbon's backend is the core verification engine that powers independent carbon credit auditing. It does three things:

1. **Satellite Verification** — Pulls real ESA Sentinel-2 and Sentinel-1 SAR imagery from Google Earth Engine and computes a 7-pillar Carbon Credit Integrity Score (CCIS) for any coordinates and date range.
2. **Document Analysis** — Accepts uploaded carbon project PDFs and uses Claude AI to extract all developer claims: project area, sequestration figures, baseline methodology, coordinates, and certification status.
3. **Fraud Audit** — Cross-references extracted document claims against live satellite evidence and returns a fraud probability score, risk classification, and a list of specific discrepancies.

---

## Tech Stack

| Layer | Technology |
|---|---|
| Runtime | Python 3.11 |
| Framework | Flask |
| Satellite Data | Google Earth Engine (Sentinel-2, Sentinel-1 SAR, Hansen Global Forest Change) |
| AI / LLM | Anthropic Claude Sonnet via API |
| Deployment | Render (free tier, auto-deploy from GitHub) |
| Auth | GEE Service Account + Anthropic API Key (env vars) |

---

## API Endpoints

### `POST /api/verify`
Runs satellite-based CCIS computation for a given location and time range.

**Request body:**
```json
{
  "latitude": 1.3521,
  "longitude": 103.8198,
  "start_date": "2019-01-01",
  "end_date": "2023-12-31",
  "project_area_ha": 50000
}
```

**Response:**
```json
{
  "ccis_score": 8.2,
  "fraud_status": "flagged",
  "confidence": "low",
  "ndvi_change": -0.105,
  "sar_change": -0.406,
  "scenes_analyzed": 18,
  "recommendation": "Reject",
  "pillars": {
    "vegetation_change": 0,
    "permanence_risk": 0,
    "additionality": 0,
    "leakage_buffer": 0,
    "temporal_consistency": 0,
    "data_confidence": 0,
    "biomass_proxy": 8.2
  }
}
```

---

### `POST /api/analyze-document`
Accepts a PDF and extracts all project claims using Claude AI.

**Request:** `multipart/form-data` with a `file` field containing the PDF.

**Response:**
```json
{
  "project_name": "Amazon Rainforest Conservation Project",
  "claimed_area_ha": 50000,
  "claimed_sequestration_tonnes": 500000,
  "coordinates": [-3.46, -62.21],
  "certification_body": "Verra VCS",
  "project_start_year": 2019,
  "methodology": "VM0015",
  "raw_claims": "..."
}
```

---

### `POST /api/fraud-audit`
Cross-references document claims against satellite data and returns a fraud verdict.

**Request body:**
```json
{
  "document_claims": { ... },
  "satellite_data": { ... }
}
```

**Response:**
```json
{
  "fraud_probability": 75,
  "risk_classification": "High",
  "audit_summary": "This project cannot be verified due to unavailable satellite data...",
  "discrepancies": [
    "Satellite data unavailable — cannot verify the claimed 50,000 hectares of tropical rainforest area",
    "Cannot validate the claimed 500,000 tonnes CO2 sequestration without satellite evidence",
    "Unable to verify existence or density of claimed 3,000,000 trees"
  ]
}
```

---

## CCIS — 7-Pillar Scoring Methodology

The Carbon Credit Integrity Score (CCIS) is computed from seven satellite-derived indicators:

| Pillar | Data Source | Weight |
|---|---|---|
| Vegetation Change (NDVI Delta) | Sentinel-2 | 25% |
| Permanence Risk | Hansen Forest Loss | 20% |
| Additionality Baseline | Sentinel-2 historical | 15% |
| Leakage Buffer | Regional NDVI comparison | 15% |
| Temporal Consistency | Multi-year time series | 10% |
| Data Confidence | Scene count + cloud cover | 10% |
| Biomass Proxy (SAR) | Sentinel-1 SAR | 5% |

Scores below 50 trigger a **Reject** recommendation. Scores 50–75 are **Review**. Above 75 is **Approve**.

---

## Local Setup

```bash
git clone https://github.com/JudyAlsharaaju/veracarbon-backend
cd veracarbon-backend
pip install -r requirements.txt
```

Create a `.env` file:
```
GEE_SERVICE_ACCOUNT=your-service-account@project.iam.gserviceaccount.com
GEE_KEY_JSON={"type": "service_account", ...}
ANTHROPIC_API_KEY=sk-ant-...
```

Run locally:
```bash
python app.py
```

---

## Deployment

Deployed on Render with auto-deploy from this GitHub repo. Environment variables (`GEE_KEY_JSON`, `GEE_SERVICE_ACCOUNT`, `ANTHROPIC_API_KEY`) are stored securely in Render's dashboard — never committed to code.

---

## Project Context

VeraCarbon was built to address a real problem: carbon credits are largely unverified, and the market is rife with greenwashing. Independent satellite verification has been technically possible for years but has never been packaged into an accessible, automated tool. This backend is the engine that makes that possible.

Built by Judy Alsharaa — Abu Dhabi, UAE.
