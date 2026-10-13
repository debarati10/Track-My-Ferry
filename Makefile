.PHONY: install dev web demo sim train test lint record docker-sim docker-live clean

install:            ## install the Python package with all extras
	pip install -e ".[all,dev]"

web:                ## build the map into web/dist (served by the API)
	cd web && npm install && npm run build

demo: web           ## one process: simulator + pipeline + API + map on http://localhost:8000
	trackmyferry run --mode sim --speedup 5

sim:                ## generate 3 weeks of simulated data with ground truth (about 4 min)
	trackmyferry simulate --out data/sim --days 21

train:              ## replay data -> features -> models -> docs/RESULTS.md
	trackmyferry train --data data/sim --models models --report docs

test:
	pytest

lint:
	ruff check trackmyferry tests

record:             ## live: start capturing feeds (needs AISSTREAM_API_KEY); leave it running
	trackmyferry record --mode live --out data/live

docker-sim:         ## full distributed stack on simulated data
	docker compose --profile sim up --build

docker-live:        ## full distributed stack on live data
	TRACKMYFERRY_MODE=live docker compose --profile live up --build

clean:
	rm -rf web/dist data/sim/features_cache.pkl .pytest_cache
