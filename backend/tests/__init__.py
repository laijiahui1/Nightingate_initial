"""Nightingale backend micro-test suite.

Contains the five required micro-test files (docs/REQUIREMENTS_EN.md §M8):

- test_rbac_scope.py             roles cannot write/edit as each other; patient isolation
- test_revision_history.py       version increments, revert restores prior state,
                                 audit log is metadata-only
- test_highlight_provenance.py   every highlight's provenance pointer resolves to a span
- test_concurrent_edits.py       different sections don't clobber; same-section conflicts
                                 resolve deterministically
- test_self_learning_importance.py  pinning a highlight boosts similar suggestions

All shared fixtures (isolated fixture PostgreSQL, migrations, the restricted
``app_nightingale`` connection pool, role-scoped JWT test clients, role-scoped
DB connections) live in ``conftest.py`` in this directory.
"""
