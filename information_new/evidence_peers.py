"""
evidence_peers.py - turns peer_comparison.json (written by sec_peers.py) into facts for the Five Whys.

Each fact is one sentence: where the target stands against named peers for one ratio, with every number calculated by
code from the target's approved statements and the peers' SEC filings. Facts show THAT the target differs, never WHY.
"""
from __future__ import annotations

PCT = {"operating_margin", "net_margin", "gross_margin", "revenue_growth", "net_income_growth",
       "ppe_share_of_assets", "inventory_share_of_assets", "debt_ratio", "equity_ratio", "retained_earnings_to_equity"}
POS = {"above_peers": "above the peer median", "below_peers": "below the peer median", "in_line": "in line with the peer median"}


def _fmt(metric: str, v) -> str:
    if v is None:
        return "n/a"
    return f"{v * 100:.1f}%" if metric in PCT else f"{v:.2f}x"


def peer_facts(comp: dict) -> list[dict]:
    target = comp.get("target") or {}
    tm = target.get("metrics") or {}
    peers = [p for p in comp.get("peers") or [] if p.get("values_used")]
    if not peers or not tm:
        return []
    who = target.get("company") or "The company"
    facts = []
    names = ", ".join(f"{p['name']} (fiscal year ending {p['period_used']})" for p in peers)
    gaps = [p for p in peers if not p.get("aligned", True)]
    facts.append({"id": "fact:peer:set", "kind": "peer", "computed": True, "fields": [], "periods": [],
                  "text": f"Peer set for the comparison: {names}. Peer figures are company-wide annual numbers from their SEC filings.",
                  "kw": "peers competitors benchmark comparison compared industry set",
                  "caution": bool(gaps)})
    for m, row in tm.items():
        used = [(p["name"], p["values_used"].get(m), p["period_used"]) for p in peers if p["values_used"].get(m) is not None]
        if row.get("latest") is None or not used:
            continue
        med = (comp.get("median") or {}).get(m, {})
        vm = row.get("vs_median") or {}
        pos = POS.get(vm.get("position"), "")
        label = m.replace("_", " ")
        text = (f"{label.capitalize()} compared with peers: {who} {_fmt(m, row['latest'])} (period ending {row.get('as_of')}); "
                + "; ".join(f"{n} {_fmt(m, v)} (fiscal year ending {p})" for n, v, p in used)
                + (f"; peer median {_fmt(m, med.get('value'))} across {med.get('n_peers')} peers" if med else "")
                + (f". {who} is {pos}." if pos else "."))
        flags = []
        if any(not p.get("aligned", True) for p in peers if p["values_used"].get(m) is not None):
            flags.append("some peers' fiscal years end months away from the company's period")
        if row.get("uses_partial_input") or m in ("debt_to_equity",):
            flags.append("debt definitions can differ between companies")
        caution = ("Data caution: " + "; ".join(flags) + ". Treat with care.") if flags else ""
        facts.append({"id": f"fact:peer:{m}", "kind": "peer", "computed": True, "fields": [m], "periods": [row.get("as_of")],
                      "text": (text + " " + caution).strip(),
                      "kw": f"{label} peers competitors benchmark comparison compared above below relative industry",
                      "caution": bool(caution)})
    return facts
