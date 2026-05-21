# genesys-mcp — convenience targets.
# install.sh is the main onboarding entry; these targets are for repeat-use.

.PHONY: help link-skills health test sync

help:
	@echo "  make sync         — uv sync (reinstall Python deps)"
	@echo "  make link-skills  — re-symlink every skill under skills/ into the"
	@echo "                      Claude Code skills directory (idempotent)"
	@echo "  make health       — run the genesys-mcp health check"
	@echo ""
	@echo "  For fresh installs, run ./install.sh instead — it covers the"
	@echo "  whole bootstrap (deps + credentials + MCP registration + skills"
	@echo "  + health check)."

sync:
	uv sync

# Detect the Claude Code skill dir and symlink everything under skills/
# into it. Idempotent — skips existing symlinks, leaves non-symlinks alone.
link-skills:
	@SKILLS_DIR=""; \
	for cand in "$$HOME/.claude/skills" "$$HOME/.agents/skills"; do \
	    if [ -d "$$cand" ]; then SKILLS_DIR="$$cand"; break; fi; \
	done; \
	SKILLS_DIR="$${SKILLS_DIR:-$$HOME/.claude/skills}"; \
	mkdir -p "$$SKILLS_DIR"; \
	for skill_dir in skills/*/; do \
	    name=$$(basename "$$skill_dir"); \
	    target="$$SKILLS_DIR/$$name"; \
	    if [ -L "$$target" ]; then \
	        echo "  ✓ $$name already linked"; \
	    elif [ -e "$$target" ]; then \
	        echo "  ! $$target exists as a non-symlink; skipping"; \
	    else \
	        ln -s "$$(pwd)/$$skill_dir" "$$target"; \
	        echo "  ✓ linked $$name → $$target"; \
	    fi \
	done

health:
	uv run python -m genesys_mcp.health_check
