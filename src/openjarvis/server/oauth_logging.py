"""Redact OAuth capability/code queries from the standard server access logger."""

import logging
import re
from urllib.parse import unquote


class OAuthAccessFilter(logging.Filter):
    def filter(self, record):
        # Uvicorn's access record: (client, method, full_path, protocol, status).
        if isinstance(record.args, tuple) and len(record.args) == 5:
            args = list(record.args)
            target = args[2]
            if isinstance(target, str):
                path = target.split("?", 1)[0]
                if re.fullmatch(
                    r"/v1/(connectors|sources)/[A-Za-z0-9_-]+/oauth/(launch|callback)",
                    unquote(path),
                ):
                    args[2] = path + ("?[redacted]" if "?" in target else "")
                    record.args = tuple(args)
        return True


def install_oauth_access_filter():
    logger = logging.getLogger("uvicorn.access")
    if not any(isinstance(item, OAuthAccessFilter) for item in logger.filters):
        logger.addFilter(OAuthAccessFilter())
