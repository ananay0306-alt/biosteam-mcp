# biosteam-mcp

An MCP server that puts [BioSTEAM](https://biosteam.readthedocs.io) inside Claude,
so process designs can be built, solved and interrogated conversationally.

Sister project to `dwsim-mcp`. BioSTEAM is pure Python, so this server is ~340
lines instead of 1,689 — no .NET marshalling, no Rosetta, no fd surgery.

## Install

Requires a Python with `biosteam` and `mcp` installed. From a fresh clone:

```bash
python3 -m venv .venv
./.venv/bin/pip install biosteam mcp
claude mcp add --scope user biosteam "$(pwd)/biosteam-mcp.sh"
./.venv/bin/python test_server.py        # ~2 min, ends ALL TESTS PASSED
```

The launcher finds an interpreter automatically: `$BIOSTEAM_MCP_PYTHON`, then
`.venv/bin/python` in the clone, then `../.venv/bin/python`, then `python3`.

**macOS note.** Homebrew's python@3.11/3.12/3.13 bottles link against a newer
libexpat than macOS 26 ships, which breaks `pyexpat` and therefore `pip` inside
a venv. Python 3.14 works.

## Tools

| Tool | Purpose |
|---|---|
| `set_chemicals` | Define the chemical set + attach thermo. Returns boiling points. |
| `create_stream` | Material stream: flows in kmol/hr, T in K, P in Pa. |
| `get_stream` | Full state: T, P, phase, flows, mole fractions. |
| `design_distillation` | Shortcut column (`BinaryDistillation`) + spec verification. |
| `design_flash` | Flash vessel; give any two of T, P, vapour fraction. |
| `get_results` | Unit results with a **Python-computed** spec check. |
| `list_objects` | Chemicals, streams, units in the session. |
| `reset` | Clear everything. |
| `run_python` | Escape hatch: exec against the live session. |

## Design decisions

**Errors are data.** A failed `simulate()` returns its exception text verbatim
rather than raising, so a broken design can never be reported as success. The
regression test asserts this with a deliberately reversed key pair.

**Specs are verified in Python, not by the model.** `get_results` computes
whether `y_top` and `x_bot` were actually achieved and returns the comparison.
The model explains a fact instead of inventing an interpretation of one.

**Recovery and composition are reported separately.** These are different
quantities that coincide only when the light key's feed flow happens to equal
the bottoms total flow. For a dilute feed they diverge by 50x. Reporting one as
if it answered the other is an easy and invisible mistake.

**Non-key components are reported.** Nothing in an LHK spec constrains them,
so the results say explicitly where each non-key landed.

**Lazy import.** BioSTEAM costs ~70 s to import (numba JIT, not cached between
processes). Loading it at module scope blows the client's 30 s startup
handshake, so it is deferred to the first tool call. Handshake is ~2 s.

**stdout is protocol-only.** BioSTEAM prints; every tool body runs inside
`redirect_stdout(stderr)`, and the test asserts stdout parses as pure JSON.

## Porting note: BioSTEAM vs DWSIM specification bases

Both simulators accept a number like "0.01" for a column spec, against
**different denominators**. Transferring values directly gives a converged,
balance-consistent, materially wrong answer.

| | BioSTEAM | DWSIM `ShortcutColumn` |
|---|---|---|
| Key spec basis | light key / (LK + HK) — keys only | heavy key / **total** distillate |
| Conversion | `DWSIM_HK_spec = (1 - y_top) x (LK + HK) / total_distillate` | |
| Reaction extent | `ParallelReaction` applies each X to the **original** inlet | each conversion applies to the **remaining** base compound |
| Conversion | | `X_seq(i) = X_par(i) / (1 - sum of X_par before i)` |

Both were found while replicating an ethanol-to-jet model across the two tools.
Each produces a wrong result that converges cleanly with perfect mass and
carbon balances.

## Attribution

Designed, written and tested with [Claude](https://claude.com) driving BioSTEAM
directly — the regression suite was run against real column solves, not mocked.

## Why the numbers move fast

| | Time |
|---|---|
| `import biosteam` | ~70 s, once per process |
| chemicals setup | 0.05 s |
| first column solve | 0.29 s |
| every solve after | **0.01 s** |

The persistent session pays the import once, so iterating on a design
("try reflux 1.2, now 1.5") is effectively instant.

## Licence

Proprietary — all rights reserved. See [LICENSE](LICENSE). This code may not be
used, copied, modified or distributed without written permission.

BioSTEAM, thermosteam and the MCP SDK are dependencies used under their own
permissive terms; none of their code is redistributed in this repository.
