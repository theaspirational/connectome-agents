# Offline spawn example

The v2 template explicitly selects clean initiation and shows the default
`ConnectomeNewborn{timestamp}` naming convention. The console home keeps the
example independent of credentials and real providers. Use `validate`, `plan`,
`dry-run`, or read existing lifecycle state with `status`; none applies changes.

**Zulip is the first real identity provider planned for T4.** A future Zulip
instance selects `connectors: [zulip_mcp]` in its template and
`home: {transport: zulip_mcp, channel: primary}` in its instance declaration,
with the required locally available catalog artifacts. Selecting a connector
alone does not implement identity provisioning, channel/topic routing or seed
import. Do not substitute a shared bot identity for per-agent provisioning.

Any members of the home channel can join the initiation dialogue. The agent
writes its own name record. See [lifecycle and T4 contracts](../../docs/spawn-lifecycle.md).
