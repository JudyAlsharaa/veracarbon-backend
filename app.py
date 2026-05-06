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
