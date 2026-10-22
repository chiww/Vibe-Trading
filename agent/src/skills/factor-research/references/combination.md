# Cross-sectional factor combination

The optional `combination` quantlib module provides one pure-compute function:

| Function | Purpose |
| --- | --- |
| `combine_factors` | Combine two or more factor panels into one composite score, with equal, IC/ICIR walk-forward, or caller-supplied weights |

It is available through `quantlib_call`, including transports without shell
tools. Nothing in the backtest path calls it: using it, skipping it, or replacing
it with your own weighting scheme is a research choice. Existing alpha formulas
and backtests are unchanged.

## Where this sits in the factor workflow

    compute factors  ->  validate each factor  ->  [clean]  ->  [combine]  ->  signal / backtest
    Alpha Zoo or          IC / IR, quantile       neutrali-     this          SignalEngine,
    your own panel        backtest                zation        module        `backtest` tool

- **Before** combining, every input factor should pass its own validity test
  (`factor_analysis`): the composite of two unusable factors is still unusable,
  and blending hides which input failed.
- **Neutralization** (a separate module) removes industry, size or style
  exposures from a *single* factor. Combination does not do that job; it merges
  factors that are already acceptable. Either step may be used without the other.
- **After** combining you hold one score panel per `(time, asset)`. Turn it into
  positions with a `SignalEngine` (`multi-factor` skill) and run the backtest
  engines; this module never sizes positions.

If you are new to factor research, the practical summary is: a single factor is
a hypothesis about which stocks will outperform, and a composite is a weighted
average of several such hypotheses. The weights decide how much each hypothesis
is believed; everything else in this document is about making that decision
honest and reproducible.

### When to skip it

- You have one factor: nothing to combine.
- You already own a defensible weighting scheme: use `method="explicit"` with
  your table, or do not call this module at all.
- You are still validating individual factors: finish `factor_analysis` first.

## Many methods, one weight contract

There is no complete list of combination methods. Equal weight, mean IC,
IC-IR-weighted, decayed IC, maximum IC, cross-sectional-regression weights,
trained models, risk-based weights and factor timing all exist, and practitioners
disagree about which is best. This module therefore does not enumerate methods;
it fixes the *weight contract*:

| You want | How to use this module |
| --- | --- |
| Equal weight | `method="equal"` |
| Mean IC weights | `method="ic"` with a forward-return panel |
| IC-IR weights | `method="icir"` with a forward-return panel |
| Decayed IC, maximum IC, regression, ML, risk parity, anything else | Compute a `time x factor` weight table yourself, then `method="explicit", weights=table` |

A table rather than a callback is deliberate: a table is auditable, reproducible,
serialisable over MCP and storable as a research artifact. A callback is none of
those, and this project has to answer "which workflow produced that number?".

The built-in methods are baselines, not recommendations. They exist because they
need no tuning, no dependency and no training, so they make a fair first
comparison.

## Weight timing is the one rule that cannot be relaxed

A weight applied at row `t` may only use information from rows `< t`.

`ic` and `icir` enforce this internally: the weight at `t` is estimated from IC
observations at rows `< t` (optionally limited to the trailing `window`). A
caller-supplied table must already respect it; the module cannot see how the
table was produced. Weighting every row by the full-sample IC is the most common
way to turn a real factor into a fake backtest: the weights already know which
factors worked over the evaluation period.

## Data contract

Each DataFrame has observation times as rows and assets as columns; one row is
one independent cross-section, whether observations are daily, weekly, monthly or
irregular. No resampling, forward filling or full-period fitting occurs. The
first panel defines output axes; other panels align by exact time/asset labels,
and labels missing from the first panel are appended in first-seen order.

- `panels` maps factor name -> panel. A name must be a non-empty string.
- `returns` is a forward-return panel aligned the same way: the value at row `t`
  is the return earned *after* observing `t`. It is required for `ic`/`icir`,
  optional elsewhere (used only for the `composite_ic` diagnostic).
- Non-finite values are treated as missing, never as zero.
- A factor missing at a given row is excluded from that row and the remaining
  weights are renormalised; a row with no available factor stays `NaN`.
- Factors are z-scored per row by default. Z-scoring is unit-invariant, and
  inputs are bounded before centering so large or tiny units cannot overflow
  into a false zero variance.

## Built-in baselines

| `method` | Weight | Needs `returns` | Fails when |
| --- | --- | --- | --- |
| `equal` | `1 / n` | no | inputs are highly correlated: the blend double-counts one style |
| `ic` | trailing mean rank IC, sign preserved, normalised by absolute sum | yes | IC history is short or unstable; a reversed factor gets a negative weight |
| `icir` | trailing mean/std rank IC, same normalisation | yes | the IC series has near-zero dispersion, which inflates the ratio |

Signs are preserved: a factor whose IC is negative receives a negative weight
instead of being silently flipped. Report the sign, do not hide it.

## Bring your own weights

```python
import pandas as pd

# Any method: here, a 60-observation half-life on the IC series.
weights = pd.DataFrame({"momentum": w_mom, "value": w_val}, index=dates)
result = combine_factors(
    {"momentum": momentum, "value": value},
    forward_returns,
    method="explicit",
    weights=weights,        # time x factor; rows may cover rebalance dates only
)
```

Rows supplied only at rebalance dates are held forward. A weight table must
cover every factor exactly, and rows whose weights are all missing stay `NaN`.
Values are normalised by their absolute sum per row.

## Output and diagnostics

`combine_factors` returns a `CombinationResult`:

- `composite`: the score panel (`time x asset`).
- `weights`: the weight actually applied per row and factor.
- `diagnostics`:
  - `factor_ic`: per-factor IC mean, std, IR, positive ratio and observation
    count. **In-sample** description of the inputs.
  - `composite_ic`: the same statistics for the composite. **In-sample**.
  - `correlation`: average per-date Spearman correlation between factors. Two
    inputs correlated above ~0.9 are effectively one factor.
  - `weight_turnover`: mean absolute weight change between rebalances.
  - `weights_source`, `rows_without_weights`, `n_observations`, `n_assets`.

`factor_ic` and `composite_ic` describe the sample they were computed on; they
are not the walk-forward weights used to build the composite, and they say
nothing about out-of-sample behaviour.

## Pre-flight checklist

1. Every input factor passed `factor_analysis` on the same universe and horizon.
2. `correlation` was inspected; highly correlated inputs were dropped,
   orthogonalised or given a reduced weight on purpose.
3. Weights respect walk-forward timing (or `method="ic"/"icir"` is used).
4. The composite has its own `composite_ic`, not only the inputs' IC.
5. `weight_turnover` is affordable at the chosen `rebalance` frequency.
6. Direction signs were reviewed, not silently flipped.
7. The universe mask and holding horizon match the backtest that follows.
8. Method and hyper-parameters were chosen on research data, not the test period.

## Common failure modes

| Symptom | Cause | Fix |
| --- | --- | --- |
| Composite IC below every input | full-sample IC weights, or one input misaligned | use `ic` with a `window`, or an explicit table built walk-forward |
| Composite behaves like one factor | correlated inputs, equal weights | inspect `correlation`, orthogonalise (`neutralization`) or reduce the weight |
| Results not reproducible | weights computed ad hoc | pass a weight table; keep it as an artifact |
| Turnover explodes | `rebalance` far shorter than the factor's horizon | lengthen `rebalance`, or smooth the weights |
| Composite is blank early | `min_periods` not yet reached (`insufficient="skip"`) | that is the honest state; shorten `min_periods` only with enough data |

## Limits

- Does not size positions, apply costs or build a risk model.
- Does not neutralise industry, size or style exposures; use `neutralization`.
- Does not choose, tune or train a weighting method.
- Does not estimate multi-horizon returns; supply one forward-return panel.
- Does not validate point-in-time availability of the inputs.
- Does not guarantee out-of-sample performance; IC weights are sample estimates.

## Entry points

- **Python**: `from src.quantlib.combination import combine_factors`.
- **Agent / Web / REST / MCP**: `quantlib_call` with `module="combination"`,
  `function="combine_factors"`.
- **CLI prompt file**: request the same call in `request.txt` and run
  `vibe-trading run --prompt-file request.txt`.

JSON envelope example:

```json
{
  "action": "call",
  "module": "combination",
  "function": "combine_factors",
  "kwargs": {
    "panels": {"momentum": {"__dataframe__": {"index": ["2024-01-02"], "columns": ["a", "b"], "data": [[0.1, -0.2]]}}},
    "method": "equal"
  }
}
```

## Local validation

```bash
python -m pytest agent/tests/quantlib/test_combination.py agent/tests/test_mcp_combination.py -q
```

The suite pins unit-scale invariance, the no-look-ahead boundary, the truthful
`NaN` policy, the weight-table contract and the MCP round trip.
