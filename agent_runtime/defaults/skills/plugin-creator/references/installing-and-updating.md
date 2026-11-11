# Installing and updating plugins

## Where plugins live

```text
.RADE/plugins/
`-- <plugin-name>/
    |-- plugin.json
    |-- skills/
    |-- agents/
    |-- mcp/
    `-- hooks/
```

Copy the package into `.RADE/plugins/<plugin-name>/`. RADE discovers direct child directories
containing `plugin.json`. Existing plugin directories are not overwritten by bundled plugins.

## When changes take effect

Run `/refresh` while idle after adding, editing, disabling or removing a plugin. Plugin files
are not watched, and opening another Session does not guarantee a reload.

Each execution plane owns its MCP connections and stdio subprocesses. Reassembling the plane
closes its connections and starts fresh servers, so `/refresh` also reloads server code.

Hook declarations require `/refresh`; scripts run afresh each invocation. Rule-file loading
is the Plugin's responsibility.

## Discovery failures

If the plugin is missing, check discovery warnings for:

- a `plugin.json` that is unreadable or not a JSON object
- an unknown manifest field, a missing `name`, a bad name, or a wrong field type
- a component path that is absolute or escapes the plugin root
- a second plugin with a name already discovered in the same directory

An invalid Agent, MCP or Hook file skips that component. Duplicate Agent, MCP server or Hook names
keep the first declaration. If a role's MCP tool is unavailable, check its full tool name, MCP enablement,
server status and `tools.enabled` allowlist. Other components can still load.

For Hook failures, check stdout JSON, stderr, executable availability, and timeout;
see [the Hook contract](hooks.md).

## Disabling and removing

- `"enabled": false` keeps the package discovered but routes no component, including its MCP
  servers, agent roles and hooks.
- To remove a plugin, delete `.RADE/plugins/<plugin-name>/` and run `/refresh` while idle.

## Verifying an installed plugin

Run the skill's validator against the installed directory before refreshing:

```bash
python3 scripts/validate_plugin.py .RADE/plugins/<plugin-name>
```

The validator checks package files and declarations, including disabled packages. Verify MCP
startup, tool availability and Hook decisions in RADE separately. Confirm that a
pre-tool denial prevents the side effect; post-tool feedback cannot undo an executed operation.
