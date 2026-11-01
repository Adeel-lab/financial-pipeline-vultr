import pandas as pd
import re

def load_statement(path):
    """Rows = line items, columns = dates (newest first). Auto-fixes orientation, strips tag prefixes."""
    df = pd.read_csv(path, index_col=0)
    try:
        pd.to_datetime(df.columns)
    except Exception:
        df = df.T
    df.columns = pd.to_datetime(df.columns)
    df = df.apply(pd.to_numeric)
    df = df[df.index.notna() & (df.index.astype(str).str.strip() != "")]
    df.index = [re.sub(r"^[A-Za-z]+(-[A-Za-z]+)?_", "", str(i)) for i in df.index]
    df = df[~df.index.duplicated(keep="first")]
    return df.sort_index(axis=1, ascending=False)
#load this statement assets/F_balance_sheet_annual_2020_2026.csv

df = load_statement("assets/F_balance_sheet_annual_2020_2026.csv")
df.to_csv("assets/F_balance_sheet_annual_2020_2026_cleaned.csv")