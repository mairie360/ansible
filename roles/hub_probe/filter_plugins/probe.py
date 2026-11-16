"""Filters of role hub_probe (MAIR-621), unit-tested in tests/test_hub_probe.py."""

import re

HOST = re.compile(r"^[a-z0-9]([a-z0-9-]*[a-z0-9])?$")
DOMAIN = re.compile(r"^([a-z0-9]([a-z0-9-]*[a-z0-9])?\.)+[a-z]{2,}$")


def hub_probe_urls(domains, hosts):
    """https://<host>.<domain>/ for every instance domain and probed host, sorted, no duplicate."""
    urls = set()
    for domain in domains:
        if not isinstance(domain, str) or not DOMAIN.match(domain):
            raise ValueError(f"not an instance domain: {domain!r}")
        for host in hosts:
            if not HOST.match(host):
                raise ValueError(f"not a host label: {host!r}")
            urls.add(f"https://{host}.{domain}/")
    return sorted(urls)


class FilterModule:
    def filters(self):
        return {"hub_probe_urls": hub_probe_urls}
