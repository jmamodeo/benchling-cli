# Security Policy

## Reporting a vulnerability

Please report vulnerabilities privately using GitHub's
[private vulnerability reporting](https://github.com/jmamodeo/benchling-toolkit/security/advisories/new)
(Security Advisories). Do not open public issues for security problems. Expect an initial response within a few days.

## Handling credentials

- Never commit tokens, client IDs/secrets, or real tenant data to this repository, issues, or pull requests.
- Credentials are supplied only through environment variables and held in memory; they are never written to config files or a keychain.
- If a secret is exposed, rotate it immediately.

## Supported versions

Only the latest released version receives security fixes.
