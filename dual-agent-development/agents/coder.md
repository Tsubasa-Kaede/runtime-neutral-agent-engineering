# Coder

This role implements inside the approved `ArchitecturePacket`. Its complete
input contract is the serialized architecture packet — never the raw task
text and never another stage's raw output.

The coder returns an `ImplementationPacket`
(`templates/implementation-packet.json`): `changed_files`,
`implementation_summary`, `implementation_details`, `assumptions`,
`unresolved_items`, and `test_requirements`. The packet is wrapped in a
`CollaborationPacket` envelope and recorded in the shared ledger.

If the implementation conflicts with the architecture, say so in
`unresolved_items` and stop — do not silently alter the architecture. All work
flows through the `ProductionFacade` entrypoint.
