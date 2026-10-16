"""
LegaTell API Server
====================
Exposes the pipeline as a REST API.
The legatell.com frontend calls this to generate live reports.

Run locally:
    python api.py

Deploy to Railway / Render / Fly.io:
    See README.md
"""

import os
import json
from flask import Flask, request, jsonify
from flask_cors import CORS
from dataclasses import asdict

from pipeline import LegaTellPipeline, CaseReport

app = Flask(__name__)

# Allow requests from legatell.com and localhost
CORS(app, origins=[
    "https://legatell.com",
    "https://www.legatell.com",
    "http://localhost:3000",
    "http://localhost:8080",
    "http://127.0.0.1:5500",
])

# Initialize pipeline once at startup (loads CSV dataset into memory)
CSV_PATH = os.environ.get("CSV_PATH", "SupremeOutput.csv")
USE_BROWSER = os.environ.get("USE_BROWSER", "false").lower() == "true"

pipeline = LegaTellPipeline(csv_path=CSV_PATH, use_browser=USE_BROWSER)
print(f"[LegaTell API] Pipeline ready. CSV: {CSV_PATH}, Browser: {USE_BROWSER}")


@app.route("/health", methods=["GET"])
def health():
    return jsonify({"status": "ok", "service": "LegaTell API"})


@app.route("/report", methods=["GET", "POST"])
def generate_report():
    """
    Generate a case report.

    GET  /report?index=614882/2023&court=Nassau+County+Supreme+Court
    POST /report  {"index": "614882/2023", "court": "Nassau County Supreme Court"}
    """
    if request.method == "POST":
        data = request.get_json(force=True) or {}
        index_number = data.get("index", "").strip()
        court = data.get("court", "").strip()
    else:
        index_number = request.args.get("index", "").strip()
        court = request.args.get("court", "").strip()

    if not index_number:
        return jsonify({"error": "index parameter is required"}), 400

    # Basic validation — NYS index numbers look like "614882/2023" or "E2023-0006"
    if len(index_number) < 4 or len(index_number) > 30:
        return jsonify({"error": "Invalid index number format"}), 400

    try:
        report = pipeline.generate_report(index_number, court)
        return jsonify(asdict(report))

    except ValueError as e:
        return jsonify({"error": str(e), "found": False}), 404

    except Exception as e:
        print(f"[LegaTell API] Error generating report for {index_number}: {e}")
        return jsonify({"error": "Internal error generating report"}), 500


@app.route("/search", methods=["GET"])
def search_cases():
    """
    Search cases by defendant name, plaintiff, or partial index number.
    Useful for the autocomplete feature on the report page.

    GET /search?q=smith&limit=5
    """
    query = request.args.get("q", "").strip().lower()
    limit = min(int(request.args.get("limit", 5)), 20)

    if len(query) < 3:
        return jsonify({"results": []})

    # Search in-memory CSV data
    results = []
    for row in pipeline.csv_lookup._data or []:
        caption = row.get("Caption", "").lower()
        case_num = row.get("Case #", "").lower()
        if query in caption or query in case_num:
            results.append({
                "index": row.get("Case #", ""),
                "caption": row.get("Caption", ""),
                "court": row.get("Court", ""),
                "status": row.get("eFiling Status Case Status", "").split("  ")[-1],
            })
        if len(results) >= limit:
            break

    return jsonify({"results": results})


@app.route("/courts", methods=["GET"])
def list_courts():
    """Return list of courts we have data for."""
    from collections import Counter
    if pipeline.csv_lookup._data is None:
        pipeline.csv_lookup._load()
    courts = Counter(r.get("Court", "") for r in pipeline.csv_lookup._data)
    return jsonify({
        "courts": [{"name": k, "case_count": v}
                   for k, v in sorted(courts.items(), key=lambda x: -x[1])]
    })


if __name__ == "__main__":
    port = int(os.environ.get("PORT", 5000))
    debug = os.environ.get("DEBUG", "true").lower() == "true"
    print(f"[LegaTell API] Starting on port {port}")
    app.run(host="0.0.0.0", port=port, debug=debug)
