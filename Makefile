SERVICES := api-gateway workflow-service decision-service payment-service

.PHONY: install test run down logs verify

install:
	@for svc in $(SERVICES); do \
		echo "Installing services/$$svc"; \
		pip install -r services/$$svc/requirements.txt; \
	done
	cd ui && npm install

# runs without Docker, Redis or PostgreSQL
test:
	@for svc in $(SERVICES); do \
		echo "Testing services/$$svc"; \
		(cd services/$$svc && pytest -q); \
	done

# end-to-end journeys against the running stack; starts it if needed
verify:
	python tests/verification/run.py

run:
	docker compose up --build

down:
	docker compose down

logs:
	docker compose logs -f
