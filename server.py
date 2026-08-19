#!/usr/bin/env python3
"""
biosteam-mcp — drive the BioSTEAM process simulator over MCP.

Design notes
------------
* BioSTEAM writes to stdout; MCP stdio needs stdout to carry only protocol
  JSON, so every tool body runs inside redirect_stdout(stderr).
* Errors are DATA. A failed simulate returns its exception text verbatim
  rather than raising, so a broken design can never be reported as success.
* get_results VERIFIES specs in Python rather than leaving the model to judge
  whether a target was met, and reports recovery and composition separately
  because those are different quantities that only coincide by accident.
"""

import contextlib
import functools
import json
import sys
import traceback
import warnings

warnings.filterwarnings("ignore")

from mcp.server.mcpserver import MCPServer

# BioSTEAM costs ~60 s to import (numba JIT). Loading it at module scope would
# blow the client's 30 s startup handshake, so it is deferred to the first tool
# call that actually needs it -- initialize/tools_list stay instant.
bst = None
tmo = None


def _load():
    global bst, tmo
    if bst is None:
        with contextlib.redirect_stdout(sys.stderr):
            import biosteam as _bst
            import thermosteam as _tmo
        bst, tmo = _bst, _tmo
    return bst, tmo

server = MCPServer(
    name="biosteam",
    instructions=(
        "BioSTEAM process simulation. Flow order: set_chemicals -> create_stream "
        "-> design_distillation / design_flash -> get_results. Flows are kmol/hr, "
        "T in K, P in Pa. get_results reports whether specs were actually met; "
        "trust that field, not the raw numbers. run_python is the escape hatch."
    ),
)

STATE: dict = {"chemicals": None}


def _quiet(fn):
    """Run a tool with stdout diverted to stderr, returning errors as data."""

    @functools.wraps(fn)   # keeps __wrapped__ so MCP can read the real signature
    def wrapper(*args, **kwargs):
        try:
            _load()
            with contextlib.redirect_stdout(sys.stderr):
                return fn(*args, **kwargs)
        except Exception as exc:  # noqa: BLE001 - errors are data
            return json.dumps(
                {
                    "error": f"{type(exc).__name__}: {exc}",
                    "traceback": traceback.format_exc().splitlines()[-4:],
                },
                indent=2,
            )

    return wrapper


def _need_chemicals():
    if STATE["chemicals"] is None:
        raise RuntimeError("No chemicals set. Call set_chemicals first.")


def _unit(tag):
    obj = bst.main_flowsheet.unit.search(tag)
    if obj is None:
        raise KeyError(f"No unit tagged {tag!r}. Units: {[u.ID for u in bst.main_flowsheet.unit]}")
    return obj


def _stream(tag):
    obj = bst.main_flowsheet.stream.search(tag)
    if obj is None:
        raise KeyError(f"No stream tagged {tag!r}. Streams: {[s.ID for s in bst.main_flowsheet.stream]}")
    return obj


def _stream_dict(s):
    """Composition on BOTH bases, because they are routinely confused."""
    total = float(s.F_mol)
    flows = {c: round(float(s.imol[c]), 6) for c in STATE["chemicals"] if s.imol[c] > 1e-9}
    return {
        "tag": s.ID,
        "T_K": round(float(s.T), 3),
        "P_Pa": round(float(s.P), 1),
        "phase": str(s.phase),
        "total_kmol_hr": round(total, 6),
        "total_kg_hr": round(float(s.F_mass), 4),
        "flows_kmol_hr": flows,
        "mole_fractions": {k: round(v / total, 6) for k, v in flows.items()} if total > 0 else {},
    }


@server.tool()
@_quiet
def set_chemicals(names: list[str]) -> str:
    """Define the chemical set and attach thermo. Clears any existing flowsheet.

    names: exact thermosteam/chemicals database names, e.g. ["Methanol","Water","Glycerol"].
    """
    bst.main_flowsheet.clear()
    chems = tmo.Chemicals(names)
    chems.compile()
    tmo.settings.set_thermo(chems)
    STATE["chemicals"] = list(chems.IDs)
    return json.dumps(
        {
            "chemicals": STATE["chemicals"],
            "boiling_points_K": {
                c.ID: (round(float(c.Tb), 2) if c.Tb else None) for c in chems
            },
        },
        indent=2,
    )


@server.tool()
@_quiet
def create_stream(
    tag: str,
    flows_kmol_hr: dict[str, float],
    T_K: float = 298.15,
    P_Pa: float = 101325.0,
    phase: str | None = None,
) -> str:
    """Create (or replace) a material stream. flows_kmol_hr maps chemical -> kmol/hr."""
    _need_chemicals()
    existing = bst.main_flowsheet.stream.search(tag)
    if existing is not None:
        existing.empty()
        for k, v in flows_kmol_hr.items():
            existing.imol[k] = v
        existing.T, existing.P = T_K, P_Pa
        if phase:
            existing.phase = phase
        s = existing
    else:
        s = tmo.Stream(tag, units="kmol/hr", T=T_K, P=P_Pa, **flows_kmol_hr)
        if phase:
            s.phase = phase
    return json.dumps(_stream_dict(s), indent=2)


@server.tool()
@_quiet
def get_stream(tag: str) -> str:
    """Full state of one stream: T, P, phase, flows, and mole fractions."""
    _need_chemicals()
    return json.dumps(_stream_dict(_stream(tag)), indent=2)


@server.tool()
@_quiet
def design_distillation(
    tag: str,
    feed: str,
    light_key: str,
    heavy_key: str,
    y_top: float,
    x_bot: float,
    k: float = 1.2,
    P_Pa: float = 101325.0,
    partial_condenser: bool = False,
) -> str:
    """Design + simulate a shortcut distillation column, then verify the specs.

    y_top: light-key mole fraction in the DISTILLATE, on a keys-only basis.
    x_bot: light-key mole fraction in the BOTTOMS, on a keys-only basis.
    NOTE this basis differs from DWSIM's ShortcutColumn, which divides by the
    TOTAL product stream including non-keys.
    k: reflux ratio as a multiple of minimum (R/Rmin).
    """
    _need_chemicals()
    f = _stream(feed)
    if bst.main_flowsheet.unit.search(tag) is not None:
        raise ValueError(f"Unit {tag!r} already exists; pick another tag or call reset.")
    col = bst.BinaryDistillation(
        tag,
        ins=f,
        outs=(f"{tag}_D", f"{tag}_B"),
        LHK=(light_key, heavy_key),
        y_top=y_top,
        x_bot=x_bot,
        k=k,
        P=P_Pa,
        is_divided=False,
        partial_condenser=partial_condenser,
    )
    try:
        col.simulate()
    except Exception:
        bst.main_flowsheet.unit.discard(col)   # do not leave a broken unit behind
        raise
    return _results(tag)


def _results(tag: str) -> str:
    unit = _unit(tag)
    out = {
        "tag": unit.ID,
        "type": type(unit).__name__,
        "inlets": [_stream_dict(s) for s in unit.ins],
        "outlets": [_stream_dict(s) for s in unit.outs],
    }

    if isinstance(unit, bst.BinaryDistillation):
        lk, hk = unit.LHK
        D, B = unit.outs
        feed = unit.ins[0]
        keys_D = float(D.imol[lk] + D.imol[hk])
        keys_B = float(B.imol[lk] + B.imol[hk])
        fed = float(feed.imol[lk])
        out["spec_check"] = {
            "light_key": lk,
            "heavy_key": hk,
            "y_top_specified": unit.y_top,
            "y_top_actual_keys_basis": round(float(D.imol[lk]) / keys_D, 6) if keys_D else None,
            "x_bot_specified": unit.x_bot,
            "x_bot_actual_keys_basis": round(float(B.imol[lk]) / keys_B, 6) if keys_B else None,
            "NOTE": "keys-only basis; divide by total stream for a DWSIM-style spec",
        }
        out["light_key_split"] = {
            "recovery_to_distillate_pct": round(100 * float(D.imol[lk]) / fed, 4) if fed else None,
            "mole_pct_of_distillate": round(100 * float(D.imol[lk]) / float(D.F_mol), 4) if D.F_mol else None,
            "mole_pct_of_bottoms": round(100 * float(B.imol[lk]) / float(B.F_mol), 4) if B.F_mol else None,
            "NOTE": "recovery and composition are different quantities; they coincide only by accident",
        }
        nonkeys = [c for c in STATE["chemicals"] if c not in (lk, hk) and feed.imol[c] > 1e-9]
        out["non_key_components"] = {
            c: {
                "fed_kmol_hr": round(float(feed.imol[c]), 6),
                "pct_to_distillate": round(100 * float(D.imol[c]) / float(feed.imol[c]), 3),
            }
            for c in nonkeys
        } or "none present"
        try:
            out["design"] = {k: (round(float(v), 4) if isinstance(v, (int, float)) else v)
                             for k, v in unit.design_results.items()}
            out["design"]["k_R_over_Rmin"] = unit.k
        except Exception:
            out["design"] = {}

    for attr, label in (("installed_cost", "installed_cost_USD"), ("utility_cost", "utility_cost_USD_hr")):
        try:
            out.setdefault("economics", {})[label] = round(float(getattr(unit, attr)), 2)
        except Exception:
            pass
    try:
        out["duties_kW"] = {
            hu.ID or f"utility{i}": round(float(hu.duty) / 3600, 3)
            for i, hu in enumerate(unit.heat_utilities)
            if abs(float(hu.duty)) > 1e-9
        }
    except Exception:
        pass
    return json.dumps(out, indent=2, default=str)


@server.tool()
@_quiet
def get_results(tag: str) -> str:
    """Results for a unit, including a Python-computed check of whether specs were met."""
    _need_chemicals()
    return _results(tag)


@server.tool()
@_quiet
def design_flash(
    tag: str,
    feed: str,
    T_K: float | None = None,
    P_Pa: float | None = None,
    V: float | None = None,
) -> str:
    """Design + simulate a flash vessel. Give any two of T_K, P_Pa, V (vapour fraction)."""
    _need_chemicals()
    f = _stream(feed)
    if bst.main_flowsheet.unit.search(tag) is not None:
        raise ValueError(f"Unit {tag!r} already exists; pick another tag or call reset.")
    kwargs = {key: val for key, val in (("T", T_K), ("P", P_Pa), ("V", V)) if val is not None}
    if len(kwargs) < 2:
        raise ValueError("Specify exactly two of T_K, P_Pa, V.")
    flash = bst.Flash(tag, ins=f, outs=(f"{tag}_vap", f"{tag}_liq"), **kwargs)
    try:
        flash.simulate()
    except Exception:
        bst.main_flowsheet.unit.discard(flash)
        raise
    return _results(tag)


@server.tool()
@_quiet
def list_objects() -> str:
    """Everything in the current session: chemicals, streams, units."""
    return json.dumps(
        {
            "chemicals": STATE["chemicals"],
            "streams": [s.ID for s in bst.main_flowsheet.stream],
            "units": [{"tag": u.ID, "type": type(u).__name__} for u in bst.main_flowsheet.unit],
        },
        indent=2,
    )


@server.tool()
@_quiet
def reset() -> str:
    """Clear the flowsheet and chemicals; start over."""
    bst.main_flowsheet.clear()
    STATE["chemicals"] = None
    return json.dumps({"status": "cleared"})


@server.tool()
@_quiet
def run_python(code: str) -> str:
    """ESCAPE HATCH: exec Python against the live session.

    Namespace has bst, tmo, json, and the flowsheet. Assign to `result` to
    return a value; anything printed is captured and returned too.
    """
    _load()
    buf = __import__("io").StringIO()
    ns = {
        "bst": bst,
        "tmo": tmo,
        "json": json,
        "flowsheet": bst.main_flowsheet,
        "chemicals": STATE["chemicals"],
        "result": None,
    }
    with contextlib.redirect_stdout(buf):
        exec(code, ns)  # noqa: S102 - deliberate escape hatch
    return json.dumps(
        {"result": ns.get("result"), "stdout": buf.getvalue()}, indent=2, default=str
    )


if __name__ == "__main__":
    server.run(transport="stdio")
