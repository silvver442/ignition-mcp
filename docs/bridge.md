# The mcp-bridge WebDev project

Ignition 8.3's REST API doesn't cover everything: live tag values, tag browsing, tag history,
alarm queries, and project file I/O (needed for Perspective view deployment) all fall outside
it. `ignition-mcp` reaches these through a small gateway-side WebDev project called `mcp-bridge`.

## What it is

A single Ignition project (`mcp-bridge`) containing:

- a project script library, `mcp_bridge_lib`, holding all the logic and security checks
- one WebDev python resource, `api`, whose `doPost` is a one-liner that delegates to the library

It is mounted at `POST /system/webdev/mcp-bridge/api/<endpoint>`. The library and its
`BRIDGE_VERSION` are in `bridge/src/mcp_bridge_lib.py`. Run `python bridge/build_zip.py`
to regenerate the bundled `bridge/mcp-bridge.zip` and `python bridge/build_zip.py --check`
to check it against the current sources. Historical upgrade inputs live in `bridge/baselines/`.

## Security

Every request carries a signed envelope produced by the MCP server:

```json
{ "ts": "<unix seconds>",
  "sig": "hmac_sha256(secret, ts + '.' + payload)",
  "payload": "<json string of the actual request>" }
```

The bridge rejects requests whose timestamp skews more than 300 seconds, verifies the HMAC with
a constant-time compare, and returns a generic `{"ok": false, "code": "auth"}` on any failure
with no further detail. File operations require a relative path whose normalized
absolute location stays within `data/projects/`.

The shared secret is `IGNITION_BRIDGE_SECRET` on the MCP side and a constant in the
`mcp_bridge_lib` script on the gateway side. They have to match. Never commit the real secret;
the archive ships with a `__BRIDGE_SECRET__` placeholder that gets substituted at install time.

## New installation of Bridge 0.1.13

Set `IGNITION_BRIDGE_SECRET` using the MCP client's secret mechanism and enable
`IGNITION_ALLOW_WRITES`. Run `bridge_install(confirm=true)` on a Gateway where
`mcp-bridge` is absent. The installer substitutes the secret in memory and sends
one project import with `overwrite=false`. It verifies the running version with a
signed ping and checks the SHA-256 of the imported library with a signed file read.

### Manual install

1. Verify the bundled archive with `python bridge/build_zip.py --check`.
2. Import `bridge/mcp-bridge.zip` as a new `mcp-bridge` project without overwrite.
3. In Designer, replace the placeholder in the `BRIDGE_SECRET` assignment in the
   `mcp_bridge_lib` project library and save. Set the same value using your MCP
   client's secret mechanism. Keep the repository copy with its placeholder.

## Check the running version

Run the read-only `bridge_status` tool and look for `version: "0.1.13"` and the
`files/read` capability. Run `ignition_diagnose` to investigate connectivity or
authentication failures.

## Upgrade an existing installation

Check the running version first. For the configured Ignition 8.3.9 test Gateway
running Bridge 0.1.12, use `python bridge/apply_upgrade_0113.py --apply` with the
existing authenticated launcher. The updater verifies its starting version,
creates and verifies a redacted backup, changes only the library, and confirms
0.1.13 and the signed raw SHA-256 readback. Earlier versions require the
version-specific upgrade path. The [0.1.1 upgrade](bridge-upgrade-011.md)
documents a historical step.

## Endpoints

All are `POST` and all require the signed envelope described above.

| Endpoint | Purpose |
|---|---|
| `ping` | version and capability list |
| `tags/browse`, `tags/read`, `tags/write`, `tags/configure` | tag operations |
| `tags/scoped-state`, `tags/scoped-configure`, `tags/scoped-restore` | verified configuration of individual test tags |
| `history/query`, `history/providers`, `history/raw` | tag history, including raw history for approved paths |
| `alarms/status`, `alarms/journal`, `alarms/ack` | alarms |
| `db/named-query`, `db/query` | database access |
| `files/list`, `files/read`, `files/write`, `files/delete` | project files under `data/projects/`, including UTF-8 reads and optional raw SHA-256 reads |
| `trial/reset` | reset the gateway trial timer, for development gateways running under a trial license |

## Scoped test tag configuration

The restricted MCP profile uses `IGNITION_TAG_CONFIG_ALLOWLIST` independently of
the Perspective project allowlist and the legacy tag-value write allowlist. On
the test launcher it contains `[default]Line_1/Cupper/Ignition`. Bridge 0.1.13
allows that Cupper folder and four exact BM1 paths. The server's configured
allowlist also applies. The Bridge checks that every ancestor is an existing
ordinary folder and that the target is an individual atomic tag.

- `tag_config_get` returns the tag configuration, effective properties, and a
  state fingerprint. Use it for `Status`, `Speed` and `Production` before making
  any decision about the simulator.
- `tag_configure_scoped` in `create` mode creates a Memory integer tag with
  history disabled and aborts on a collision. In `history` mode it merges only the
  supplied history fields and requires the state from `tag_config_get`.
- The server saves and verifies `backups/mcp-tag-config/<id>/before.json` before
  the mutation. A separate readback must match before the server creates its
  signed verification receipt and offers `tag_config_restore`. Restore checks
  the backup hash and the current configuration and backs up the current state
  before writing. Undoing a creation is limited to the tag in its verified
  receipt and requires the Memory value to still match.

The client must reconnect to load the updated MCP tool list. Before enabling the
new writes, verify that the installed WebDev project reports all three scoped
capabilities in `bridge_status`. The local bridge ZIP contains a secret
placeholder. Continue to use the existing secret mechanism for the installed
bridge.

## Troubleshooting

**`bridge_install` reports `status: "uncertain"`.** Check `project_get` for
`mcp-bridge` and run `bridge_status`. If the project is absent, retry the install.
If the project exists, confirm that WebDev is active and that the Gateway and MCP
use the same secret. Inspect the library and WebDev resource in Designer. A
project export can contain the configured secret. Keep any export in protected
storage outside the repository.

If the confirmed new project is incomplete, an operator can remove that project
after saving a protected copy, then retry the create-only install. For a project
that existed before the attempt, follow the version-specific upgrade or restore
procedure.

**403 on everything.** See the API-key permission fix in the README: the key's auto-created
security level needs to be added to both Gateway Read and Write Permissions.

## Compatibility

Targets and is tested on Ignition 8.3 with the WebDev module active. The bridge declares
`BRIDGE_VERSION`; the MCP server declares a minimum supported version and surfaces mismatches in
`ignition_diagnose`.
