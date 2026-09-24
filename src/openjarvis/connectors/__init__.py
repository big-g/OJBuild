"""Data source connectors for Deep Research."""

import importlib
import sys

from openjarvis.connectors._stubs import (
    Attachment,
    BaseConnector,
    Document,
    SyncStatus,
)
from openjarvis.connectors.store import KnowledgeStore

__all__ = [
    "Attachment",
    "BaseConnector",
    "Document",
    "KnowledgeStore",
    "SyncStatus",
    "ensure_connectors_populated",
]

# Auto-register built-in connectors
import openjarvis.connectors.obsidian  # noqa: F401

try:
    import openjarvis.connectors.gmail  # noqa: F401
except ImportError:
    pass

try:
    import openjarvis.connectors.gmail_imap  # noqa: F401
except ImportError:
    pass

try:
    import openjarvis.connectors.gdrive  # noqa: F401
except ImportError:
    pass  # httpx may not be installed

try:
    import openjarvis.connectors.notion  # noqa: F401
except ImportError:
    pass

try:
    import openjarvis.connectors.granola  # noqa: F401
except ImportError:
    pass

try:
    import openjarvis.connectors.gcontacts  # noqa: F401
except ImportError:
    pass

try:
    import openjarvis.connectors.imessage  # noqa: F401
except ImportError:
    pass

try:
    import openjarvis.connectors.apple_notes  # noqa: F401
except ImportError:
    pass

try:
    import openjarvis.connectors.apple_music  # noqa: F401
except ImportError:
    pass

try:
    import openjarvis.connectors.apple_contacts  # noqa: F401
except ImportError:
    pass

try:
    import openjarvis.connectors.apple_calendar  # noqa: F401
except ImportError:
    pass

try:
    import openjarvis.connectors.slack_connector  # noqa: F401
except ImportError:
    pass

try:
    import openjarvis.connectors.outlook  # noqa: F401
except ImportError:
    pass

try:
    import openjarvis.connectors.imap  # noqa: F401
except ImportError:
    pass

try:
    import openjarvis.connectors.gcalendar  # noqa: F401
except ImportError:
    pass

try:
    import openjarvis.connectors.dropbox  # noqa: F401
except ImportError:
    pass  # httpx may not be installed

try:
    import openjarvis.connectors.whatsapp  # noqa: F401
except ImportError:
    pass

try:
    import openjarvis.connectors.oura  # noqa: F401
except ImportError:
    pass

try:
    import openjarvis.connectors.apple_health  # noqa: F401
except ImportError:
    pass

try:
    import openjarvis.connectors.strava  # noqa: F401
except ImportError:
    pass

try:
    import openjarvis.connectors.spotify  # noqa: F401
except ImportError:
    pass

try:
    import openjarvis.connectors.google_tasks  # noqa: F401
except ImportError:
    pass

try:
    import openjarvis.connectors.weather  # noqa: F401
except ImportError:
    pass

try:
    import openjarvis.connectors.github_notifications  # noqa: F401
except ImportError:
    pass

try:
    import openjarvis.connectors.hackernews  # noqa: F401
except ImportError:
    pass

try:
    import openjarvis.connectors.news_rss  # noqa: F401
except ImportError:
    pass



def ensure_connectors_populated() -> None:
    """Restore built-in connector registrations after registry clearing."""
    from openjarvis.core.registry import ConnectorRegistry

    if ConnectorRegistry.keys():
        return

    for module_name in list(sys.modules):
        if (
            module_name.startswith("openjarvis.connectors.")
            and module_name not in {
                "openjarvis.connectors._stubs",
                "openjarvis.connectors.store",
            }
        ):
            module = sys.modules.get(module_name)
            if module is not None:
                try:
                    importlib.reload(module)
                except Exception:
                    pass
