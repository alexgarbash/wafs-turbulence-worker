import os
import requests
import tempfile
from datetime import datetime, timezone, timedelta
from supabase import create_client

SUPABASE_URL = os.environ["SUPABASE_URL"]
SUPABASE_KEY = os.environ["SUPABASE_SERVICE_KEY"]
supabase = create_client(SUPABASE_URL, SUPABASE_KEY)

WIFS_API_BASE = "https://aviationweather.gov/wifs/api"
WIFS_API_KEY = os.environ["WIFS_API_KEY"]
WIFS_COLLECTION = "kwbc_wafshzds_blended_turb_0p25"

# Only these flight levels are needed (commercial cruise altitudes)
ALLOWED_FL = {270, 330, 360, 390, 420}

# 3-hour forecast blocks available from WIFS
FORECAST_BLOCKS = [
    "006-008", "009-011", "012-014", "015-017",
    "018-020", "021-023", "024-026", "027-029",
    "030-032", "033-035", "036-038",
]

def get_latest_cycle():
    now = datetime.now(timezone.utc)
    cycle_hour = (now.hour // 6) * 6
    cycle = now.replace(hour=cycle_hour, minute=0, second=0, microsecond=0)
    if (now - cycle).total_seconds() < 3600 * 3.5:
        cycle -= timedelta(hours=6)
    return cycle

def get_or_create_cycle(cycle_dt):
    """Get or create cycle in turbulence_cycles, return its UUID."""
    cycle_str = cycle_dt.strftime("%Y-%m-%dT%H:%M:%SZ")
    # Try to find existing cycle first
    res = supabase.table("turbulence_cycles").select("id").eq("cycle_issued_utc", cycle_str).execute()
    if res.data:
        cycle_id = res.data[0]["id"]
        print(f"  Found existing cycle: {cycle_id}")
        return cycle_id
    # Insert new cycle
    res = supabase.table("turbulence_cycles").insert({"cycle_issued_utc": cycle_str}).execute()
    cycle_id = res.data[0]["id"]
    print(f"  Created new cycle: {cycle_id}")
    return cycle_id

def download_grib(block):
    """Download a GRIB2 file from WIFS API for a forecast hour block."""
    item_id = f"YLDYG{block}FLALL"
    url = f"{WIFS_API_BASE}/collections/{WIFS_COLLECTION}/items/{item_id}"
    print(f"Downloading {url}")
    resp = requests.get(
        url,
        headers={"X-API-Key": WIFS_API_KEY},
        params={"f": "grib2"},
        timeout=180,
        stream=True,
    )
    if resp.status_code != 200:
        print(f"  HTTP {resp.status_code}, skipping")
        return None
    tmp = tempfile.NamedTemporaryFile(suffix=".grib2", delete=False)
    for chunk in resp.iter_content(8192):
        tmp.write(chunk)
    tmp.close()
    print(f"  Saved {os.path.getsize(tmp.name)} bytes")
    return tmp.name

def parse_grib(filepath, cycle_id, cycle_dt):
    import eccodes
    rows = []
    with open(filepath, "rb") as f:
        while True:
            msgid = eccodes.codes_grib_new_from_file(f)
            if msgid is None:
                break
            try:
                short = eccodes.codes_get(msgid, "shortName")
                if short not in ("EDPARM", "edr", "edpf"):
                    continue
                level_pa = eccodes.codes_get(msgid, "level")
                fl = level_pa // 100
                if fl not in ALLOWED_FL:
                    continue
                step = eccodes.codes_get(msgid, "forecastTime")
                valid_dt = cycle_dt + timedelta(hours=step)
                valid_str = valid_dt.strftime("%Y-%m-%dT%H:%M:%SZ")
                ni = eccodes.codes_get(msgid, "Ni")
                nj = eccodes.codes_get(msgid, "Nj")
                lat1 = eccodes.codes_get(msgid, "latitudeOfFirstGridPointInDegrees")
                lon1 = eccodes.codes_get(msgid, "longitudeOfFirstGridPointInDegrees")
                lat2 = eccodes.codes_get(msgid, "latitudeOfLastGridPointInDegrees")
                di = eccodes.codes_get(msgid, "iDirectionIncrementInDegrees")
                dj = eccodes.codes_get(msgid, "jDirectionIncrementInDegrees")
                values = eccodes.codes_get_values(msgid)
                for j in range(nj):
                    lat = lat1 - j * dj if lat1 > lat2 else lat1 + j * dj
                    for i in range(ni):
                        lon = lon1 + i * di
                        idx = j * ni + i
                        edr = float(values[idx])
                        if edr < 0.01:
                            continue
                        if lon > 180:
                            lon -= 360
                        rows.append({
                            "cycle_id": cycle_id,
                            "valid_from_utc": valid_str,
                            "fl": fl,
                            "lat": round(lat, 4),
                            "lon": round(lon, 4),
                            "edr": round(edr, 4),
                        })
            finally:
                eccodes.codes_release(msgid)
    return rows

def cleanup_old(cycle_dt):
    """Delete cycles older than 12h — grid data deleted via FK cascade."""
    cutoff = (cycle_dt - timedelta(hours=12)).strftime("%Y-%m-%dT%H:%M:%SZ")
    # Get old cycle IDs
    old = supabase.table("turbulence_cycles").select("id").lt("cycle_issued_utc", cutoff).execute()
    if not old.data:
        print(f"Nothing to clean (cutoff {cutoff})")
        return
    old_ids = [r["id"] for r in old.data]
    # Delete grid data first (in case no CASCADE)
    supabase.table("turbulence_grid_data").delete().in_("cycle_id", old_ids).execute()
    # Delete cycles
    supabase.table("turbulence_cycles").delete().in_("id", old_ids).execute()
    print(f"Cleaned {len(old_ids)} old cycles (older than {cutoff})")

def upsert_batch(rows):
    BATCH = 1000
    total = 0
    for i in range(0, len(rows), BATCH):
        res = supabase.table("turbulence_grid_data").upsert(rows[i:i+BATCH]).execute()
        total += len(res.data) if res.data else BATCH
    print(f"  Upserted {len(rows)} rows")

def ingest():
    cycle = get_latest_cycle()
    print(f"=== Cycle: {cycle.isoformat()} ===")
    cycle_id = get_or_create_cycle(cycle)
    total_ingested = 0
    for block in FORECAST_BLOCKS:
        filepath = download_grib(block)
        if not filepath:
            continue
        try:
            rows = parse_grib(filepath, cycle_id, cycle)
            print(f"  Parsed {len(rows)} turbulence points for f{block}")
            if rows:
                upsert_batch(rows)
                total_ingested += len(rows)
        finally:
            os.unlink(filepath)
    if total_ingested > 0:
        cleanup_old(cycle)
        print(f"=== Done: ingested {total_ingested} points, cleaned old cycles ===")
    else:
        print("=== No data ingested, skipping cleanup to preserve existing data ===")

if __name__ == "__main__":
    ingest()
