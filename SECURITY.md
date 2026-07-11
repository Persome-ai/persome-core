# Security policy

The Runtime's data handling, network egress, local API boundary, export
caveats, and threat model are documented in
[SECURITY_PRIVACY.md](SECURITY_PRIVACY.md).

## Supported versions

Security fixes are made on the latest `0.2.x` release and `main`. Persome is
currently an alpha macOS Runtime, so users should upgrade to the newest patch
before reporting a defect. The live capture stack supports macOS 13 and newer;
Linux CI covers the offline Python pipeline only.

| Version | Security updates |
|---|---|
| latest `0.2.x` | yes |
| `main` | yes |
| older releases | no |

## Reporting a vulnerability

Use GitHub's private vulnerability reporting:
https://github.com/Persome-ai/persome-core/security/advisories/new.

Do not include vulnerabilities or personal capture data in a public issue.
Private vulnerability reporting is enabled for this repository.

Expected acknowledgement: within 72 hours.

There is no bug bounty at this time.
