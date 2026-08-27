# Security Policy

## Reporting a Vulnerability

Please do not open a public issue containing sensitive details, exploit
descriptions, secrets, or credentials.

To report a security problem, use GitHub's private security reporting on
this repository (**Security** tab → **Report a vulnerability**). Include the
commit or release you tested, a minimal reproduction, and the component
involved (adapter, validation, collaboration, CLI). Do not include real
credentials in the report.

Secret scanning and push protection are enabled on this repository.

## Scope

This project is an orchestration layer above coding-agent CLIs. By design
the engine never reads, stores, prints, or modifies credentials; never logs
in to or out of any runtime; and never touches runtime configuration.
Authentication belongs to the runtimes themselves. The enforced boundaries —
the no-secrets contract, content scanning, protected paths, and the
subprocess environment whitelist — are described in the Security section of
the README.

## Supported Versions

| Version | Supported |
|---|---|
| 2.0.0 (current release) | ✅ |
