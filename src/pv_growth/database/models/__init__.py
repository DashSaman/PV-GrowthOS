"""Model registry: import every model module so Base.metadata is complete.

Alembic's env.py imports this package; new domain models must be added here.
"""

from pv_growth.database.models._flag import FeatureFlag  # noqa: F401
