# SuperMCP: Swagger UI & Substack Auth Plan

> **Note to AI Assistant:** Execute this plan to add Swagger documentation and outline the Substack token provisioning strategy.

## Phase 1: Swagger UI & OpenAPI Specification

FastMCP runs on Starlette and doesn't auto-generate OpenAPI specs like FastAPI does. We will manually inject the `/openapi.json` and `/docs/` routes into `src/app.py`. Since `BearerAuth` only protects paths starting with `/api`, these new endpoints will be publicly accessible (but executing the actual endpoints in the UI will still require the Bearer token).

### 1. Add `/openapi.json` Route

Add the following to `src/app.py` (before the ASGI setup):

```python
from starlette.responses import HTMLResponse
from starlette.responses import JSONResponse

@mcp.custom_route("/openapi.json", methods=["GET"])
async def api_openapi(req: Request) -> JSONResponse:
    """Returns the OpenAPI specification for the custom Starlette routes."""
    return JSONResponse({
        "openapi": "3.0.0",
        "info": {
            "title": "SuperMCP JSON API",
            "version": "1.0.0",
            "description": "API endpoints for the SuperMCP dashboard and quant tools."
        },
        "servers": [{"url": "https://mcp.mphinance.com"}],
        "components": {
            "securitySchemes": {
                "bearerAuth": {
                    "type": "http",
                    "scheme": "bearer"
                }
            }
        },
        "security": [{"bearerAuth": []}],
        "paths": {
            "/api/holdings": {
                "get": {
                    "summary": "Get live holdings",
                    "responses": {"200": {"description": "Successful Response"}}
                }
            },
            "/api/order/preview": {
                "post": {
                    "summary": "Preview a dry-run order",
                    "responses": {"200": {"description": "Order Preview JSON"}}
                }
            },
            "/api/order/execute": {
                "post": {
                    "summary": "Execute a live order (Requires Trade Scope)",
                    "responses": {"200": {"description": "Execution Result"}}
                }
            },
            "/api/accounts": {
                "get": {
                    "summary": "List linked accounts",
                    "responses": {"200": {"description": "Accounts Data"}}
                }
            }
        }
    })
```

### 2. Add `/docs/` Route for Swagger UI

Add this route to serve the Swagger UI HTML, loading scripts from `jsdelivr`.

```python
@mcp.custom_route("/docs/", methods=["GET"])
async def api_docs(req: Request) -> HTMLResponse:
    """Serve the Swagger UI HTML page."""
    html = '''
    <!DOCTYPE html>
    <html lang="en">
    <head>
        <meta charset="UTF-8">
        <title>SuperMCP API Docs</title>
        <link rel="stylesheet" href="https://cdn.jsdelivr.net/npm/swagger-ui-dist@5/swagger-ui.css">
    </head>
    <body>
        <div id="swagger-ui"></div>
        <script src="https://cdn.jsdelivr.net/npm/swagger-ui-dist@5/swagger-ui-bundle.js"></script>
        <script>
            window.onload = () => {
                window.ui = SwaggerUIBundle({
                    url: '/openapi.json',
                    dom_id: '#swagger-ui',
                });
            };
        </script>
    </body>
    </html>
    '''
    return HTMLResponse(html)
```

---

## Phase 2: Substack Token Provisioning (The "Creating/Auth for my peeps" Plan)

Currently, your web dashboard uses a Flask session cookie (`hub_session.member_ok()`) to authenticate Substack readers. This is great for web browsers, but **Claude Desktop and Cursor cannot use cookies**—they need a static API key (a `SUPERMCP_TOKEN`).

Since Substack doesn't have a public OAuth 2.0 flow for third-party desktop apps, you must generate and manage these keys yourself.

### Strategy: The API Key Generator Portal

Since you already have `tools.mphinance.com` authenticating users via Substack (the hub), you should use it as the token provisioning portal.

1. **The Portal Button**: Add a "Generate MCP Token" button to the `tools.mphinance.com` web dashboard. A logged-in Substack subscriber clicks this to get their personal API key.
2. **Admin Token Generation Route**: We need a new admin-gated endpoint in `supermcp` that the hub can call.
   * `POST /api/admin/keys/generate`
   * Hub authenticates with `SUPERMCP_ADMIN_TOKEN`.
   * Passes the subscriber's email/ID as the label.
   * `supermcp` uses `mcp_keys.py` to mint a new token with `role: viewer`, saves it, and returns the token string to the hub.
3. **Display to User**: The hub shows the generated token to the user *once* with instructions to paste it into their Claude Desktop `mcp.json` file.
4. **Revocation (Stripe/Webhooks)**: 
   * **Stripe API**: Since Substack runs on Stripe, the most robust way to manage cancellations is to periodically poll the Stripe API or listen to Stripe webhooks. When a user cancels their paid Substack subscription, the hub hits `DELETE /api/admin/keys/{label}` to revoke their MCP token.

### Execution Plan for AI

To implement the auth provisioning on the MCP side, you need to:
1. Create a `generate_key(label: str, role: str) -> str` function in `src/mcp_keys.py`.
2. Add a `@mcp.custom_route("/api/admin/keys", methods=["POST"])` in `src/app.py`.
3. Protect it by strictly checking for the `SUPERMCP_ADMIN_TOKEN` (the token gating in `BearerAuth` currently groups viewer/admin tokens, so inside the route, explicitly verify `admin` scope).
