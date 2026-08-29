# Baseline: single direct prompt

The fair comparison point. One prompt, one model call per case, no tools, no
verification, no orchestration. It receives exactly the same inputs the agent
receives and is scored by the same scorer over the same 16 cases.

Meaningful resource differences, stated plainly as the brief requires: the
baseline gets one call and cannot execute anything. The agent can parse the AST,
run `go build`, and run tests. That gap is the thing being measured - it is not
an unfair advantage, it is the hypothesis.

## Prompt

```
You are reviewing a Go HTTP API for contract drift.

Below are the API's Go source files and its published OpenAPI specification.
The specification is what external partners integrate against. Find every place
where the code and the specification disagree.

For each disagreement report:
  - path:   the API path as written in the spec, e.g. /payouts/{id}
  - method: the HTTP method, lowercase
  - kind:   one of route_missing_from_spec, route_missing_from_code,
            response_field_mismatch, response_type_mismatch,
            response_header_mismatch, request_param_mismatch,
            request_required_mismatch, status_code_mismatch,
            undocumented_status, auth_mismatch, validation_mismatch,
            default_value_mismatch
  - detail: the specific field, parameter, header or status involved
  - severity: critical, high, medium or low
  - evidence: the file and line supporting the finding

Report only genuine disagreements between code and specification. Refactors that
leave the wire contract unchanged are not findings.

Respond with JSON only:
{"findings": [ ... ]}

=== GO SOURCE ===
{{SOURCE}}

=== OPENAPI SPECIFICATION ===
{{SPEC}}
```

## Notes

The `kind` vocabulary is given to the baseline deliberately. Withholding it
would make the baseline lose on formatting rather than on substance, and the
scorer matches on `kind`. A baseline that fails for a preventable reason proves
nothing about the agent.
