"""AI Auto-Analytics Dashboard.

Existing analytics engine (pandas on the user's own frame)
  -> profiler      : what the columns *are* (measures, dimensions, dates, hierarchies, quality)
  -> decisions     : which visualisations the data genuinely supports, and why others are withheld
  -> insights      : findings and what-changed, as sentences built only from computed numbers
  -> composer      : the dashboard payload the UI renders (KPIs, hero, tabs, charts, filters, empty states)

Nothing here types a number: every figure is read from a pandas result, and every chart records how it was
calculated. A model is never asked to produce or adjust a value.
"""
