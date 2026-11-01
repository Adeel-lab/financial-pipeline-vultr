import planning as P

path = r"C:\Users\adeel\Desktop\notebooks\vultr\assets\GM_balance_sheet_annual_2020_2026.csv"
bs = P.load_statement(path)
words = ("debt", "borrow", "lease", "notes", "property", "equipment")
for label in bs.index:
    if any(w in label.lower() for w in words):
        print(f"{label:90s} {bs.loc[label].round(0).tolist()}")