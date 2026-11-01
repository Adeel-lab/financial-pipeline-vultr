import json
from planning import analyze
from llm_client import llm_json
from label_mapper import resolve

client = "GM"
bs_path = r"C:\Users\adeel\Desktop\notebooks\vultr\assets\GM_balance_sheet_annual_2020_2026.csv"
is_path = r"C:\Users\adeel\Desktop\notebooks\vultr\assets\GM_income_statement_annual_2020_2026.csv"

CONTEXT_NOTES = {
    "GM": "Statements are consolidated and include a captive finance arm (GM Financial); leverage and liquidity ratios are not comparable to pure manufacturers.",
    "Ford": "Statements are consolidated and include a captive finance arm (Ford Credit); leverage and liquidity ratios are not comparable to pure manufacturers.",
}

mapping = resolve(bs_path, is_path, llm_json, cache_path=f"mapping_{client.lower()}.json")

extra_notes = []
if client in CONTEXT_NOTES:
    extra_notes.append(CONTEXT_NOTES[client])
extra_notes += [f"LLM mapped '{d['item']}' to {d['labels']} ({d['reason']})"
                for d in mapping["llm_decisions"]]
extra_notes += [f"DATA CHECK FAILED: {e}" for e in mapping["validation_errors"]]

result = analyze(bs_path, is_path,
                 out_path=f"{client.lower()}_analysis.json",
                 client=client,
                 aliases=mapping["aliases"],
                 extra_notes=extra_notes,
                 unreliable=mapping.get("unreliable_metrics", {}))