.PHONY: all build audit check clean

all: build

build:
	@bash scripts/build-local.sh

audit:
	@bash scripts/unbound-audit.sh $(CONF)

check:
	@python3 scripts/check-redaction.py

clean:
	@rm -rf dist build book/cover.png
	@echo "  cleaned dist/ build/ cover.png"
