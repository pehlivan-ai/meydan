"""Email addresses we own: whether their domain is set up to receive and to be trusted.

Not a channel yet (no sending or reading). `dns_health` checks what makes an address work and
lets mail from it be trusted (RFC 7208 SPF, RFC 6376 DKIM, RFC 7489 DMARC), as an
EndpointUpdate for `meydan.runtime.endpoints`:

    mx       someone receives mail for the domain
    spf      exactly one v=spf1 record, ending in -all or ~all
    dkim     each given selector publishes a key (selectors are the provider's, e.g. its docs)
    dmarc    a v=DMARC1 policy: p=none only monitors (warn), quarantine/reject enforce (ok)
    reports  DMARC aggregate reports (rua) go somewhere, so a policy can be raised safely

DNS lookups use dnspython (`meydan[email]`); pass `lookup` to use your own resolver or in tests.
"""

from collections.abc import Callable, Sequence
from datetime import UTC, datetime

from meydan.channels.base import Address, EndpointUpdate

CHANNEL = "email"
SCHEME = "mailto"

# lookup(name, "MX" | "TXT") -> record texts; [] when there are none.
Lookup = Callable[[str, str], list[str]]


def dns_lookup(nameserver: str | None = None) -> Lookup:
    """A lookup through dnspython; `nameserver` asks that server instead of the system's (useful
    behind a VPN that answers DNS from its own cache)."""
    import dns.exception
    import dns.resolver

    resolver = dns.resolver.Resolver(configure=nameserver is None)
    if nameserver:
        resolver.nameservers = [nameserver]

    def lookup(name: str, rdtype: str) -> list[str]:
        try:
            answer = resolver.resolve(name, rdtype)
        except (dns.resolver.NXDOMAIN, dns.resolver.NoAnswer):
            return []
        except dns.exception.DNSException as e:
            raise LookupError(f"{rdtype} {name}: {type(e).__name__}") from e
        if rdtype == "TXT":  # a TXT record may come in several strings: one text
            return [b"".join(r.strings).decode() for r in answer]
        return [r.to_text() for r in answer]

    return lookup


def _check(name: str, level: str, detail: str) -> dict[str, str]:
    return {"name": name, "level": level, "detail": detail}


def _tags(record: str) -> dict[str, str]:
    out = {}
    for part in record.split(";"):
        key, _, value = part.strip().partition("=")
        if key:
            out[key.strip().lower()] = value.strip()
    return out


def checks(domain: str, lookup: Lookup, selectors: Sequence[str] = ()) -> list[dict[str, str]]:
    out = []
    mx = lookup(domain, "MX")
    out.append(_check("mx", "ok", ", ".join(sorted(mx))) if mx else _check("mx", "fail", "no MX"))

    spf = [r for r in lookup(domain, "TXT") if r.lower().startswith("v=spf1")]
    if not spf:
        out.append(_check("spf", "fail", "no v=spf1 record"))
    elif len(spf) > 1:
        out.append(_check("spf", "fail", f"{len(spf)} v=spf1 records: receivers reject both"))
    else:
        last = spf[0].split()[-1].lower()
        good = last in ("-all", "~all")
        out.append(_check("spf", "ok" if good else "warn", spf[0]))

    if not selectors:
        out.append(_check("dkim", "warn", "no selector given: not checked"))
    for selector in selectors:
        keys = [r for r in lookup(f"{selector}._domainkey.{domain}", "TXT") if "p=" in r]
        has_key = any(_tags(k).get("p") for k in keys)
        out.append(
            _check(f"dkim:{selector}", "ok" if has_key else "fail",
                   "key published" if has_key else "no key")
        )  # fmt: skip

    dmarc = [r for r in lookup(f"_dmarc.{domain}", "TXT") if r.lower().startswith("v=dmarc1")]
    if len(dmarc) != 1:
        out.append(_check("dmarc", "fail", "no DMARC record" if not dmarc else "several records"))
    else:
        tags = _tags(dmarc[0])
        policy = tags.get("p", "").lower()
        if policy in ("quarantine", "reject"):
            out.append(_check("dmarc", "ok", f"p={policy}"))
        elif policy == "none":
            out.append(_check("dmarc", "warn", "p=none: monitoring only, nothing is enforced"))
        else:
            out.append(_check("dmarc", "fail", f"invalid policy {policy!r}"))
        rua = tags.get("rua")
        out.append(
            _check("reports", "ok", rua) if rua else _check("reports", "warn", "no rua: no reports")
        )
    return out


def dns_health(
    address: str, *, selectors: Sequence[str] = (), lookup: Lookup | None = None
) -> EndpointUpdate:
    """Check the domain of `address` and report it as one of our endpoints."""
    address = address.strip().lower()
    local, _, domain = address.rpartition("@")
    if not local or not domain:
        raise ValueError("not an email address")
    found = checks(domain, lookup or dns_lookup(), selectors)
    return EndpointUpdate(
        channel=CHANNEL, address=Address(SCHEME, address), at=datetime.now(UTC),
        account=domain, display=address, details={"checks": found},
    )  # fmt: skip
