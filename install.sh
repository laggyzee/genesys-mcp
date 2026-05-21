#!/usr/bin/env bash
# genesys-mcp installer — clones the repo (if needed), syncs deps, prompts for
# OAuth creds, registers the MCP with Claude Code, symlinks every skill into
# ~/.claude/skills/ (or ~/.agents/skills/), and runs a final health check.
#
# Usage:
#   curl -fsSL https://raw.githubusercontent.com/laggyzee/genesys-mcp/main/install.sh | bash
#   # or from a cloned repo:
#   ./install.sh
#
# Idempotent — re-running upgrades cleanly. Safe to interrupt and resume.

set -euo pipefail

# ── Pretty output ───────────────────────────────────────────────────────
if [[ -t 1 ]]; then
    BOLD=$'\033[1m'; GREEN=$'\033[32m'; YELLOW=$'\033[33m'; RED=$'\033[31m'; RESET=$'\033[0m'
else
    BOLD=""; GREEN=""; YELLOW=""; RED=""; RESET=""
fi
say()  { printf "%s\n" "${BOLD}» $*${RESET}"; }
ok()   { printf "  %s%s%s %s\n" "$GREEN" "✓" "$RESET" "$*"; }
warn() { printf "  %s%s%s %s\n" "$YELLOW" "!" "$RESET" "$*"; }
die()  { printf "  %s%s%s %s\n" "$RED" "✗" "$RESET" "$*"; exit 1; }

# ── Locate or clone the repo ────────────────────────────────────────────
REPO_URL="${GENESYS_MCP_REPO_URL:-https://github.com/laggyzee/genesys-mcp.git}"
DEFAULT_DIR="${HOME}/code/genesys-mcp"

if [[ -f "./pyproject.toml" ]] && grep -q "genesys-mcp" pyproject.toml 2>/dev/null; then
    REPO_DIR="$(pwd)"
    say "Using repo at $REPO_DIR"
else
    REPO_DIR="${GENESYS_MCP_DIR:-$DEFAULT_DIR}"
    if [[ -d "$REPO_DIR/.git" ]]; then
        say "Repo already cloned at $REPO_DIR; pulling latest"
        git -C "$REPO_DIR" pull --ff-only || warn "git pull failed; continuing with current checkout"
    else
        say "Cloning $REPO_URL into $REPO_DIR"
        mkdir -p "$(dirname "$REPO_DIR")"
        git clone "$REPO_URL" "$REPO_DIR"
    fi
    cd "$REPO_DIR"
fi

# ── Sync Python deps ───────────────────────────────────────────────────
say "Installing Python dependencies (uv sync)"
if ! command -v uv >/dev/null 2>&1; then
    die "uv is not installed. Install with: curl -LsSf https://astral.sh/uv/install.sh | sh"
fi
uv sync --quiet
ok "dependencies installed"

# ── OAuth credentials ──────────────────────────────────────────────────
ENV_PATH="${HOME}/.config/genesys-mcp.env"
say "OAuth credentials → $ENV_PATH"
if [[ -f "$ENV_PATH" ]] && grep -q "^GENESYS_CLIENT_ID=" "$ENV_PATH" 2>/dev/null; then
    ok "existing creds found at $ENV_PATH (skipping prompt; edit by hand to change)"
else
    mkdir -p "$(dirname "$ENV_PATH")"
    chmod 700 "$(dirname "$ENV_PATH")"
    printf "    %s\n" "(Find these in Genesys Admin → Integrations → OAuth → your read-only Client Credentials client.)"
    read -r -p "    GENESYS_CLIENT_ID: " GC_ID
    read -r -s -p "    GENESYS_CLIENT_SECRET: " GC_SECRET; echo
    read -r -p "    GENESYS_REGION [ap-southeast-2]: " GC_REGION
    GC_REGION="${GC_REGION:-ap-southeast-2}"
    cat > "$ENV_PATH" <<EOF
GENESYS_CLIENT_ID=$GC_ID
GENESYS_CLIENT_SECRET=$GC_SECRET
GENESYS_REGION=$GC_REGION
EOF
    chmod 600 "$ENV_PATH"
    ok "credentials written"
fi

# ── Register MCP with Claude Code ──────────────────────────────────────
say "Registering the genesys MCP with Claude Code"
if command -v claude >/dev/null 2>&1; then
    if claude mcp list 2>/dev/null | grep -q "^genesys"; then
        ok "MCP already registered (use 'claude mcp remove genesys' to re-register)"
    else
        # Build the launch command the same way RELEASE-NOTES suggests.
        claude mcp add --scope user genesys \
            -- uv run --directory "$REPO_DIR" python -m genesys_mcp.server
        ok "MCP registered (scope: user)"
    fi
else
    warn "claude CLI not found — add this snippet to ~/.claude/mcp.json by hand:"
    cat <<EOF
  "genesys": {
    "command": "uv",
    "args": ["run", "--directory", "$REPO_DIR", "python", "-m", "genesys_mcp.server"]
  }
EOF
fi

# ── Symlink skills ─────────────────────────────────────────────────────
say "Symlinking companion skills"
# Detect the skills directory — Claude Code uses ~/.claude/skills/; some
# older installs use ~/.agents/skills/. Pick whichever exists, default
# to ~/.claude/skills/.
SKILLS_DIR=""
for cand in "${HOME}/.claude/skills" "${HOME}/.agents/skills"; do
    if [[ -d "$cand" ]]; then
        SKILLS_DIR="$cand"
        break
    fi
done
SKILLS_DIR="${SKILLS_DIR:-${HOME}/.claude/skills}"
mkdir -p "$SKILLS_DIR"

for skill_dir in "$REPO_DIR"/skills/*/; do
    name="$(basename "$skill_dir")"
    [[ "$name" == "README.md" ]] && continue
    target="$SKILLS_DIR/$name"
    if [[ -L "$target" ]]; then
        ok "$name already linked"
    elif [[ -e "$target" ]]; then
        warn "$target exists as a non-symlink; not touching"
    else
        ln -s "$skill_dir" "$target"
        ok "linked $name → $target"
    fi
done

# ── Health check ───────────────────────────────────────────────────────
say "Running health check"
if uv run python -m genesys_mcp.health_check; then
    echo
    say "Setup complete."
    echo
    echo "  Next: run the genesys-tenant-setup skill inside Claude Code to"
    echo "  populate ~/.config/genesys-mcp/tenant.yaml — auto-discovers most"
    echo "  values from your tenant, asks 6–8 questions for the rest."
    echo
    echo "  Then try:  \"do the monthly CC report for last month\""
    echo
    exit 0
else
    echo
    warn "Health check reported issues — review the output above. Most likely"
    warn "your OAuth client is missing a required scope. Re-run install.sh"
    warn "(or 'uv run python -m genesys_mcp.health_check') after fixing."
    exit 1
fi
