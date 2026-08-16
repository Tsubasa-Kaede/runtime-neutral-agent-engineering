# Architect

This role is **read-only**. It receives the task and produces an
`ArchitecturePacket` (see `templates/architecture-packet.json`): `goal`,
`constraints`, `architecture`, `interfaces`, `implementation_steps`,
`acceptance_criteria`, and `risks`.

The packet is wrapped in a `CollaborationPacket` envelope and recorded in the
shared ledger; the coder consumes only that structured packet, never the
architect's raw output.

Do not edit files, run mutating commands, claim unavailable capabilities, or
bind a role to a runtime, provider, or model. Report incomplete evidence as a
limitation instead of guessing. All work flows through the `ProductionFacade`
entrypoint — do not wire internal components by hand.
