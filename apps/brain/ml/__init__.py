"""Machine-learning subsystem for SEVPS.

Every model in here returns a :class:`~apps.brain.ml.base.Prediction`, which
carries three things together and refuses to be constructed with fewer:

    prediction   the value
    confidence   how much to trust it
    explanation  which inputs drove it, and by how much

That is not decoration. A dispatcher shown "Level 1" by a model has no way to
weigh it against their own judgement without knowing the model was 54%
confident and that the deciding factor was the caller's phrasing. An
unexplained number in an emergency system either gets blindly obeyed or
ignored entirely, and both failures are worse than no model at all.

Two rules hold throughout:

* **Models advise; rules decide.** Nothing here overrides the Rule-Based
  Emergency Engine or the Layer 6 priority ladder. A model that disagrees
  raises the disagreement for a human - it does not act on it.
* **A model that cannot beat its baseline is not used.** Every estimator
  reports its accuracy against the statistical fallback it would replace, and
  the training commands refuse to recommend a model that loses.
"""
