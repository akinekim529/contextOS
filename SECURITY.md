# Security Policy

ContextOS is multi-tenant middleware; **zero cross-tenant leakage** is its core guarantee. We
take isolation and fail-closed behavior seriously and appreciate responsible disclosure.

## Reporting a vulnerability

**Do not open a public issue for security problems** — including cross-tenant leakage, RBAC/RLS
bypass, prompt-injection, secret exposure, or any data-isolation flaw.

Report privately via **[GitHub Security Advisories](https://github.com/akinekim529/contextOS/security/advisories/new)**
(repo → Security → *Report a vulnerability*). Include a minimal reproduction and the affected
version or commit.

We aim to acknowledge within 72 hours, agree on a disclosure timeline, and credit reporters who
wish to be credited.

## Scope

In scope: anything that breaks tenant isolation, the RBAC firewall, secret handling, or the
documented fail-closed guarantees. Out of scope: issues that require a deployment configured
contrary to the documented secure defaults.

## Supported versions

Pre-1.0: only the latest `0.x` release line receives security fixes.
