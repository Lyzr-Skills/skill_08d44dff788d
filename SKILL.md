---
name: fpa-variance-analysis
description: Analyse Plan vs Actual (budget vs actuals) opex data returned by the get_plan_vs_actual MCP tool and answer FP&A questions with deterministic numbers, top variance drivers, outliers and a histogram of variance %. Use this skill whenever the user asks about budget variance, over/under spend, plan vs actual, where money is going, why a region, department, cost center or account is over or under budget, or wants a variance distribution or histogram, even if they don't say "variance".
---

# FP&A variance analysis

You answer FP&A questions about Plan vs Actual opex. The numbers come from a script, never from your own arithmetic: in a finance demo, one wrong total destroys trust in everything else you say.

## Workflow

1. **Get the data.** Call `get_plan_vs_actual` with the narrowest filters that still answer the question (for "why is EMEA over budget?", pass `region="EMEA"`). Smaller inputs are faster and cheaper. If you are unsure of valid filter values, call `list_dimensions` first. If the tool returns an `error`, fix the filter and retry once; otherwise tell the user what went wrong.

2. **Run the script on the tool response, unchanged.** Pass the full JSON object returned by the tool (`meta` and `rows`) to `scripts/analyze_variance.py`. Do not edit, round, summarise or drop rows before passing them: the script validates the rows itself.

   ```
   python scripts/analyze_variance.py input.json
   ```
   or pipe the JSON on stdin, or call `analyze(data)` from the module.

   Options (defaults are fine for most questions):
   - `--top-n 5` number of drivers returned per direction
   - `--outlier-pct 15` rows with |variance %| at or above this are outliers
   - `--bins -20,-10,-5,0,5,10,20,50` histogram edges (the ends are open-ended)

   If the script prints `{"error": ...}`, report the problem; do not fall back to computing numbers yourself.

3. **Write the answer** from the script output (see "Reading the output"). Every number you state must appear in the script output. Prefer quoting `headline_facts`, which are pre-formatted sentences.

4. **Return the final JSON** described in "Response format".

## Reading the output

- **Sign convention:** `variance = actual - plan`. For opex, positive means overspend, negative means underspend.
- **`totals`** is the overall picture for whatever was filtered. **`breakdowns`** has the same figures by region, department, account and period.
- **`drivers.overspend` / `drivers.underspend`** rank cost center × account lines, summed across periods. Each has a `pattern`:
  - `one_off`: one period carries at least 80% of the line's variance (a spike, e.g. an event or a true-up). Name the `peak_period`.
  - `run_rate`: every period is off plan in the same direction by 8% or more. This is the one to worry about, because it will continue unless something changes.
  - `mixed`: material but irregular.
  - `within_tolerance`: under 5% for the line overall. Don't present these as findings.
- **`concentration`** tells you whether the variance is concentrated or broad-based. If the top lines carry most of the gross overspend, say the problem is a few specific items, not the whole region.
- **`histogram`** is the distribution of row-level variance % (one observation per cost center × account × month). A tall centre around 0% with a few bars far out means "mostly on plan, a few outliers". A whole distribution shifted to one side means the plan itself was off. `histogram.ascii` is a ready-made text chart you can show when the client cannot render charts.
- **`outliers`** are individual rows beyond the threshold, largest first.
- **Underspend isn't automatically good news.** Underspend on salaries usually means unfilled roles (hiring delays), which can put delivery at risk. Say so when it applies.
- **Check `context`.** If `rows_rejected > 0` or `input_truncated` is true, mention it, because the figures may be incomplete.

## What you must not do

- Don't do arithmetic yourself, including adding up drivers or converting to percentages. If a figure you want isn't in the output, leave it out.
- Don't invent causes. The data shows *where* and *when*, not *why*. Say "a one-off spike in August on Events" rather than "an over-budget conference"; you can suggest the budget owner for that cost center as the person to ask.

## Response format

Return a single JSON object, with values copied verbatim from the script output:

```json
{
  "answer": "2-4 sentences answering the user's question in plain language.",
  "key_points": ["3-5 short bullets, usually based on headline_facts"],
  "totals": { "...": "copy of totals" },
  "top_drivers": [
    {
      "cost_center": "...", "cost_center_name": "...", "account": "...",
      "variance": 0, "variance_pct": 0, "pattern": "...", "peak_period": "..."
    }
  ],
  "histogram": {
    "metric": "variance_pct",
    "bins": [ { "label": "...", "lower": null, "upper": null, "count": 0 } ]
  },
  "outliers": [ { "period": "...", "cost_center": "...", "account": "...", "variance": 0, "variance_pct": 0 } ],
  "caveats": ["only if rows were rejected, input was truncated, or the question could not be fully answered"]
}
```

- `top_drivers`: the first 3 entries of `drivers.overspend` (or `drivers.underspend` if the question is about underspend), keeping only the fields shown, and skipping `within_tolerance` lines.
- `histogram.bins`: all bins, keeping `label`, `lower`, `upper` and `count`.
- `outliers`: up to 5 entries from `outliers.rows`.
- Omit `caveats` if there are none.

## Example

User: "Why is EMEA over budget this quarter?"

1. `get_plan_vs_actual(region="EMEA")`
2. Run the script on the response.
3. Answer, e.g.: "EMEA is 293K USD (4.8%) over plan for FY2026 Q3, and the overspend is concentrated rather than broad-based: the top 5 lines account for 94% of it. The two largest are one-off spikes (Field Marketing Events in August, IT Software Licenses in September); the one to watch is Sales Operations Travel & Entertainment, which runs 45% over plan every month."
