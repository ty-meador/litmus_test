# litmus — development entry points. All tooling config lives in pyproject.toml.
.PHONY: install format lint fix typecheck test coverage audit build smoke docs check soak nightly mutants hooks clean \
	bench-serial bench-serial-up bench-serial-down bench-ssh bench-ssh-down bench-pihole bench-pihole-down \
	bench-coap bench-coap-down bench-root-setup bench-can bench-i2c bench-gpio bench-up bench-down \
	bench-suite-serial bench-suite-guest bench-suite-pihole bench-suite-coap bench-suite-gpio bench-rack bench-soak

install:  ## Sync venv (incl. dev deps) and install git hooks
	uv sync
	$(MAKE) hooks

format:  ## Auto-format
	uv run ruff format .

lint:  ## Lint with auto-fix
	uv run ruff check --fix .

fix: format lint  ## Auto-format + auto-fix lint

typecheck:  ## Pyright strict
	uv run pyright
	uv run python tools/import_cycles.py

test:  ## Run the framework self-test suite
	uv run pytest

coverage:  ## Self-tests under branch coverage; global and per-file floors enforced
	uv run pytest --cov --cov-report=term --cov-report=json --cov-report=xml
	uv run python tools/coverage_floor.py

audit:  ## Vulnerability scan over the locked dependency set (network required)
	mkdir -p .audit
	uv export -q --locked --format requirements-txt --no-emit-project -o .audit/requirements.txt
	uv run pip-audit -r .audit/requirements.txt --disable-pip --require-hashes --strict --progress-spinner off

build:  ## Wheel and sdist into dist/; a package that cannot build is red before it is tagged
	rm -rf dist
	uv build

# The smoke runs from inside .smoke/ so the source tree is not on the path: what runs
# is the wheel, not src/. Only the two entry points the floor uses are exercised; the
# self-tests already cover behaviour, this proves the package imports and starts.
# The old venv is removed before the build, not after: an sdist that sweeps in a venv
# fails to build, and .gitignore is a courtesy the build must not depend on.
smoke:  ## Install the wheel into a clean venv and run both console entry points
	rm -rf .smoke
	$(MAKE) build
	uv venv -q .smoke/venv
	uv pip install -q --python .smoke/venv/bin/python dist/*.whl
	cd .smoke && venv/bin/litmus-check --list-rules > /dev/null
	cd .smoke && venv/bin/litmus run ../examples/hello_world.py

check:  ## The gate (there is no CI) — verify only, no auto-fix. Green before push; the reviewer re-runs it before merge.
	uv run ruff format --check .
	uv run ruff check .
	uv run pyright
	uv run python tools/import_cycles.py
	$(MAKE) coverage
	uv run litmus-check examples
	$(MAKE) smoke
	$(MAKE) docs
	$(MAKE) audit

docs:  ## Documentation site; strict: a broken cross-reference is red
	uv run mkdocs build --strict

# mkdocs refuses a site_dir inside docs_dir, so the build lands in site/ and moves.
docs-release:  ## The same build moved to docs/site/, committed on a release branch only; main never carries it (#138)
	uv run mkdocs build --strict
	rm -rf docs/site
	mv site docs/site

SOAK_RUNS ?= 8
RACK_REPEATS ?= 20

soak:  ## Race gate — full suite SOAK_RUNS times; one green run proves nothing about timing
	@i=1; while [ $$i -le $(SOAK_RUNS) ]; do \
		echo "soak $$i/$(SOAK_RUNS)"; \
		uv run pytest -q || exit 1; \
		i=$$((i + 1)); \
	done

nightly:  ## The nightly soak at the bench (soak + rack suite RACK_REPEATS times); prints, posts nothing
	SOAK_RUNS=$(SOAK_RUNS) RACK_REPEATS=$(RACK_REPEATS) uv run python tools/nightly_soak.py --no-post

nightly-record:  ## The nightly soak, recorded on issue #68 (needs LITMUS_BOT_TOKEN; DISCORD_WEBHOOK_URL for a red night)
	SOAK_RUNS=$(SOAK_RUNS) RACK_REPEATS=$(RACK_REPEATS) uv run python tools/nightly_soak.py

deps-plan:  ## Print the uv.lock upgrade plan; touches nothing
	uv run python tools/dependency_update.py --dry-run

deps-update:  ## Refresh uv.lock and open the update MR (needs LITMUS_BOT_TOKEN); run weekly at the bench
	uv run python tools/dependency_update.py

mutants:  ## Mutation audit over the kernel of trust (verdict/base/lifecycle); not a gate, see STANDARDS.md §6
	uv run mutmut run
	uv run mutmut results

hooks:  ## Point git at the shared hooks (auto-lint on commit)
	git config core.hooksPath .githooks

# Bench (bench/README.md): sibling to examples/, models real-world use of litmus. None of
# these is reached by `make check`, and every one exits non-zero on a [FAIL] line.
BENCH_PY = PYTHONPATH=. uv run python -m
bench-serial:  ## Serial contract over a socat pty pair (no root)
	$(BENCH_PY) bench.serial_pair check
bench-serial-up:  ## Leave a pty pair up with the console on the far end (SERIAL_DEVICE for the suites)
	$(BENCH_PY) bench.serial_pair up
bench-serial-down:  ## Stop the pair
	$(BENCH_PY) bench.serial_pair down
bench-ssh:  ## SSH contract + the shipped SSH example against a Debian guest under QEMU/KVM
	$(BENCH_PY) bench.qemu_ssh up
	$(BENCH_PY) bench.qemu_ssh check
	SSH_HOST=127.0.0.1 SSH_PORT=$${LITMUS_BENCH_SSH_PORT:-2222} SSH_USER=bench SSH_PASSWORD=bench \
		PYTHONPATH=. uv run litmus run bench/suites/ssh_lab.py
bench-ssh-down:  ## Stop the guest
	$(BENCH_PY) bench.qemu_ssh down
bench-pihole:  ## SSH + HTTP contracts and the shipped Pi-hole example against the Pi-hole container
	$(BENCH_PY) bench.pihole up
	$(BENCH_PY) bench.pihole check
	TARGET_ADDRESS=127.0.0.1 TARGET_PORT=$${LITMUS_BENCH_PIHOLE_SSH_PORT:-2223} TARGET_USER=root TARGET_PASSWORD=bench \
		PYTHONPATH=. uv run litmus run bench/suites/pihole_lab.py
bench-pihole-down:  ## Remove the Pi-hole container
	$(BENCH_PY) bench.pihole down
bench-coap:  ## Device API over CoAP against an aiocoap container
	$(BENCH_PY) bench.coap_server up
	$(BENCH_PY) bench.coap_server check
bench-coap-down:  ## Remove the CoAP container
	$(BENCH_PY) bench.coap_server down
bench-root-setup:  ## Load vcan/vxcan, i2c-stub, and a gpio-sim chip (the one step that needs root)
	sudo sh bench/root_setup.sh
bench-can:  ## SocketCAN round trip over the vxcan pair
	$(BENCH_PY) bench.can_pair
bench-i2c:  ## SMBus write/read-back against i2c-stub
	$(BENCH_PY) bench.i2c_stub
bench-gpio:  ## Input, output, and edge events against a gpio-sim chip
	PYTHONPATH=. uv run --group bench python -m bench.gpio_sim
bench-up:  ## Bring every bench device up (serial pair, guest, Pi-hole, CoAP); idempotent
	$(BENCH_PY) bench.serial_pair up
	$(BENCH_PY) bench.qemu_ssh up
	$(BENCH_PY) bench.pihole up
	$(BENCH_PY) bench.coap_server up
bench-down: bench-ssh-down bench-pihole-down bench-coap-down bench-serial-down  ## Stop every bench device

# The real suites (bench/suites/): written the way a developer writes against a rack,
# aimed at the surrogates above. Each target assumes its device is up (`make bench-up`).
BENCH_ENV = SERIAL_DEVICE=$$(cat $${LITMUS_BENCH_CACHE:-$$HOME/.cache/litmus-bench}/serial/device 2>/dev/null) \
	GUEST_HOST=127.0.0.1 GUEST_PORT=$${LITMUS_BENCH_SSH_PORT:-2222} GUEST_USER=bench GUEST_PASSWORD=bench \
	PIHOLE_HOST=127.0.0.1 PIHOLE_SSH_PORT=$${LITMUS_BENCH_PIHOLE_SSH_PORT:-2223} PIHOLE_USER=root PIHOLE_PASSWORD=bench \
	PIHOLE_API_URL=http://127.0.0.1:$${LITMUS_BENCH_PIHOLE_HTTP_PORT:-8080} PIHOLE_API_PASSWORD=bench \
	COAP_URL=coap://127.0.0.1:$${LITMUS_BENCH_COAP_PORT:-5683}
BENCH_RUN = $(BENCH_ENV) PYTHONPATH=. uv run litmus run
bench-suite-serial:  ## Console by tty path: prompt sync, status, large reply, cancel
	$(BENCH_RUN) bench/suites/serial_console.py
bench-suite-guest:  ## Guest acceptance over SSH: survey, audit, wait_for, measurements
	$(BENCH_RUN) bench/suites/guest_acceptance.py
bench-suite-pihole:  ## Pi-hole as a gateway: hop, REST deny-list round trip, dig behind the hop
	$(BENCH_RUN) bench/suites/pihole_rack.py::PiHole
bench-suite-coap:  ## CoAP device: hello, 4.04, echo per payload size
	$(BENCH_RUN) bench/suites/coap_device.py
bench-suite-gpio:  ## gpio-sim header through gpiod inside the claims (needs root setup)
	PYTHONPATH=. uv run --group bench litmus run bench/suites/gpio_lines.py
bench-rack:  ## Every device under one parallel owner
	$(BENCH_RUN) bench/suites/rack.py
RACK_RUNS ?= 10
bench-soak:  ## The rack RACK_RUNS times, every run recorded under .soak/bench/; red if any run is not Pass
	@mkdir -p .soak/bench; rm -f .soak/bench/rack-*.json; i=1; red=0; while [ $$i -le $(RACK_RUNS) ]; do \
		start=$$(date +%s); \
		$(BENCH_RUN) --json bench/suites/rack.py > .soak/bench/rack-$$i.json 2> .soak/bench/rack-$$i.log; rc=$$?; \
		echo "rack $$i/$(RACK_RUNS): exit $$rc in $$(( $$(date +%s) - start ))s"; \
		[ $$rc -eq 0 ] || red=1; \
		i=$$((i + 1)); \
	done; exit $$red

clean:
	rm -rf .venv .pytest_cache .ruff_cache .coverage .audit .soak .smoke .mutants dist site
	find . -type d -name __pycache__ -exec rm -rf {} +
