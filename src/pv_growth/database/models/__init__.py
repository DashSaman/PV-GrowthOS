"""Model registry: import every model module so Base.metadata is complete.

Alembic's env.py imports this package; new domain models must be added here.
"""

from pv_growth.database.models._flag import FeatureFlag  # noqa: F401
from pv_growth.database.models.campaigns import Campaign  # noqa: F401
from pv_growth.database.models.engagement import (  # noqa: F401
    CompetitorChange,
    CompetitorSource,
    ContentInsight,
    ContentItem,
    ContentPublication,
    FeedbackRating,
)
from pv_growth.database.models.events import EVENT_TYPES, Event  # noqa: F401
from pv_growth.database.models.free_config import (  # noqa: F401
    ConfigSource,
    ExclusiveClaim,
    PublishedPost,
    RawConfig,
)
from pv_growth.database.models.growth import (  # noqa: F401
    CommissionEntry,
    Milestone,
    Partner,
    Referral,
    ReferralCode,
    RewardLedger,
)
from pv_growth.database.models.intel import (  # noqa: F401
    AppConfig,
    Experiment,
    ExperimentAssignment,
)
from pv_growth.database.models.jobs import (  # noqa: F401
    Job,
    LifecycleRule,
    MessageLog,
    MessageTemplate,
)
from pv_growth.database.models.sources import SOURCE_KINDS, AttributionTouch, Source  # noqa: F401
from pv_growth.database.models.users import User  # noqa: F401
