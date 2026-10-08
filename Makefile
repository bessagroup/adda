.DEFAULT_GOAL := help

PACKAGEDIR := dist
COVERAGEREPORTDIR := coverage_html_report

.PHONY: help test test-html build docs lint paper promptmap promptmap-status prepush attest-paper decline-paper

help:
	@echo "Please use \`make <target>' where <target> is one of:"
	@echo "  test        Run the tests with pytest"
	@echo "  test-html   Run the tests and open the HTML coverage report"
	@echo "  build       Build the package"
	@echo "  docs        Build the documentation with mkdocs"
	@echo "  lint        Lint the code with ruff"
	@echo "  paper       Rebuild paper/main.pdf reproducibly"
	@echo "  promptmap   Regenerate internal/promptmap.html (prompt + gate provenance)"
	@echo "  promptmap-status  Is the map in sync with the code, and with what is published?"
	@echo "  prepush     Run what CI runs: ruff, tests, promptmap diff, promptmap tests"
	@echo "  attest-paper   Sign that paper/ reflects the code as of HEAD"
	@echo "  decline-paper  WHY='...' Sign, recording that no decision was taken"

test:
	uv run pytest -m "not integration and not ollama and not corpus"

test-html:
	pytest
	xdg-open ./$(COVERAGEREPORTDIR)/index.html

build:
	uv build

docs:
	mkdocs build

lint:
	ruff check

# Pinned to 3.12: promptmap.py resolves citations via `ast`, whose line
# attribution differs between 3.10/3.11 and 3.12/3.13 -- an unpinned run
# regenerates byte-different output depending on whichever interpreter is
# in .venv, and CI's raw-byte `check_promptmap` job is itself pinned to
# 3.12, so this must match it exactly.
promptmap:
	uv run --python 3.12 python internal/tools/promptmap.py

promptmap-status:
	uv run python internal/tools/promptmap_sync.py

# Mirrors .github/workflows/pull_request.yml: run-tests, check_promptmap (the
# raw diff of internal/promptmap.html, then `pytest -m promptmap`). Re-read
# that file when it changes. The API keys are unset so no wet test can run.
prepush:
	uv run ruff check
	env -u OPENROUTER_API_KEY -u ANTHROPIC_API_KEY uv run pytest -n auto -m "not integration and not ollama and not corpus and not promptmap and not docling" -p no:cacheprovider --no-cov
	uv run --python 3.12 python internal/tools/promptmap.py -o /tmp/promptmap.html
	diff -q internal/promptmap.html /tmp/promptmap.html
	uv run pytest -m promptmap --no-cov -n0 -p no:cacheprovider

attest-paper:
	uv run python internal/tools/paper_attestation.py --attest

decline-paper:
	@test -n "$(WHY)" || (echo "usage: make decline-paper WHY='no design decision here'"; exit 1)
	uv run python internal/tools/paper_attestation.py --decline "$(WHY)"

# The committed paper/main.pdf is a generated artifact, so it must not depend
# on WHEN or WHERE it was built: pdfTeX stamps a creation date and a random
# document /ID, which would make every rebuild a diff even with the .tex
# unchanged (and a binary, unmergeable one). SOURCE_DATE_EPOCH + the
# \pdftrailerid{} in main.tex pin both, so the PDF only changes when the paper
# does. Deliberately NOT gated in CI -- a stale PDF is not a correctness
# failure, and nobody's build should break over a document.
paper:
	SOURCE_DATE_EPOCH=1600000000 FORCE_SOURCE_DATE=1 \
	  latexmk -cd -pdf -interaction=nonstopmode -outdir=build paper/main.tex
	cp paper/build/main.pdf paper/main.pdf
	@echo "paper/main.pdf rebuilt"
