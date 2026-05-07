import base64
import json

import anthropic
from flask import Flask, jsonify, request
from flask_cors import CORS

import config
import gee_engine

config.validate()

app = Flask(__name__)
CORS(app, origins=config.ALLOWED_ORIGINS)


# ---------------------------------------------------------------------------
# POST /api/verify
# ---------------------------------------------------------------------------

REQUIRED_FIELDS = ("project_id", "project_name", "coordinates", "start_date", "end_date")


@app.route("/api/verify", methods=["POST"])
def verify():
    body = request.get_json(silent=True)
    if not body:
        return _error("Request body must be JSON", 400)

    missing = [f for f in REQUIRED_FIELDS if f not in body]
    if missing:
        return _error(f"Missing required fields: {', '.join(missing)}", 400)

    project_id = body["project_id"]
    project_name = body["project_name"]
    coordinates = body["coordinates"]
    start_date = body["start_date"]
    end_date = body["end_date"]

    if not isinstance(coordinates, dict) or "type" not in coordinates:
        return _error(
            "coordinates must be a GeoJSON geometry or FeatureCollection", 400
        )

    if start_date >= end_date:
        return _error("start_date must be before end_date", 400)

    try:
        result = gee_engine.compute_ccis(
            project_id=project_id,
            project_name=project_name,
            geojson_polygon=coordinates,
            start_date=start_date,
            end_date=end_date,
        )
    except ValueError as exc:
        return _error(str(exc), 422)
    except RuntimeError as exc:
        return _error(str(exc), 504)
    except Exception as exc:  # noqa: BLE001
        app.logger.exception("Unexpected GEE error for project %s", project_id)
        return _error("Internal computation error — please retry", 500)

    return jsonify(
        {
            "project_id": project_id,
            "project_name": project_name,
            **result,
        }
    )


# ---------------------------------------------------------------------------
# POST /api/analyze-document
# ---------------------------------------------------------------------------

_CLAUDE_MODEL = "claude-sonnet-4-5"

_EXTRACT_PROMPT = (
    "Extract the following fields from this carbon project document. "
    "If a field is not mentioned, use null. "
    "Return ONLY valid JSON, no other text: "
    "{project_name, lat, lng, hectares, co2_tonnes, trees, start_year, end_year, registry, ecosystem}"
)


@app.route("/api/analyze-document", methods=["POST"])
def analyze_document():
    if "file" not in request.files:
        return _error("No file provided — include a PDF as 'file' in the form data", 400)

    f = request.files["file"]
    if not f.filename or not f.filename.lower().endswith(".pdf"):
        return _error("Only PDF files are supported", 400)

    pdf_bytes = f.read()
    if not pdf_bytes:
        return _error("Uploaded file is empty", 400)

    claude = anthropic.Anthropic(api_key=config.ANTHROPIC_API_KEY)

    try:
        pdf_b64 = base64.standard_b64encode(pdf_bytes).decode("utf-8")
        response = claude.messages.create(
            model=_CLAUDE_MODEL,
            max_tokens=1024,
            messages=[
                {
                    "role": "user",
                    "content": [
                        {
                            "type": "document",
                            "source": {
                                "type": "base64",
                                "media_type": "application/pdf",
                                "data": pdf_b64,
                            },
                        },
                        {"type": "text", "text": _EXTRACT_PROMPT},
                    ],
                }
            ],
        )
    except anthropic.BadRequestError:
        # PDF could not be parsed — fall back to reading as plain text
        text_content = pdf_bytes.decode("utf-8", errors="replace")
        try:
            response = claude.messages.create(
                model=_CLAUDE_MODEL,
                max_tokens=1024,
                messages=[
                    {"role": "user", "content": f"{text_content}\n\n{_EXTRACT_PROMPT}"},
                ],
            )
        except anthropic.APIError as exc:
            app.logger.exception("Anthropic API error during document analysis (text fallback)")
            return _error(f"AI analysis failed: {exc}", 502)
    except anthropic.APIError as exc:
        app.logger.exception("Anthropic API error during document analysis")
        return _error(f"AI analysis failed: {exc}", 502)

    raw = response.content[0].text.strip()
    if raw.startswith("```"):
        raw = raw[raw.index("\n") + 1:] if "\n" in raw else ""
    if raw.endswith("```"):
        raw = raw[:raw.rindex("```")].strip()

    try:
        extracted = json.loads(raw)
    except json.JSONDecodeError:
        app.logger.error("Non-JSON response from Claude: %s", raw[:200])
        return _error("AI returned non-JSON response", 502)

    return jsonify(extracted)


# ---------------------------------------------------------------------------
# POST /api/fraud-audit
# ---------------------------------------------------------------------------


@app.route("/api/fraud-audit", methods=["POST"])
def fraud_audit():
    body = request.get_json(silent=True)
    if not body:
        return _error("Request body must be JSON", 400)

    document_claims = body.get("document_claims")
    satellite_results = body.get("satellite_results")

    if not document_claims or not satellite_results:
        return _error("Both 'document_claims' and 'satellite_results' are required", 400)

    claude = anthropic.Anthropic(api_key=config.ANTHROPIC_API_KEY)

    prompt = (
        f"DEVELOPER'S CLAIMED PROJECT DATA:\n{json.dumps(document_claims, indent=2)}\n\n"
        f"SATELLITE CCIS EVIDENCE:\n{json.dumps(satellite_results, indent=2)}\n\n"
        "Respond with a JSON object containing exactly these fields:\n"
        '- fraud_risk: one of "Low", "Medium", "High", or "Critical"\n'
        "- fraud_probability: integer 0-100\n"
        "- discrepancies: array of strings, each describing one specific discrepancy\n"
        "- audit_summary: 2-3 sentence natural-language verdict\n\n"
        "Return ONLY the JSON object, nothing else."
    )

    try:
        response = claude.messages.create(
            model=_CLAUDE_MODEL,
            max_tokens=1024,
            system=(
                "You are a carbon credit fraud auditor. Compare the developer's claimed project data "
                "against satellite evidence. Identify discrepancies, flag overclaiming, assess fraud risk. "
                "Be specific and cite numbers."
            ),
            messages=[{"role": "user", "content": prompt}],
        )
    except anthropic.APIError as exc:
        app.logger.exception("Anthropic API error during fraud audit")
        return _error(f"AI audit failed: {exc}", 502)

    raw = response.content[0].text.strip()
    if raw.startswith("```"):
        raw = raw[raw.index("\n") + 1:] if "\n" in raw else ""
    if raw.endswith("```"):
        raw = raw[:raw.rindex("```")].strip()

    try:
        audit = json.loads(raw)
    except json.JSONDecodeError:
        app.logger.error("Non-JSON response from Claude: %s", raw[:200])
        return _error("AI returned non-JSON response", 502)

    return jsonify(audit)


# ---------------------------------------------------------------------------
# Health check
# ---------------------------------------------------------------------------

@app.route("/health", methods=["GET"])
def health():
    return jsonify({"status": "ok"})


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _error(message, status_code):
    return jsonify({"error": message}), status_code


if __name__ == "__main__":
    app.run(debug=False, host="0.0.0.0", port=5001)
