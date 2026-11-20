# MCP

CodeKB exposes its analysis to IDEs and AI assistants over the
[Model Context Protocol](https://modelcontextprotocol.io), using the Streamable
HTTP transport. The endpoint is `/mcp/mcp`: the MCP app is mounted at `/mcp`, and
its endpoint inside that mount is also `/mcp`.

## Connect a client

Add CodeKB to your client's MCP configuration. For Kiro, that is
`.kiro/settings/mcp.json` in a workspace or `~/.kiro/settings/mcp.json` for all
workspaces:

```json
{
  "mcpServers": {
    "aws-codekb": {
      "url": "https://<your-codekb-host>/mcp/mcp"
    }
  }
}
```

For a [local evaluation](local.md), use `http://localhost:8000/mcp/mcp`. Other
clients use the same URL; see their documentation for where the configuration
lives. Restart or reload the client after saving.

## Sign-in

On a deployment with sign-in on, such as the [CDK deployment](deployment.md),
an unauthenticated request gets a 401
that points the client to CodeKB's OAuth metadata. A client that supports MCP
authorization then opens your browser, you sign in with the same account as the
Web UI, and the client stores the token. CodeKB only accepts sign-in callbacks
to `localhost`, which is what desktop MCP clients use.

A client that doesn't support MCP authorization can't connect to a deployment
with sign-in on.

What you can see over MCP follows [space authorization](security.md#space-authorization),
the same as in the Web UI.

## Tools

| Tool | What it does |
| --- | --- |
| `get_component_from_description` | Finds components that match a plain-language description. The best starting point |
| `find_component` | Finds a component by its exact name |
| `search_components` | Finds components by wildcard name pattern |
| `list_components` | Lists components, optionally by type |
| `show_component` | Shows a component's source code and metadata |
| `show_dependencies` | Shows outgoing dependencies. Set `raw_output=true` to include incoming dependents for impact analysis |
| `trace_flow` | Traces the calls made from a function |
| `get_call_graph_with_source` | Returns a call graph with the source of every function in it |
| `get_stats` | Summarizes an analyzed codebase |
| `load_knowledge_base` | Checks a saved knowledge base and reports its summary |
| `list_spaces`, `get_space_details` | Lists spaces, or describes one |
| `list_hyperspaces`, `get_hyperspace_details` | Lists hyperspaces, or describes one with its spaces |

## First query

1. Call `list_spaces` to choose a space.
2. Start with `get_component_from_description` and describe the behavior you want
   to find.
3. Pass the returned space-qualified component ID to `show_component`.
4. For change impact, call `show_dependencies` with `raw_output=true` so incoming
   dependents are included.
