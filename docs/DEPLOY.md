# Running and deploying Track My Ferry

## 1. Demo on your laptop (no keys, about 5 minutes)

```bash
python -m venv .venv && source .venv/bin/activate
pip install -e ".[dev]"
cd web && npm install && npm run build && cd ..
trackmyferry run --mode sim --speedup 5        # open http://localhost:8000
```

The simulator runs a realistic fleet on a sample network at 5× speed. If you start it at night it
jumps to the morning rush so there's something to watch. For frontend work, run `npm run dev` in
`web/` (port 5173, proxies the API on 8000).

## 2. The full distributed stack (Docker)

```bash
docker compose --profile sim up --build
```

| URL | What |
|---|---|
| http://localhost:8000 | map + API (`/docs` for the OpenAPI UI) |
| http://localhost:3000 | Grafana (admin / admin): throughput, latency, live accuracy |
| http://localhost:9090 | Prometheus |

## 3. Going live with real NYC Ferry data

1. **Get an AIS key.** Sign in at https://aisstream.io with GitHub and create an API key (free).
2. **Start recording today.** Live feeds keep no history, and the model needs a few weeks of data.
   ```bash
   export AISSTREAM_API_KEY=...
   trackmyferry record --mode live --out data/live          # leave this running (tmux / a small VM)
   ```
3. **Download ridership** for the crowding model:
   ```bash
   trackmyferry ridership --out data/live/ridership.csv --since 2024-01-01
   ```
4. **Discover the fleet** after a day of recording. This lists every boat that behaves like an NYC
   Ferry vessel. Check the names, delete anything that isn't one (NY Waterway boats share Pier 11 and
   E 34th St), and keep the file:
   ```bash
   trackmyferry fleet --data data/live --out data/fleet.json   # the app reads data/fleet.json
   ```
5. **Train** after two to three weeks:
   ```bash
   trackmyferry train --data data/live --models models --report docs --test-days 5
   ```
   This writes `docs/RESULTS.md` with real accuracy numbers. The ground-truth sections (detector,
   matching) only appear for simulated data.
6. **Run live**:
   ```bash
   trackmyferry run --mode live          # or: TRACKMYFERRY_MODE=live docker compose --profile live up --build
   ```

Things to check on day one of live data:
* AIS coverage: open `GET /api/vessels` during rush hour and compare against the boats you expect.
* Whether NYC Ferry's TripUpdates include a vehicle label. If they do, trip matching gets easier
  automatically.
* Ferry capacities. The real fleet has roughly 150- and 350-passenger boats. Set them per route in
  `CrowdingModel.capacity_by_route` (saved in `models/crowding.json`).

## 4. Hosting it

**Smallest setup:** one VM (AWS t3.small / GCP e2-small, about $15/month) running `trackmyferry run --mode
live` behind Caddy or nginx for HTTPS, with `trackmyferry record` alongside it. Track history and live
accuracy need PostgreSQL: set `DATABASE_URL` to a managed instance, or run the compose stack.

**Full stack:** the compose file maps one-to-one to managed services:
* Kafka → Redpanda Cloud / Confluent / Amazon MSK
* PostgreSQL + TimescaleDB → Timescale Cloud, or RDS PostgreSQL (without hypertables)
* Redis → ElastiCache / Memorystore
* containers → ECS, Cloud Run (API) + a VM or GKE for the long-running processor

**Custom domain:** serve the map from the API (it's already bundled into the image) and point a
subdomain such as `ferry.debarati-chakrabarti.com` at the VM or load balancer.

## Configuration

Everything is set through environment variables; see `.env.example` for the full list.
