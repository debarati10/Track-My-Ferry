-- Track My Ferry storage schema (PostgreSQL 14+).
-- TimescaleDB (hypertables + retention) and PostGIS (geography index) are used when installed;
-- everything also works on plain PostgreSQL.

DO $$
BEGIN
  BEGIN
    CREATE EXTENSION IF NOT EXISTS timescaledb;
  EXCEPTION WHEN OTHERS THEN
    RAISE NOTICE 'timescaledb not available, using plain tables';
  END;
  BEGIN
    CREATE EXTENSION IF NOT EXISTS postgis;
  EXCEPTION WHEN OTHERS THEN
    RAISE NOTICE 'postgis not available, skipping geometry columns';
  END;
END $$;

CREATE TABLE IF NOT EXISTS vessel_positions (
  ts        timestamptz      NOT NULL,
  mmsi      bigint           NOT NULL,
  lat       double precision NOT NULL,
  lon       double precision NOT NULL,
  sog_kn    real,
  cog       real,
  heading   real,
  name      text
);
CREATE INDEX IF NOT EXISTS vessel_positions_mmsi_ts ON vessel_positions (mmsi, ts DESC);

CREATE TABLE IF NOT EXISTS vessel_states (
  ts               timestamptz NOT NULL,
  mmsi             bigint      NOT NULL,
  lat              double precision,
  lon              double precision,
  sog_kn           real,
  status           text,
  route_id         text,
  direction_id     smallint,
  trip_id          text,
  along_m          real,
  next_stop_id     text,
  delay_s          real,
  match_confidence real
);
CREATE INDEX IF NOT EXISTS vessel_states_mmsi_ts ON vessel_states (mmsi, ts DESC);

CREATE TABLE IF NOT EXISTS stop_events (
  mmsi          bigint      NOT NULL,
  stop_id       text        NOT NULL,
  arrival_ts    timestamptz NOT NULL,
  departure_ts  timestamptz,
  trip_id       text,
  route_id      text,
  direction_id  smallint,
  service_date  text,
  vessel_name   text,
  PRIMARY KEY (mmsi, stop_id, arrival_ts)
);
CREATE INDEX IF NOT EXISTS stop_events_stop_ts ON stop_events (stop_id, arrival_ts DESC);

CREATE TABLE IF NOT EXISTS trip_updates (
  ts           timestamptz NOT NULL,
  trip_id      text        NOT NULL,
  start_date   text,
  stop_id      text        NOT NULL,
  arrival_ts   timestamptz,
  departure_ts timestamptz,
  delay_s      integer
);
CREATE INDEX IF NOT EXISTS trip_updates_trip_ts ON trip_updates (trip_id, ts DESC);

CREATE TABLE IF NOT EXISTS predictions (
  made_at       timestamptz NOT NULL,
  mmsi          bigint      NOT NULL,
  trip_id       text        NOT NULL,
  route_id      text,
  direction_id  smallint,
  stop_id       text        NOT NULL,
  stops_ahead   smallint,
  eta_p10       timestamptz,
  eta_p50       timestamptz,
  eta_p90       timestamptz,
  official_eta  timestamptz,
  scheduled_eta timestamptz,
  p_full        real,
  model_version text
);
CREATE INDEX IF NOT EXISTS predictions_stop_made ON predictions (stop_id, made_at DESC);
CREATE INDEX IF NOT EXISTS predictions_mmsi_stop_made ON predictions (mmsi, stop_id, made_at);

CREATE TABLE IF NOT EXISTS env_observations (
  ts      timestamptz NOT NULL,
  kind    text        NOT NULL,
  "values" jsonb      NOT NULL,
  source  text
);

CREATE TABLE IF NOT EXISTS ridership_hourly (
  date      date     NOT NULL,
  hour      smallint NOT NULL,
  route     text     NOT NULL,
  direction text     NOT NULL,
  stop      text     NOT NULL,
  boardings integer  NOT NULL,
  typeday   text,
  PRIMARY KEY (date, hour, route, direction, stop)
);

DO $$
BEGIN
  IF EXISTS (SELECT 1 FROM pg_extension WHERE extname = 'timescaledb') THEN
    PERFORM create_hypertable('vessel_positions', 'ts', if_not_exists => TRUE, migrate_data => TRUE);
    PERFORM create_hypertable('vessel_states', 'ts', if_not_exists => TRUE, migrate_data => TRUE);
    PERFORM create_hypertable('trip_updates', 'ts', if_not_exists => TRUE, migrate_data => TRUE);
    PERFORM create_hypertable('predictions', 'made_at', if_not_exists => TRUE, migrate_data => TRUE);
    PERFORM create_hypertable('env_observations', 'ts', if_not_exists => TRUE, migrate_data => TRUE);
    PERFORM add_retention_policy('vessel_states', INTERVAL '30 days', if_not_exists => TRUE);
    PERFORM add_retention_policy('trip_updates', INTERVAL '90 days', if_not_exists => TRUE);
  END IF;
  IF EXISTS (SELECT 1 FROM pg_extension WHERE extname = 'postgis') THEN
    ALTER TABLE vessel_positions ADD COLUMN IF NOT EXISTS geom geography(Point, 4326)
      GENERATED ALWAYS AS (ST_SetSRID(ST_MakePoint(lon, lat), 4326)::geography) STORED;
    CREATE INDEX IF NOT EXISTS vessel_positions_geom ON vessel_positions USING GIST (geom);
  END IF;
END $$;

-- Every prediction joined to the arrival that actually happened: live accuracy monitoring.
CREATE OR REPLACE VIEW prediction_errors AS
SELECT p.made_at, p.mmsi, p.trip_id, p.route_id, p.stop_id, p.stops_ahead, p.model_version,
       e.arrival_ts                                           AS actual_arrival,
       EXTRACT(EPOCH FROM (e.arrival_ts - p.made_at))         AS horizon_s,
       EXTRACT(EPOCH FROM (p.eta_p50 - e.arrival_ts))         AS error_s,
       EXTRACT(EPOCH FROM (p.official_eta - e.arrival_ts))    AS official_error_s,
       (e.arrival_ts BETWEEN p.eta_p10 AND p.eta_p90)         AS inside_band
FROM predictions p
JOIN LATERAL (
  SELECT arrival_ts FROM stop_events e
  WHERE e.mmsi = p.mmsi AND e.stop_id = p.stop_id AND e.arrival_ts > p.made_at
  ORDER BY e.arrival_ts LIMIT 1
) e ON e.arrival_ts < p.made_at + INTERVAL '2 hours';
