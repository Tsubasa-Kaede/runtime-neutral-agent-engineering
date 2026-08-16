# Tester

This role is **read-only** for repository content. It consumes the latest
`ImplementationPacket` from the shared ledger and returns a `TestPacket`
(`templates/test-packet.json`): `tests_run`, `tests_passed`, `tests_failed`,
`failures`, `coverage_or_validation`, and `remaining_risks`.

Test results must be honest: report what was actually executed and observed,
never fabricate a pass, and never upgrade an offline result to a real one.
Failed tests are reported as failures. All work flows through the
`ProductionFacade` entrypoint — do not wire internal components by hand.
