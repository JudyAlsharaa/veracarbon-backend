"""
Google Earth Engine computation engine for the 7-pillar CCIS score.

Pillar weights (sum to 1.0):
  P1 NDVI delta            0.20
  P2 SAR backscatter       0.15
  P3 Forest permanence     0.20
  P4 Additionality         0.15
  P5 Leakage               0.15
  P6 Temporal consistency  0.10
  P7 Data confidence       0.05
"""

import math
import threading
from datetime import datetime, timezone

import ee

import config

PILLAR_WEIGHTS = {
    "p1": 0.20,
    "p2": 0.15,
    "p3": 0.20,
    "p4": 0.15,
    "p5": 0.15,
    "p6": 0.10,
    "p7": 0.05,
}

_init_lock = threading.Lock()
_initialized = False


def _init_gee():
    global _initialized
    with _init_lock:
        if _initialized:
            return
        credentials = ee.ServiceAccountCredentials(
            config.GEE_SERVICE_ACCOUNT, config.GEE_KEY_FILE
        )
        ee.Initialize(credentials)
        _initialized = True


# ---------------------------------------------------------------------------
# Public entry point
# ---------------------------------------------------------------------------

def compute_ccis(project_id, project_name, geojson_polygon, start_date, end_date):
    """
    Returns a dict with ccis_score, pillar_scores, confidence, ndvi_delta,
    sar_delta, verification_date, and status.

    Raises ValueError for bad geometry, RuntimeError for GEE failures.
    """
    _init_gee()

    aoi = _geojson_to_ee_geometry(geojson_polygon)

    result = _run_with_timeout(
        _compute_all_pillars,
        args=(aoi, start_date, end_date),
        timeout=config.GEE_TIMEOUT_SECONDS,
    )

    pillar_scores = result["pillar_scores"]
    ndvi_delta = result["ndvi_delta"]
    sar_delta = result["sar_delta"]
    cloud_pct = result["cloud_pct"]
    n_clean_scenes = result["n_clean_scenes"]

    ccis_score = sum(
        pillar_scores[p] * PILLAR_WEIGHTS[p] for p in PILLAR_WEIGHTS
    )
    ccis_score = round(min(max(ccis_score * 100, 0), 100), 2)

    confidence = _derive_confidence(cloud_pct, n_clean_scenes, pillar_scores)
    status = "verified" if ccis_score >= 60 and confidence != "low" else "needs_review"

    return {
        "ccis_score": ccis_score,
        "pillar_scores": {k: round(v, 4) for k, v in pillar_scores.items()},
        "confidence": confidence,
        "ndvi_delta": round(ndvi_delta, 4),
        "sar_delta": round(sar_delta, 4),
        "verification_date": datetime.now(timezone.utc).isoformat(),
        "status": status,
    }


# ---------------------------------------------------------------------------
# Geometry helpers
# ---------------------------------------------------------------------------

def _geojson_to_ee_geometry(geojson):
    geom_type = geojson.get("type")
    if geom_type == "FeatureCollection":
        features = geojson.get("features", [])
        if not features:
            raise ValueError("FeatureCollection has no features")
        geojson = features[0].get("geometry", features[0])
        geom_type = geojson.get("type")

    if geom_type == "Feature":
        geojson = geojson.get("geometry")
        geom_type = geojson.get("type") if geojson else None

    if geom_type not in ("Polygon", "MultiPolygon"):
        raise ValueError(
            f"Geometry must be Polygon or MultiPolygon, got {geom_type!r}"
        )

    coords = geojson.get("coordinates")
    if not coords:
        raise ValueError("Geometry has no coordinates")

    try:
        if geom_type == "Polygon":
            return ee.Geometry.Polygon(coords)
        return ee.Geometry.MultiPolygon(coords)
    except Exception as exc:
        raise ValueError(f"Invalid geometry coordinates: {exc}") from exc


# ---------------------------------------------------------------------------
# Core computation
# ---------------------------------------------------------------------------

def _compute_all_pillars(aoi, start_date, end_date):
    start = ee.Date(start_date)
    end = ee.Date(end_date)

    mid = start.advance(
        ee.Number(end.difference(start, "day")).divide(2).int(), "day"
    )

    # --- Sentinel-2 composites (cloud-masked) ---
    s2_early = _s2_composite(aoi, start, mid)
    s2_late = _s2_composite(aoi, mid, end)
    s2_full = _s2_composite(aoi, start, end)

    # --- Sentinel-1 composites ---
    s1_early = _s1_composite(aoi, start, mid)
    s1_late = _s1_composite(aoi, mid, end)

    # --- Hansen forest loss ---
    hansen = ee.Image("UMD/hansen/global_forest_change_2023_v1_11")

    buffer_aoi = aoi.buffer(config.ADDITIONALITY_BUFFER_METERS)
    leakage_aoi = aoi.buffer(config.LEAKAGE_BUFFER_METERS).difference(aoi)

    # --- Evaluate all pillars ---
    p1, ndvi_early, ndvi_late = _pillar1_ndvi_delta(s2_early, s2_late, aoi)
    p2, sar_early, sar_late = _pillar2_sar_backscatter(s1_early, s1_late, aoi)
    p3 = _pillar3_forest_permanence(hansen, aoi, start_date, end_date)
    p4 = _pillar4_additionality(s2_early, s2_late, aoi, buffer_aoi)
    p5 = _pillar5_leakage(hansen, leakage_aoi, start_date, end_date)
    p6, n_clean, cloud_pct = _pillar6_temporal_consistency(aoi, start, end)
    p7 = _pillar7_data_confidence(cloud_pct, n_clean)

    ndvi_delta = float(ndvi_late) - float(ndvi_early)
    sar_delta = float(sar_late) - float(sar_early)

    return {
        "pillar_scores": {
            "p1": float(p1),
            "p2": float(p2),
            "p3": float(p3),
            "p4": float(p4),
            "p5": float(p5),
            "p6": float(p6),
            "p7": float(p7),
        },
        "ndvi_delta": ndvi_delta,
        "sar_delta": sar_delta,
        "cloud_pct": cloud_pct,
        "n_clean_scenes": n_clean,
    }


# ---------------------------------------------------------------------------
# Sentinel-2 helpers
# ---------------------------------------------------------------------------

def _mask_s2_clouds(image):
    qa = image.select("QA60")
    cloud_bit = 1 << 10
    cirrus_bit = 1 << 11
    mask = qa.bitwiseAnd(cloud_bit).eq(0).And(qa.bitwiseAnd(cirrus_bit).eq(0))
    return image.updateMask(mask).divide(10000)


def _s2_composite(aoi, start, end):
    return (
        ee.ImageCollection("COPERNICUS/S2_SR_HARMONIZED")
        .filterBounds(aoi)
        .filterDate(start, end)
        .filter(ee.Filter.lt("CLOUDY_PIXEL_PERCENTAGE", config.MAX_CLOUD_COVER_PCT))
        .map(_mask_s2_clouds)
        .median()
    )


def _safe_ndvi(image):
    nir = image.select("B8")
    red = image.select("B4")
    return nir.subtract(red).divide(nir.add(red)).rename("NDVI")


# ---------------------------------------------------------------------------
# Pillar 1: NDVI delta
# ---------------------------------------------------------------------------

def _pillar1_ndvi_delta(s2_early, s2_late, aoi):
    scale = 30
    ndvi_early = (
        _safe_ndvi(s2_early)
        .reduceRegion(ee.Reducer.mean(), aoi, scale)
        .get("NDVI")
        .getInfo()
    )
    ndvi_late = (
        _safe_ndvi(s2_late)
        .reduceRegion(ee.Reducer.mean(), aoi, scale)
        .get("NDVI")
        .getInfo()
    )

    if ndvi_early is None or ndvi_late is None:
        return 0.5, 0.0, 0.0  # neutral when no data

    delta = float(ndvi_late) - float(ndvi_early)
    # Score: 1.0 for delta >= +0.15; 0.0 for delta <= -0.10
    score = _clamp((delta + 0.10) / 0.25, 0.0, 1.0)
    return score, float(ndvi_early), float(ndvi_late)


# ---------------------------------------------------------------------------
# Pillar 2: SAR backscatter change (biomass proxy)
# ---------------------------------------------------------------------------

def _s1_composite(aoi, start, end):
    return (
        ee.ImageCollection("COPERNICUS/S1_GRD")
        .filterBounds(aoi)
        .filterDate(start, end)
        .filter(ee.Filter.eq("instrumentMode", "IW"))
        .filter(
            ee.Filter.listContains("transmitterReceiverPolarisation", "VV").And(
                ee.Filter.listContains("transmitterReceiverPolarisation", "VH")
            )
        )
        .select(["VV", "VH"])
        .median()
    )


def _pillar2_sar_backscatter(s1_early, s1_late, aoi):
    scale = 30
    vh_early = (
        s1_early.select("VH")
        .reduceRegion(ee.Reducer.mean(), aoi, scale)
        .get("VH")
        .getInfo()
    )
    vh_late = (
        s1_late.select("VH")
        .reduceRegion(ee.Reducer.mean(), aoi, scale)
        .get("VH")
        .getInfo()
    )

    if vh_early is None or vh_late is None:
        return 0.5, 0.0, 0.0

    delta = float(vh_late) - float(vh_early)  # dB
    # Score: 1.0 for +3dB gain, 0.0 for -3dB loss
    score = _clamp((delta + 3.0) / 6.0, 0.0, 1.0)
    return score, float(vh_early), float(vh_late)


# ---------------------------------------------------------------------------
# Pillar 3: Forest cover permanence (Hansen loss layer)
# ---------------------------------------------------------------------------

def _pillar3_forest_permanence(hansen, aoi, start_date, end_date):
    start_year = int(start_date[:4]) - 2000
    end_year = int(end_date[:4]) - 2000

    loss_year = hansen.select("lossyear")
    loss_in_period = loss_year.gte(start_year).And(loss_year.lte(end_year))

    treecover = hansen.select("treecover2000").gt(30)

    stats = (
        ee.Image.cat([treecover.rename("forest"), loss_in_period.rename("loss")])
        .reduceRegion(ee.Reducer.mean(), aoi, 30)
        .getInfo()
    )

    forest_pct = stats.get("forest") or 0.0
    loss_pct = stats.get("loss") or 0.0

    if forest_pct < 0.01:
        return 0.5  # non-forest project; neutral

    # Score: fraction of forest that was NOT lost
    loss_fraction = loss_pct / max(forest_pct, 1e-6)
    return _clamp(1.0 - loss_fraction * 10, 0.0, 1.0)


# ---------------------------------------------------------------------------
# Pillar 4: Additionality (project NDVI gain vs. buffer baseline)
# ---------------------------------------------------------------------------

def _pillar4_additionality(s2_early, s2_late, aoi, buffer_aoi):
    scale = 30

    def _mean_ndvi(composite, region):
        return (
            _safe_ndvi(composite)
            .reduceRegion(ee.Reducer.mean(), region, scale)
            .get("NDVI")
            .getInfo()
        )

    proj_early = _mean_ndvi(s2_early, aoi)
    proj_late = _mean_ndvi(s2_late, aoi)
    buf_early = _mean_ndvi(s2_early, buffer_aoi)
    buf_late = _mean_ndvi(s2_late, buffer_aoi)

    if any(v is None for v in (proj_early, proj_late, buf_early, buf_late)):
        return 0.5

    proj_delta = float(proj_late) - float(proj_early)
    buf_delta = float(buf_late) - float(buf_early)

    # Additionality = how much better the project performed vs. counterfactual
    additional = proj_delta - buf_delta
    return _clamp((additional + 0.05) / 0.15, 0.0, 1.0)


# ---------------------------------------------------------------------------
# Pillar 5: Leakage (deforestation in surrounding buffer zone)
# ---------------------------------------------------------------------------

def _pillar5_leakage(hansen, leakage_aoi, start_date, end_date):
    start_year = int(start_date[:4]) - 2000
    end_year = int(end_date[:4]) - 2000

    loss_year = hansen.select("lossyear")
    loss_in_period = loss_year.gte(start_year).And(loss_year.lte(end_year))

    stats = (
        loss_in_period.rename("loss")
        .reduceRegion(ee.Reducer.mean(), leakage_aoi, 30)
        .getInfo()
    )

    loss_pct = stats.get("loss") or 0.0
    # Score: 1.0 for no leakage; 0.0 if >5% of buffer lost
    return _clamp(1.0 - loss_pct / 0.05, 0.0, 1.0)


# ---------------------------------------------------------------------------
# Pillar 6: Temporal consistency (coefficient of variation of NDVI)
# ---------------------------------------------------------------------------

def _pillar6_temporal_consistency(aoi, start, end):
    collection = (
        ee.ImageCollection("COPERNICUS/S2_SR_HARMONIZED")
        .filterBounds(aoi)
        .filterDate(start, end)
        .filter(ee.Filter.lt("CLOUDY_PIXEL_PERCENTAGE", config.MAX_CLOUD_COVER_PCT))
        .map(_mask_s2_clouds)
    )

    n_clean = int(collection.size().getInfo())

    if n_clean < 2:
        cloud_pct = 100.0
        return 0.2, n_clean, cloud_pct

    def _add_mean_ndvi(image):
        val = (
            _safe_ndvi(image)
            .reduceRegion(ee.Reducer.mean(), aoi, 30)
            .get("NDVI")
        )
        return image.set("mean_ndvi", val)

    with_ndvi = collection.map(_add_mean_ndvi)
    values = with_ndvi.aggregate_array("mean_ndvi").getInfo()
    values = [v for v in values if v is not None]

    if len(values) < 2:
        return 0.3, n_clean, 50.0

    mean_v = sum(values) / len(values)
    std_v = math.sqrt(sum((x - mean_v) ** 2 for x in values) / len(values))
    cv = std_v / max(abs(mean_v), 1e-6)

    # Low CV = high consistency = high score
    score = _clamp(1.0 - cv / 0.3, 0.0, 1.0)

    # Estimate cloud pct from collection metadata
    cloud_pcts = (
        collection.aggregate_array("CLOUDY_PIXEL_PERCENTAGE").getInfo() or []
    )
    cloud_pct = float(sum(cloud_pcts) / len(cloud_pcts)) if cloud_pcts else 0.0

    return score, n_clean, cloud_pct


# ---------------------------------------------------------------------------
# Pillar 7: Data confidence
# ---------------------------------------------------------------------------

def _pillar7_data_confidence(cloud_pct, n_clean_scenes):
    cloud_score = _clamp(1.0 - cloud_pct / 100.0, 0.0, 1.0)
    # 12+ scenes per year is ideal; fewer reduces confidence
    scene_score = _clamp(n_clean_scenes / 12.0, 0.0, 1.0)
    return 0.6 * cloud_score + 0.4 * scene_score


# ---------------------------------------------------------------------------
# Confidence label
# ---------------------------------------------------------------------------

def _derive_confidence(cloud_pct, n_clean_scenes, pillar_scores):
    if n_clean_scenes >= 6 and cloud_pct < 20:
        return "high"
    if n_clean_scenes >= 3 and cloud_pct < 50:
        return "medium"
    return "low"


# ---------------------------------------------------------------------------
# Utilities
# ---------------------------------------------------------------------------

def _clamp(value, lo, hi):
    return max(lo, min(hi, value))


def _run_with_timeout(fn, args=(), timeout=120):
    result = {}
    exc_box = {}

    def _target():
        try:
            result["value"] = fn(*args)
        except Exception as exc:  # noqa: BLE001
            exc_box["error"] = exc

    t = threading.Thread(target=_target, daemon=True)
    t.start()
    t.join(timeout)

    if t.is_alive():
        raise RuntimeError(
            f"GEE computation timed out after {timeout}s. "
            "Try a smaller area or shorter date range."
        )
    if "error" in exc_box:
        raise exc_box["error"]
    return result["value"]


def get_satellite_thumbnail(geojson_polygon, start_date, end_date):
    """Returns a base64-encoded RGB satellite image of the AOI."""
    _init_gee()
    aoi = _geojson_to_ee_geometry(geojson_polygon)
    start = ee.Date(start_date)
    end = ee.Date(end_date)
    composite = _s2_composite(aoi, start, end)
    url = composite.select(["B4", "B3", "B2"]).getThumbURL({
        "region": aoi,
        "dimensions": 512,
        "format": "png",
        "min": 0,
        "max": 0.3,
        "gamma": 1.4,
    })
    import urllib.request, base64
    with urllib.request.urlopen(url) as r:
        img_bytes = r.read()
    return base64.standard_b64encode(img_bytes).decode("utf-8")
