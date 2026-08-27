.PHONY: verify verify-fast verify-ci test browser signals docker-build docker-run

signals:
	python3 refresh_fund_signals.py

verify:
	python3 verify.py

verify-fast:
	python3 verify.py --fast

verify-ci:
	python3 verify.py --ci

test:
	python3 -m unittest discover -v -s tests

browser:
	python3 tests/chromium_walkthrough.py --report artifacts/chromium-report.json

docker-build:
	docker compose build

docker-run:
	docker compose up -d --build
