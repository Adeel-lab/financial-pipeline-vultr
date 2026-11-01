from pathlib import Path

ASSETS = Path(r"C:\Users\adeel\Desktop\notebooks\vultr\assets")
JOBS = {
    "GM_balance_sheet_annual_2020_2026": ("us-gaap_DebtCurrent,", "us-gaap_LongTermDebtAndCapitalLeaseObligations,","us-gaap_InterestExpenseNonoperating"),
    "GM_income_statement_annual_2020_2026": ("Interest Expense,","us-gaap_InterestExpenseNonoperating"),
}
for name, drop in JOBS.items():
    lines = (ASSETS / f"{name}.csv").read_text(encoding="utf-8-sig").splitlines()
    kept = [l for l in lines if not l.startswith(drop)]
    (ASSETS / f"{name}_cleaned.csv").write_text("\n".join(kept) + "\n", encoding="utf-8")
    print(name, "rows", len(lines), "->", len(kept), "| removed:", [l.split(",")[0] for l in lines if l.startswith(drop)])