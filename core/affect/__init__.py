"""Affect: the companion's explicit, decaying emotional state.

Split by responsibility so the interesting part stays testable without a
database or a model:

* ``core.affect.engine``  -- pure math: signals -> state, plus time decay
* ``core.affect.reader``  -- pure rendering: state -> prompt text
* ``core.affect.manager`` -- the only piece that talks to a repository
"""
