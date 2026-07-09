SERVICES := api-gateway workflow-service decision-service payment-service

.PHONY: install test run down logs

# Install backend test dependencies for each service and UI packages.
install:
	@for svc in $(SERVICES); do \
		echo "Installing services/$$svc"; \
		pip install -r services/$$svc/requirements.txt; \
	done
	cd ui && npm install

# Run the health tests for every backend service. No external services required.
test:
	@for svc in $(SERVICES); do \
		echo "Testing services/$$svc"; \
		(cd services/$$svc && pytest -q); \
	done

run:
	docker compose up --build

down:
	docker compose down

logs:
	docker compose logs -f
