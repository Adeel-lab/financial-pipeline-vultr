# 5 Whys: Ford (Hungary)

**Problem:** Debt ratio is 87.6% as of 2025-12-31; weakness against a generic threshold (70.0% weak / 50.0% strong); trend stable

- Item: `fin:debt_ratio` (weakness, review status: proposed)
- Outcome: **root_cause_reached**
- Model: openai/gpt-oss-120b | prompt rca-v1.0
- Generated: 2026-10-30T19:55:28.529683+00:00

| # | Question | Answer | Evidence | Basis | Check |
|---|---|---|---|---|---|
| 1 | Why is this true: Debt ratio is 87.6% as of 2025-12-31; weakness against a generic threshold (70.0% weak / 50.0% strong); trend stable? | The debt ratio is high because total debt represents 87.6% of total assets, which exceeds the 70% weakness threshold and indicates a highly leveraged capital structure. | E0 | reported | supported |
| 2 | Why is this true: The debt ratio is high because total debt represents 87.6% of total assets, which exceeds the 70% weakness threshold and indicates a highly leveraged capital structure.? | Because the debt ratio of 87.6% exceeds the 70% weakness threshold, indicating a highly leveraged capital structure. | E0 | reported | supported |
| 3 | Why is this true: Because the debt ratio of 87.6% exceeds the 70% weakness threshold, indicating a highly leveraged capital structure.? | The debt ratio exceeds the 70% weakness threshold, so it is classified as a high‑leverage weakness. | E0 | reported | supported |
| 4 | Why is this true: The debt ratio exceeds the 70% weakness threshold, so it is classified as a high‑leverage weakness.? | The analysis applies a generic threshold that classifies any debt ratio above 70% as a weakness, and the observed 87.6% exceeds that threshold, leading to a high‑leverage weakness classification. | E0 | reported | supported |

## Root cause

The company’s capital structure relies heavily on debt, driving the debt ratio to 87.6%—well above the 70% weakness threshold—while the debt‑to‑equity metric remains low (0.61x) because the denominator (equity) is small relative to total assets, keeping the ratio under the 2.00x weak threshold. This mismatch reflects management’s choice to finance assets with debt rather than equity, resulting in a high leverage profile despite a technically acceptable debt‑to‑equity figure.

**Type:** management_controllable

**Validation test:** If the firm reduced its total debt or increased equity so that the debt ratio fell below the 70% weakness threshold, the leverage weakness would be eliminated. However, because the debt‑to‑equity metric is already strong, fixing the debt ratio alone may not fully resolve underlying liquidity or solvency risks, especially if equity remains low.

## Corrective actions (recommendations)

- Implement a debt reduction plan through asset disposals, refinancing at lower rates, or targeted debt repayments - Lowering total debt will directly reduce the debt ratio, bringing it under the 70% weakness threshold and improving the leverage profile [E0]
- Raise additional equity via share issuance or retained earnings - Increasing equity will both lower the debt ratio (by raising the denominator in the debt‑to‑assets calculation) and improve the equity ratio, addressing the weak equity position [E1]
- Improve operating profitability to generate cash flow for debt paydown - Higher operating margins and net income will provide internal funds for debt reduction and strengthen the equity base over time [E2, E3]

## Evidence gaps

- Lack of detailed breakdown of debt components (short‑term vs long‑term) and covenant terms
- Missing cash‑flow statement data to assess debt‑service capacity
- No industry‑specific benchmark thresholds for debt ratio and debt‑to‑equity

## Caveats

- The thresholds used are generic and may not reflect automotive industry norms
- The reported debt‑to‑equity figure (0.61x) appears inconsistent with the high debt ratio and low equity ratio, suggesting possible definition differences or data errors
- Future market conditions (interest rates, demand) could affect the feasibility of debt reduction strategies

## Evidence used

- [E0] (PROBLEM EVIDENCE) FINANCIAL debt_ratio (consolidated annual statements, file C:\Users\adeel\Desktop\notebooks\vultr\jobs\ba333d2466\finance\financial_ford_20261030T194927\approved_financials.json): Debt ratio is 87.6% as of 2025-12-31; weakness against a generic threshold (70.0% weak / 50.0% strong); trend stable. History: 2025-12-31 87.6%; 2024-12-31 84.3%; 2023-12-31 84.3%; 2022-12-31 83.1%; 2021-12-31 81.1%; 2020-12-31 88.5%

## Warnings

- Analysis rests only on items in the SWOT draft; it is not a complete causal study.
- Inference steps are the model's reasoning over cited facts, not source claims.
- Corrective actions are recommendations, not evidence.
