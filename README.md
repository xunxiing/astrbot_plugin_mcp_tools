# astrbot_plugin_mcp_tools

`astrbot_plugin_mcp_tools` is now positioned as a plugin-side FastAPI REST gateway for AstrBot.

It does not proxy or reuse Dashboard routes. It talks to AstrBot managers and runtime objects directly inside the plugin process, then exposes those capabilities as stable REST resources.

## What It Exposes

The gateway is designed to cover as many useful AstrBot runtime capabilities as possible while keeping the code maintainable:

- System metadata and health checks
- Platform inspection and platform config lifecycle
- Plugin inspection, install, reload, enable, disable, update, uninstall
- Core config, abconf, and plugin config access
- Provider inspection, selection, lifecycle, and model listing
- Recent sessions, proactive messaging, and persisted message history
- Conversation CRUD and activation
- Knowledge base CRUD, search, and URL import
- Persona and persona-folder management
- Cron job management
- LLM tool inspection and enable/disable
- Subagent listing
- Synthetic inbound message injection

## Architecture

The implementation is split into small modules:

- `main.py`: plugin lifecycle, AstrBot loop bridging, session capture, and server startup
- `gateway/registry.py`: declarative endpoint registry
- `gateway/models.py`: request DTOs
- `gateway/services.py`: AstrBot manager-backed operations
- `gateway/server.py`: FastAPI app factory, auth, error handling, OpenAPI generation

This structure keeps the plugin maintainable and makes it easy to add new endpoints by extending the registry and service layer instead of building everything in one file.

## Authentication

All non-public endpoints require:

```http
Authorization: Bearer <auth_token>
```

Public endpoint:

- `GET /health`

If `auth_token` is empty, authenticated endpoints return an error instead of silently allowing access.

## Configuration

Key plugin config fields:

- `enable_server`
- `host`
- `port`
- `auth_token`
- `cors_allow_origins`
- `enable_docs`
- `docs_path`
- `openapi_path`
- `capture_recent_sessions`
- `max_recent_sessions`

Default server address:

```text
http://127.0.0.1:6185
```

## Synthetic Event Injection

The endpoint:

```text
POST /events/injections/message
```

injects a synthetic inbound message into AstrBot by using the `webchat` queue mechanism inside AstrBot itself. This means the injected event enters the normal message-processing pipeline instead of calling Dashboard APIs.

The injected event returns a `webchat`-style `unified_msg_origin`, which can then be used for conversation and provider-scoped operations.

## Example Requests

Health:

```bash
curl http://127.0.0.1:6185/health
```

Metadata:

```bash
curl -H "Authorization: Bearer change-me" \
  http://127.0.0.1:6185/meta
```

Send a proactive message:

```bash
curl -X POST \
  -H "Authorization: Bearer change-me" \
  -H "Content-Type: application/json" \
  http://127.0.0.1:6185/messages/send \
  -d '{
    "session": "aiocqhttp:friend_message:aiocqhttp!123456!123456",
    "message": "hello"
  }'
```

Inject a synthetic inbound message:

```bash
curl -X POST \
  -H "Authorization: Bearer change-me" \
  -H "Content-Type: application/json" \
  http://127.0.0.1:6185/events/injections/message \
  -d '{
    "sender_id": "rest-user",
    "conversation_id": "rest-conv",
    "message": "hello from injected event"
  }'
```

## OpenAPI

If `enable_docs` is enabled:

- Swagger UI: `/docs`
- OpenAPI JSON: `/openapi.json`

The OpenAPI schema is generated from the declarative endpoint registry and request models.
