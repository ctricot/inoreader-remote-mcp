"""`python -m inoreader_remote_mcp`: serve on :8000."""

import logging

import uvicorn

from .config import get_settings


def main() -> None:
    # uvicorn configures its own loggers only; this makes ours (quota lines, token
    # refreshes) reach the container logs too.
    logging.basicConfig(
        level=get_settings().log_level.upper(),
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )
    uvicorn.run(
        "inoreader_remote_mcp.server:create_app",
        factory=True,
        host="0.0.0.0",
        port=8000,
        # Behind Nginx Proxy Manager: trust its forwarded headers. The container publishes
        # no host port, so only the `proxy` network can reach it.
        proxy_headers=True,
        forwarded_allow_ips="*",
    )


if __name__ == "__main__":
    main()
