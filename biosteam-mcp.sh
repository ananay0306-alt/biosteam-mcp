#!/bin/zsh
# Launcher for biosteam-mcp.
#
# Picks a Python that has BioSTEAM installed, in this order:
#   1. $BIOSTEAM_MCP_PYTHON        (explicit override)
#   2. <repo>/.venv/bin/python     (venv inside the clone)
#   3. <repo>/../.venv/bin/python  (shared venv one level up)
#   4. python3 on PATH
#
# stdout must carry only MCP protocol JSON; server.py diverts BioSTEAM's
# chatter to stderr, and test_server.py asserts that it stays that way.
HERE="${0:A:h}"

for CANDIDATE in \
    "$BIOSTEAM_MCP_PYTHON" \
    "$HERE/.venv/bin/python" \
    "$HERE/../.venv/bin/python" \
    "$(command -v python3)"
do
    if [[ -n "$CANDIDATE" && -x "$CANDIDATE" ]]; then
        # find_spec locates the package WITHOUT importing it -- a real
        # `import biosteam` here would cost ~70 s of numba JIT per launch.
        if "$CANDIDATE" -c 'import importlib.util,sys; sys.exit(0 if importlib.util.find_spec("biosteam") else 1)' 2>/dev/null; then
            exec "$CANDIDATE" "$HERE/server.py" "$@"
        fi
    fi
done

print -u2 "biosteam-mcp: no Python with BioSTEAM found."
print -u2 "  python3 -m venv .venv && ./.venv/bin/pip install biosteam mcp"
print -u2 "  or set BIOSTEAM_MCP_PYTHON to an interpreter that has it."
exit 1
