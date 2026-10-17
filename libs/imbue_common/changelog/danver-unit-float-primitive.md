Added the `UnitFloat` primitive: a float constrained to the closed unit interval `[0.0, 1.0]`, validated on construction and through a pydantic schema.

It mirrors the existing `Probability` but without claiming to be one, for fractions and ratios that are not chances -- a required completion fraction, a pass rate, a proportion of a budget.
