# WAFS Turbulence Worker

Downloads WAFS GRIB2 turbulence data from NOAA and writes EDR grid points to Supabase.

## Schedule
Runs 4× per day at 01:30, 07:30, 13:30, 19:30 UTC — after each GFS 6-hourly cycle is available.

## Environment Variables
- `SUPABASE_URL` — your Supabase project URL
- `SUPABASE_SERVICE_KEY` — your Supabase service role key

## Table
Writes to `turbulence_grid_data` with columns: `cycle_utc`, `valid_from_utc`, `fl`, `lat`, `lon`, `edr`
