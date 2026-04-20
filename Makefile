.PHONY: help build build-simple test test-unit clean install

help:
	@echo "onionpress Build System"
	@echo ""
	@echo "Available targets:"
	@echo "  make build        - Build DMG with custom window (requires UI)"
	@echo "  make build-simple - Build DMG without customization (faster)"
	@echo "  make test         - Test the app bundle locally"
	@echo "  make clean        - Clean build artifacts"
	@echo "  make install      - Install app to /Applications (for testing)"
	@echo ""

build:
	@echo "Building DMG with customization..."
	./build/build-dmg.sh

build-simple:
	@echo "Building simple DMG..."
	./build/build-dmg-simple.sh

test:
	@echo "Testing source layout..."
	@echo "Checking structure..."
	@test -d app/MacOS || (echo "ERROR: app/MacOS directory missing" && exit 1)
	@test -f app/MacOS/launcher-wrapper.swift || (echo "ERROR: launcher-wrapper.swift missing" && exit 1)
	@test -f app/MacOS/onionpress || (echo "ERROR: onionpress script missing" && exit 1)
	@test -f app/Info.plist || (echo "ERROR: Info.plist missing" && exit 1)
	@test -f app/Resources/docker/docker-compose.yml || (echo "ERROR: docker-compose.yml missing" && exit 1)
	@test -f src/menubar.py || (echo "ERROR: src/menubar.py missing" && exit 1)
	@test -f src/onionpress/key_manager.py || (echo "ERROR: src/onionpress/key_manager.py missing" && exit 1)
	@echo "All required source files present"
	@echo ""
	@echo "Checking permissions..."
	@test -x app/MacOS/onionpress || (echo "ERROR: onionpress not executable" && exit 1)
	@echo "Permissions correct"
	@echo ""
	@echo "Source layout is valid!"
	@echo "To build: make build-simple"

test-unit:
	@# Run the Python unit tests under a pinned 3.14 via uv. Some modules
	@# in src/onionpress/ use `X | None` syntax that fails to import on
	@# stock /usr/bin/python3 (3.9) on macOS — uv fetches an isolated
	@# 3.14 into ~/.local/share/uv/ without touching system Python.
	@if ! command -v uv >/dev/null 2>&1; then \
		echo "ERROR: 'uv' is not installed."; \
		echo "Install with:  curl -LsSf https://astral.sh/uv/install.sh | sh"; \
		echo "          or:  brew install uv"; \
		exit 1; \
	fi
	uv run --python 3.14 python -m unittest discover tests -p 'test_*.py'

clean:
	@echo "Cleaning build artifacts..."
	rm -rf build/*.dmg
	rm -rf build/temp.dmg
	rm -rf OnionPress.app
	@echo "Build artifacts cleaned"

install:
	@echo "Installing to /Applications..."
	@if [ -d "/Applications/OnionPress.app" ]; then \
		echo "Removing existing installation..."; \
		rm -rf "/Applications/OnionPress.app"; \
	fi
	cp -R OnionPress.app /Applications/
	@echo "Installed to /Applications/OnionPress.app"
	@echo "You can now launch it from Applications or Spotlight"
