# Security Policy

## Reporting a vulnerability

Please **do not** report security vulnerabilities through public GitHub
issues, discussions or pull requests.

Report them privately through GitHub's private vulnerability reporting:

1. Open the [Security tab](https://github.com/tum-gis/3dcitydb-mcp-server/security)
   of `tum-gis/3dcitydb-mcp-server`.
2. Click **Report a vulnerability**.
3. Describe the issue: affected version(s) and component (MCP server over
   stdio or HTTP/SSE, web UI, Docker images), steps to reproduce or a proof of
   concept, and the impact you expect.

Only the maintainers and you can see the report. We will acknowledge it,
work on a fix in private, and publish a GitHub security advisory together with
the fixed release. We are happy to credit you in the advisory.

## Supported versions

Security fixes are made for the latest release. Please check that the issue
is still present in the latest version before reporting it.

## Deployment notes

- The stdio transport (`3dcitydb-mcp`) is only reachable by the process that
  starts it.
- The HTTP/SSE transport (`3dcitydb-mcp-sse`) gives every client that passes
  authentication access to all tools, including `run_query`. Keep
  `CITYDB_MCP_AUTH_TOKEN` secret, bind to `127.0.0.1` where possible, and put a
  reverse proxy with TLS in front of the server for access from other machines.
- Connect the MCP server with a database role that has only the privileges it
  needs; the server additionally runs every session read-only.
- The Gradio web UI has no login. Do not expose it to untrusted networks.
