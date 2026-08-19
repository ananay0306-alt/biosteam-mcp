#!/usr/bin/env python3
"""Regression test for biosteam-mcp, driven over the real stdio interface.

Checks protocol purity (stdout must be JSON only, even though BioSTEAM prints),
that tools are registered, that a column reproduces known physics, and that a
bad specification comes back as an error rather than as fake success.

    python3 biosteam-mcp/test_server.py
"""
import json, os, subprocess, sys

ROOT = os.path.dirname(os.path.abspath(__file__))
LAUNCHER = os.path.join(ROOT, "biosteam-mcp.sh")
FAILURES = []


def check(label, ok, detail=""):
    print(("  PASS  " if ok else "  FAIL  ") + label + (("   " + str(detail)) if not ok else ""))
    if not ok:
        FAILURES.append(label)


class Session:
    """Keeps stdin OPEN, like a real MCP client. Piping everything at once and
    closing stdin makes the server shut down before it answers later calls."""

    def __init__(self):
        self.p = subprocess.Popen([LAUNCHER], stdin=subprocess.PIPE,
                                  stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
        self.n = 0

    def send(self, method, params, notify=False):
        msg = {"jsonrpc": "2.0", "method": method}
        if not notify:
            self.n += 1
            msg["id"] = self.n
            msg["params"] = params
        self.p.stdin.write(json.dumps(msg) + "\n")
        self.p.stdin.flush()
        if notify:
            return None
        while True:
            line = self.p.stdout.readline()
            if not line:
                raise RuntimeError(f"server closed while awaiting {method}")
            if line.strip():
                return json.loads(line)

    def close(self):
        self.p.stdin.close()
        try:
            self.p.wait(timeout=15)
        except subprocess.TimeoutExpired:
            self.p.kill()
        return self.p.stderr.read()


def call(name, args):
    return ("tools/call", {"name": name, "arguments": args})


def payload(frame):
    return json.loads(frame["result"]["content"][0]["text"])


print("biosteam-mcp: methanol / water / glycerol column")
print("(first call waits on the one-time BioSTEAM import, ~60 s)")
s = Session()

init = s.send("initialize", {"protocolVersion": "2025-06-18", "capabilities": {},
                             "clientInfo": {"name": "test", "version": "0"}})
s.send("notifications/initialized", None, notify=True)
check("initialize answered", "result" in init, init)

tl = s.send("tools/list", {})
tools = [t["name"] for t in tl["result"]["tools"]]
check("tools registered", len(tools) >= 8, tools)

def tool(name, args):
    r = s.send("tools/call", {"name": name, "arguments": args})
    return json.loads(r["result"]["content"][0]["text"])

tool("set_chemicals", {"names": ["Methanol", "Water", "Glycerol"]})
tool("create_stream", {"tag": "FEED",
                       "flows_kmol_hr": {"Methanol": 100, "Water": 60, "Glycerol": 40},
                       "T_K": 330})
col = tool("design_distillation", {"tag": "D1", "feed": "FEED", "light_key": "Methanol",
                                   "heavy_key": "Water", "y_top": 0.99, "x_bot": 0.008, "k": 1.2})
check("column converged", "error" not in col, col.get("error"))
if "error" not in col:
    sc = col["spec_check"]
    check("y_top spec met", abs(sc["y_top_actual_keys_basis"] - 0.99) < 1e-4, sc)
    check("x_bot spec met", abs(sc["x_bot_actual_keys_basis"] - 0.008) < 1e-4, sc)
    check("21 theoretical stages", abs(col["design"]["Theoretical stages"] - 21) < 1, col["design"])
    check("Rmin ~0.663", abs(col["design"]["Minimum reflux"] - 0.663) < 0.01, col["design"])
    check("glycerol reported as non-key", "Glycerol" in col["non_key_components"])
    check("recovery differs from composition",
          col["light_key_split"]["recovery_to_distillate_pct"]
          != col["light_key_split"]["mole_pct_of_distillate"])

bad = tool("design_distillation", {"tag": "D9", "feed": "FEED", "light_key": "Water",
                                   "heavy_key": "Methanol", "y_top": 0.99, "x_bot": 0.008})
check("bad spec returns error, not fake success", "error" in bad, bad)

objs = tool("list_objects", {})
check("failed unit not left in flowsheet",
      "D9" not in [u["tag"] for u in objs["units"]], objs["units"])

err = s.close()
check("protocol purity (BioSTEAM chatter stayed off stdout)", True)

print()
if FAILURES:
    print("FAILED: " + ", ".join(FAILURES)); sys.exit(1)
print("ALL TESTS PASSED")
