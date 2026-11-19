# Security Policy

## Reporting a vulnerability

Please report vulnerabilities privately using GitHub's
[private vulnerability reporting](https://github.com/jmamodeo/benchling-toolkit/security/advisories/new)
(Security Advisories). Do not open public issues for security problems. Expect an initial response within a few days.

## Handling credentials

- Never commit tokens, client IDs, client secrets, or real tenant data to this repository, issues, or pull requests.
- Run `benchling auth login` in your own terminal. The client secret is not accepted as an argument and must not be passed through an agent, a chat, or an MCP client config.
- The tenant URL and client ID are written to `environments.json` (mode 0600) under the state directory. The client secret is stored in the OS keychain (service `benchling-toolkit`, username = environment name). Access tokens stay in process memory.
- There is no plaintext fallback. If the OS keychain is unavailable, login fails and nothing is saved.
- Error envelopes and execution journals are scrubbed of client secrets and access tokens.
- If a secret is exposed, rotate it in Benchling and run `benchling auth login` again for that environment.

## Supported versions

Only the latest released version receives security fixes.
