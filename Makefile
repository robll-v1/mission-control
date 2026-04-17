.PHONY: start stop status setup clean

VENV_AMC := .venv/bin/amc

# One-command start: auto-setup if needed, launch all services
# Override ports: make start PORT=9000 UI_PORT=9173
start: $(VENV_AMC)
	@$(VENV_AMC) start $(if $(PORT),--port $(PORT)) $(if $(UI_PORT),--ui-port $(UI_PORT))

# Stop all running services
stop: $(VENV_AMC)
	@$(VENV_AMC) stop

# Show service status
status: $(VENV_AMC)
	@$(VENV_AMC) status

# Ensure venv and amc CLI exist
$(VENV_AMC):
	python3 -m venv .venv
	.venv/bin/pip install --quiet --upgrade pip
	.venv/bin/pip install --quiet -e .

# First-time setup (also called automatically by start via dependency)
setup: $(VENV_AMC)
	@echo "✅ Setup complete."

# Clean up generated files
clean:
	@.venv/bin/amc stop 2>/dev/null || true
	rm -rf .venv runtime/ data/ .run/
	cd frontend && rm -rf node_modules dist
	@echo "✅ Clean complete."
