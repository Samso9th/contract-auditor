# Agent trajectories

One per evaluation case, generated from the scored run in `reports/runs/agent`. Each is followable from the agent's instructions to its final result, including the claims the verification gate deleted.

Worth reading first:

- **D02, D04, N02** are the cases where the gate deleted a claim: the agent proposed something false and execution removed it before it reached the report.
- **D06, D09, D11** are the drifts the deterministic rules cannot settle at all, so they exist to be found by the agent or not at all.

| case | drift | agent claims | gate dropped | kept | result |
|---|---|---|---|---|---|
| [D01](D01.md) | response-field-rename | 0 | 0 | 2 | correct |
| [D02](D02.md) | status-code | 1 | 1 | 1 | correct |
| [D03](D03.md) | undocumented-route | 0 | 0 | 1 | correct |
| [D04](D04.md) | phantom-route | 1 | 1 | 1 | correct |
| [D05](D05.md) | money-type-change | 0 | 0 | 2 | correct |
| [D06](D06.md) | undocumented-required-field | 1 | 0 | 1 | correct |
| [D07](D07.md) | query-param-rename | 0 | 0 | 1 | correct |
| [D08](D08.md) | auth-drift | 0 | 0 | 2 | correct |
| [D09](D09.md) | default-value-drift | 1 | 0 | 1 | correct |
| [D10](D10.md) | header-rename | 0 | 0 | 1 | correct |
| [D11](D11.md) | validation-drift | 1 | 0 | 1 | correct |
| [D12](D12.md) | undocumented-error | 0 | 0 | 1 | correct |
| [N01](N01.md) | decoy-local-rename | 0 | 0 | 0 | correct |
| [N02](N02.md) | decoy-comment | 1 | 1 | 0 | correct |
| [N03](N03.md) | decoy-extract-helper | 0 | 0 | 0 | correct |
| [N04](N04.md) | decoy-field-reorder | 0 | 0 | 0 | correct |

**Across all 16 cases:** the agent proposed 6 claims; the gate refuted 3 and confirmed 3. That is a raw agent precision of **0.50** before the gate and **1.00** after it. 15 findings reached the report, for $0.0704.
