"""Apify Proxy connection strings (https://docs.apify.com/proxy).

Format: http://<username>:<password>@proxy.apify.com:8000
A distinct `session` in the username pins a distinct IP, so a fresh session
id = a proxy rotation — pass one when retrying after a block.

Needs APIFY_PROXY_PASSWORD in .env (used for RESIDENTIAL/default groups).
Some proxy groups (e.g. a dedicated datacenter group bought separately) have
their OWN password tied to that group — pass password_env to point at a
different .env key for those. Optional: APIFY_PROXY_GROUPS (default "auto"),
APIFY_PROXY_COUNTRY.

Usage:
    from src.proxy import ApifyProxyConfig

    proxy = ApifyProxyConfig(groups="RESIDENTIAL")
    dc_proxy = ApifyProxyConfig(groups="BUYPROXIES94952", password_env="APIFY_PROXY_PASSWORD_DATACENTER")
    driver.get(url)  # or pass proxy.url() straight to @browser(proxy=...)
"""
import os
import random
import string
from functools import lru_cache

from dotenv import load_dotenv

load_dotenv()

_UNSET = object()  # distinguishes "not passed, use env default" from "explicitly None"


class ApifyProxyConfig:
    HOST = "proxy.apify.com"
    PORT = 8000

    def __init__(self, groups=None, country=_UNSET, password_env="APIFY_PROXY_PASSWORD"):
        self.password = os.environ[password_env]
        self.groups = groups or os.environ.get("APIFY_PROXY_GROUPS", "auto")
        # Some proxy groups (e.g. a specific bought datacenter group) reject
        # auth entirely (407) if a country is attached — pass country=None
        # explicitly to opt out, distinct from "unset -> use env default".
        self.country = (os.environ.get("APIFY_PROXY_COUNTRY") or None) if country is _UNSET else country

    def url(self, session: str | None = None) -> str:
        parts = [f"groups-{self.groups}"] if self.groups and self.groups.lower() != "auto" else []
        if session:
            parts.append(f"session-{session}")
        if self.country:
            parts.append(f"country-{self.country}")
        username = ",".join(parts) if parts else "auto"
        return f"http://{username}:{self.password}@{self.HOST}:{self.PORT}"

    @staticmethod
    def new_session_id() -> str:
        """Random session id -> forces a fresh IP on the next call to .url()."""
        return "s" + "".join(random.choices(string.ascii_lowercase + string.digits, k=12))


@lru_cache(maxsize=None)
def _group_proxy(groups: str) -> "ApifyProxyConfig":
    # "auto" -> dedicated datacenter group, own password (bought separately,
    # not covered by APIFY_PROXY_PASSWORD).
    if groups == "auto":
        return ApifyProxyConfig(groups="BUYPROXIES94952", password_env="APIFY_PROXY_PASSWORD_DATACENTER", country=None)
    return ApifyProxyConfig(groups=groups)


# Crawlee's ProxyConfiguration calls new_url_function(session_id, request) —
# no third arg, so these take exactly what Crawlee passes.
async def dc_proxy_url(session_id=None, request=None):
    return _group_proxy("auto").url(session=session_id or ApifyProxyConfig.new_session_id())


async def res_proxy_url(session_id=None, request=None):
    return _group_proxy("RESIDENTIAL").url(session=session_id or ApifyProxyConfig.new_session_id())


def _selftest():
    os.environ["APIFY_PROXY_PASSWORD"] = "pw"
    os.environ.pop("APIFY_PROXY_COUNTRY", None)
    os.environ.pop("APIFY_PROXY_GROUPS", None)

    auto = ApifyProxyConfig(groups="auto")
    assert auto.url() == "http://auto:pw@proxy.apify.com:8000"
    assert auto.url(session="a1") == "http://session-a1:pw@proxy.apify.com:8000"

    res = ApifyProxyConfig(groups="RESIDENTIAL", country="MX")
    assert res.url(session="a1") == "http://groups-RESIDENTIAL,session-a1,country-MX:pw@proxy.apify.com:8000"

    os.environ["APIFY_PROXY_COUNTRY"] = "MX"
    no_country = ApifyProxyConfig(groups="BUYPROXIES94952", country=None)
    assert no_country.url() == "http://groups-BUYPROXIES94952:pw@proxy.apify.com:8000", \
        "country=None must override APIFY_PROXY_COUNTRY, not just fall back to it"
    os.environ.pop("APIFY_PROXY_COUNTRY", None)

    s1, s2 = ApifyProxyConfig.new_session_id(), ApifyProxyConfig.new_session_id()
    assert s1 != s2, "session ids must be unique per call"

    print("ok")


if __name__ == "__main__":
    _selftest()
