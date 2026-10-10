# Security policy

## Reporting a vulnerability

Please report security issues privately through GitHub:
**Security → Report a vulnerability** on this repository. Do not open a public issue.

Include what you found, how to reproduce it, and the impact. Expect an initial reply within a few days.

## Scope

In scope: ways to bypass the risk gate or its limits, to change equity, rules or lockouts through an MCP tool,
to corrupt or forge state, or to get past the HTTP bearer-token guard.

Known and documented, so not vulnerabilities: the agent reports its own fills and closes, and the HTTP
transport has no TLS. See "Security and trust model" in the README.

## Supported versions

Only the latest release on `main`.
