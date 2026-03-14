# AstrBot REST Gateway API

Declaration-driven FastAPI gateway for AstrBot runtime managers, plugin hooks, config, tools, skills, MCP, subagents, and synthetic inbound events.

## Base

- Default address: `http://127.0.0.1:6185`
- Swagger UI: `GET /docs`
- OpenAPI JSON: `GET /openapi.json`
- Public endpoint: `GET /health`
- Auth header: `Authorization: Bearer <auth_token>`
- Success envelope: `{"ok": true, "data": ...}`
- Error envelope: `{"ok": false, "error": {"message": "...", "details": [...]}}`
- Route source of truth: `gateway/registry.py`
- Runtime implementation source of truth: `gateway/services.py`

## Plugin Config

`_conf_schema.json`

- `enable_server`: start embedded FastAPI server
- `host`: bind host
- `port`: bind port
- `auth_token`: bearer token
- `cors_allow_origins`: CORS allowlist
- `enable_docs`: expose Swagger/OpenAPI
- `docs_path`: Swagger path
- `openapi_path`: OpenAPI path
- `capture_recent_sessions`: keep recent sessions for proactive send APIs
- `max_recent_sessions`: in-memory recent session cap

## Request Rules

- `GET` and most `DELETE` endpoints use path/query only.
- JSON endpoints accept `application/json`.
- Multipart endpoints accept `multipart/form-data`.
- All Pydantic request models allow extra keys, but only implemented fields are used.
- `POST /tools/{tool_name}/invoke` always takes tool kwargs under `arguments`; gateway control fields stay at the top level.
- `POST /tools/{tool_name}/invoke` normalizes emitted media into attachment objects with `attachment_id`, `filename`, `mime_type`, `size`, `file_token`, `url`, `download_path`, and `download_url`.
- Set `debug_arguments=true` to include UTF-8 safe argument diagnostics under `data.debug.arguments`, including path, length, SHA-256, and `unicode_escape` preview for every string argument.
- Set `include_base64=true` to inline emitted attachments as base64 when file size is within `base64_max_bytes`.
- Mutating endpoints are blocked in AstrBot demo mode and return `403`.

## Multipart Endpoints

- `POST /plugins/install/upload`
  - form field: `file`
- `POST /configs/plugins/{plugin_name}/files/upload?key=<dotpath>`
  - form field: `file`
  - optional fallback form field: `key`
- `POST /skills/upload`
  - form field: `file`

## Common Query Parameters

- `GET /configs/core`, `GET /configs/abconfs/{conf_id}`, `GET /configs/plugins/{plugin_name}`
  - `path`: dotpath snapshot
- `GET /configs/plugins/{plugin_name}/files`
  - `key`: plugin config dotpath of a `file` schema item
- `DELETE /configs/plugins/{plugin_name}/files`
  - `key`: plugin config dotpath of a `file` schema item
- `GET /platforms/stats/history`
  - `offset_sec`
- `GET /plugins/market`
  - `custom_registry`
  - `force_refresh=true|false`
- `GET /messages/history`
  - `platform_id`
  - `user_id`
  - `page`
  - `page_size`
- `GET /logs/history`, `GET /logs/compact`, `GET /logs/stream`
  - `limit`
  - `level` comma-separated, for example `INFO,ERROR`
  - `contains`
  - `wait_seconds` on `/logs/history` and `/logs/compact`
  - `replay_history=true|false` on `/logs/stream`
- `GET /conversations`
  - `page`
  - `page_size`
  - `platforms`
  - `message_types`
  - `exclude_ids`
  - `exclude_platforms`
  - `search`
- `GET /personas`
  - `folder_id`
- `GET /persona-folders`
  - `parent_id`
- `GET /cron/jobs`
  - `job_type`

## Route Index

### System

| Method | Path | Body | Notes |
| --- | --- | --- | --- |
| `GET` | `/health` | - | Public health check |
| `GET` | `/meta` | - | Gateway bind info and subsystem counts |
| `POST` | `/system/restart-core` | - | Restart AstrBot core without going through dashboard HTTP |

### Platforms

| Method | Path | Body | Notes |
| --- | --- | --- | --- |
| `GET` | `/platforms` | - | List loaded platform instances |
| `GET` | `/platforms/{platform_id}` | - | Get one platform instance |
| `GET` | `/platform-types` | - | List registered adapter types |
| `GET` | `/platforms/stats` | - | Live platform stats |
| `GET` | `/platforms/stats/history` | - | Persisted platform stats |
| `POST` | `/platforms` | `PlatformConfigRequest` | Create and optionally load a platform |
| `PUT` | `/platforms/{platform_id}` | `PlatformConfigRequest` | Update and reload a platform |
| `DELETE` | `/platforms/{platform_id}` | - | Delete and terminate a platform |
| `POST` | `/platforms/{platform_id}/reload` | - | Reload one platform |
| `POST` | `/platforms/{platform_id}/enable` | - | Enable one platform |
| `POST` | `/platforms/{platform_id}/disable` | - | Disable one platform |

### Plugins

| Method | Path | Body | Notes |
| --- | --- | --- | --- |
| `GET` | `/plugins` | - | List plugins with handlers, logo fields, failed plugin info |
| `GET` | `/plugins/{plugin_name}` | - | Get one plugin with config snapshot |
| `GET` | `/plugins/market` | - | Fetch plugin market with cache |
| `GET` | `/plugins/readme/{plugin_name}` | - | Read plugin README |
| `GET` | `/plugins/changelog/{plugin_name}` | - | Read plugin changelog if present |
| `GET` | `/plugins/sources` | - | Get custom plugin sources |
| `PUT` | `/plugins/sources` | `PluginSourceSaveRequest` | Save custom plugin sources |
| `POST` | `/plugins/install/repo` | `PluginInstallRepoRequest` | Install from repository URL |
| `POST` | `/plugins/install/zip` | `PluginInstallZipRequest` | Install from local zip path |
| `POST` | `/plugins/install/upload` | multipart | Upload and install plugin zip |
| `POST` | `/plugins/reload` | - | Reload all plugins |
| `POST` | `/plugins/{plugin_name}/reload` | - | Reload one plugin |
| `POST` | `/plugins/{plugin_name}/enable` | - | Enable one plugin |
| `POST` | `/plugins/{plugin_name}/disable` | - | Disable one plugin |
| `POST` | `/plugins/{plugin_name}/update` | `PluginUpdateRequest` | Update one plugin with optional proxy |
| `POST` | `/plugins/update-all` | `PluginBulkUpdateRequest` | Batch update plugins with optional proxy |
| `DELETE` | `/plugins/{plugin_name}` | `PluginUninstallRequest` | Uninstall plugin with config/data flags |

### Configs

| Method | Path | Body | Notes |
| --- | --- | --- | --- |
| `GET` | `/configs/core` | - | Snapshot core config |
| `PATCH` | `/configs/core` | `ConfigPatchRequest` | Patch core config by dotpath |
| `GET` | `/configs/abconfs` | - | List abconf files |
| `POST` | `/configs/abconfs` | `ConfigCreateRequest` | Create abconf |
| `GET` | `/configs/abconfs/{conf_id}` | - | Snapshot abconf |
| `PATCH` | `/configs/abconfs/{conf_id}` | `ConfigPatchRequest` | Patch abconf by dotpath |
| `POST` | `/configs/abconfs/{conf_id}/rename` | `RenameRequest` | Rename abconf |
| `DELETE` | `/configs/abconfs/{conf_id}` | - | Delete abconf |
| `GET` | `/configs/plugins` | - | List plugin config availability |
| `GET` | `/configs/plugins/{plugin_name}` | - | Snapshot plugin config plus schema |
| `PATCH` | `/configs/plugins/{plugin_name}` | `ConfigPatchRequest` | Patch plugin config, validate, save, reload |
| `PUT` | `/configs/plugins/{plugin_name}/full` | `PluginConfigReplaceRequest` | Replace full plugin config, validate, save, reload |
| `GET` | `/configs/plugins/{plugin_name}/files` | - | List files under a file-type config item |
| `POST` | `/configs/plugins/{plugin_name}/files/upload` | multipart | Upload file(s) for a file-type config item |
| `DELETE` | `/configs/plugins/{plugin_name}/files` | `ConfigFileDeleteRequest` | Delete a file under a file-type config item |

### Providers

| Method | Path | Body | Notes |
| --- | --- | --- | --- |
| `GET` | `/providers` | - | List loaded providers |
| `GET` | `/providers/{provider_id}` | - | Get one provider |
| `GET` | `/provider-types` | - | List provider templates |
| `GET` | `/providers/current` | - | Get current provider selection by UMO |
| `POST` | `/providers/current` | `ProviderSelectRequest` | Set current provider by UMO and type |
| `POST` | `/providers` | `ProviderConfigRequest` | Create provider |
| `PUT` | `/providers/{provider_id}` | `ProviderConfigRequest` | Update provider |
| `DELETE` | `/providers/{provider_id}` | - | Delete provider |
| `POST` | `/providers/{provider_id}/reload` | - | Reload provider |
| `GET` | `/providers/{provider_id}/models` | - | Query provider models |

### Messages

| Method | Path | Body | Notes |
| --- | --- | --- | --- |
| `GET` | `/messages/recent-sessions` | - | Recent sessions captured by the plugin |
| `POST` | `/messages/send` | `SendMessageRequest` | Send outbound message to a session |
| `POST` | `/messages/send-last` | `SendLastMessageRequest` | Send to last captured session |
| `GET` | `/messages/history` | - | Query persisted history |

### Logs

| Method | Path | Body | Notes |
| --- | --- | --- | --- |
| `GET` | `/logs/history` | - | Global cached logs with optional `level`, `contains`, `limit`, `wait_seconds` filters |
| `GET` | `/logs/compact` | - | Compact text-oriented global log view; also returns `lines` and joined `text` |
| `GET` | `/logs/stream` | - | Global SSE log stream with `level`, `contains`, `replay_history`, `Last-Event-ID` support |
| `GET` | `/logs/events/{event_id}` | - | Filter cached logs by one event/message id; supports `wait_seconds` for short watch |
| `GET` | `/logs/events/{event_id}/stream` | - | SSE stream of logs for one event/message id |

### Conversations

| Method | Path | Body | Notes |
| --- | --- | --- | --- |
| `GET` | `/conversations` | - | Filtered conversation list with pagination |
| `POST` | `/conversations` | `ConversationCreateRequest` | Create conversation |
| `GET` | `/conversations/{conversation_id}` | - | Get one conversation |
| `PATCH` | `/conversations/{conversation_id}` | `ConversationUpdateRequest` | Update title, persona, history, token usage |
| `DELETE` | `/conversations/{conversation_id}` | - | Delete conversation |
| `POST` | `/conversations/{conversation_id}/activate` | `ConversationActivateRequest` | Switch active conversation for a UMO |

### Knowledge Bases

| Method | Path | Body | Notes |
| --- | --- | --- | --- |
| `GET` | `/knowledge-bases` | - | List KBs |
| `POST` | `/knowledge-bases` | `KnowledgeBaseCreateRequest` | Create KB |
| `GET` | `/knowledge-bases/{kb_id}` | - | Get one KB |
| `PATCH` | `/knowledge-bases/{kb_id}` | `KnowledgeBaseUpdateRequest` | Update KB |
| `DELETE` | `/knowledge-bases/{kb_id}` | - | Delete KB |
| `GET` | `/knowledge-bases/{kb_id}/documents` | - | List KB documents |
| `POST` | `/knowledge-bases/search` | `KBSearchRequest` | Retrieve fused KB context |
| `POST` | `/knowledge-bases/{kb_id}/documents/url` | `KBUploadUrlRequest` | Crawl URL into KB |

### Personas

| Method | Path | Body | Notes |
| --- | --- | --- | --- |
| `GET` | `/personas` | - | List personas |
| `POST` | `/personas` | `PersonaCreateRequest` | Create persona |
| `GET` | `/personas/{persona_id}` | - | Get one persona |
| `PATCH` | `/personas/{persona_id}` | `PersonaUpdateRequest` | Update prompt, dialogs, tools, skills |
| `DELETE` | `/personas/{persona_id}` | - | Delete persona |
| `POST` | `/personas/{persona_id}/move` | `PersonaMoveRequest` | Move persona to folder |
| `GET` | `/persona-folders` | - | List persona folders |
| `GET` | `/persona-folders/tree` | - | Folder tree |
| `POST` | `/persona-folders` | `PersonaFolderCreateRequest` | Create persona folder |
| `PATCH` | `/persona-folders/{folder_id}` | `PersonaFolderUpdateRequest` | Update persona folder |
| `DELETE` | `/persona-folders/{folder_id}` | - | Delete persona folder |
| `POST` | `/personas/sort-order/batch` | `BatchSortRequest` | Batch sort update |

### Cron

| Method | Path | Body | Notes |
| --- | --- | --- | --- |
| `GET` | `/cron/jobs` | - | List cron jobs |
| `GET` | `/cron/jobs/{job_id}` | - | Get one cron job |
| `POST` | `/cron/jobs` | `CronCreateRequest` | Create active-agent cron job |
| `PATCH` | `/cron/jobs/{job_id}` | `CronUpdateRequest` | Update cron job |
| `DELETE` | `/cron/jobs/{job_id}` | - | Delete cron job |

### Tools And MCP

| Method | Path | Body | Notes |
| --- | --- | --- | --- |
| `GET` | `/tools` | - | List LLM tools with `origin` and `origin_name` |
| `GET` | `/tools/{tool_name}` | - | Get one tool |
| `POST` | `/tools/{tool_name}/invoke` | `ToolInvokeRequest` | Execute one tool directly with `arguments` and capture both return values and `event.send()` output |
| `POST` | `/tools/{tool_name}/toggle` | `ToolToggleRequest` | Enable or disable tool |
| `GET` | `/attachments/{attachment_id}/download` | - | Download one emitted attachment directly from the gateway |
| `GET` | `/tools/mcp/servers` | - | List MCP servers with tools and errlogs |
| `POST` | `/tools/mcp/servers` | `MCPServerCreateRequest` | Add MCP server |
| `PUT` | `/tools/mcp/servers/{server_name}` | `MCPServerUpdateRequest` | Update or rename MCP server |
| `DELETE` | `/tools/mcp/servers/{server_name}` | - | Delete MCP server |
| `POST` | `/tools/mcp/test` | `MCPConnectionTestRequest` | Test MCP server config |
| `POST` | `/tools/mcp/sync-provider` | `MCPSyncProviderRequest` | Sync MCP provider, currently `modelscope` |

### Subagents

| Method | Path | Body | Notes |
| --- | --- | --- | --- |
| `GET` | `/subagents` | - | List loaded handoff subagents |
| `GET` | `/subagents/config` | - | Read subagent orchestrator config |
| `PUT` | `/subagents/config` | `SubagentConfigUpdateRequest` | Save and reload subagent config |
| `GET` | `/subagents/available-tools` | - | List tools assignable to subagents |

### Skills

| Method | Path | Body | Notes |
| --- | --- | --- | --- |
| `GET` | `/skills` | - | List installed skills |
| `POST` | `/skills/upload` | multipart | Upload and install skill zip |
| `POST` | `/skills/{skill_name}/toggle` | `SkillToggleRequest` | Enable or disable skill |
| `DELETE` | `/skills/{skill_name}` | - | Delete skill |

### Events

| Method | Path | Body | Notes |
| --- | --- | --- | --- |
| `POST` | `/events/injections/message` | `InjectMessageRequest` | Inject a synthetic inbound message event into AstrBot |

## Important Request Bodies

### Create Plugin Source List

```json
{"sources":[{"name":"custom","url":"https://example.com/plugins.json"}]}
```

### Update One Plugin With Proxy

```json
{"proxy":"http://127.0.0.1:7890"}
```

### Batch Update Plugins

```json
{"names":["astrbot_plugin_a","astrbot_plugin_b"],"proxy":""}
```

### Replace Full Plugin Config

```json
{"config":{"auth_token":"change-me","port":6185,"enable_docs":true}}
```

### Delete One Plugin Config File

```json
{"path":"files/assets/logo.png"}
```

### Set Current Provider

```json
{"provider_id":"openai-default","provider_type":"chat","umo":"webchat:friend_message:webchat!u1!conv1"}
```

### Create Persona

```json
{"persona_id":"assistant","system_prompt":"You are an assistant.","begin_dialogs":["hi"],"tools":["web_search"],"skills":["rag"],"folder_id":null,"sort_order":0}
```

### Invoke One Tool

```json
{"arguments":{"query":"hello","top_k":5},"tool_call_timeout":60,"capture_messages":true,"persist_history":false,"debug_arguments":false,"include_base64":false,"base64_max_bytes":2097152}
```

### Debug Tool Arguments

```json
{"arguments":{"prompt":"閻㈣绔撮崣顏嗘閼规彃鐨悪鎰閿涘苯娼楅崷銊╂穿閸︿即鍣烽敍灞炬）缁粯褰冮悽濠氼棑閺嶇》绱濋弻鏂挎嫲閸忓鍤?},"debug_arguments":true,"capture_messages":false}
```

### Create MCP Server

```json
{"name":"my-mcp","active":true,"config":{"command":"npx","args":["-y","@modelcontextprotocol/server-filesystem","D:/data"]}}
```

### Update MCP Server

```json
{"name":"my-mcp-renamed","oldName":"my-mcp","active":true,"config":{"command":"npx","args":["-y","@modelcontextprotocol/server-filesystem","D:/data"]}}
```

### Test MCP Server Config

```json
{"mcp_server_config":{"command":"npx","args":["-y","@modelcontextprotocol/server-filesystem","D:/data"]}}
```

### Update Subagent Config

```json
{"config":{"main_enable":true,"remove_main_duplicate_tools":false,"agents":[{"name":"planner","tool_description":"Delegate planning tasks","instructions":"Plan first, then hand off.","tools":["web_search"],"provider_id":null,"persona_id":null}]}}
```

### Toggle Skill

```json
{"active":true}
```

### Log Watch By Event Id

History plus short watch:

```bash
curl -H "Authorization: Bearer change-me" "http://127.0.0.1:6185/logs/events/injected_1f8f87a90ca84102ba45cc12bb26aada?wait_seconds=5&limit=200"
```

Continuous SSE monitoring:

```bash
curl -N -H "Authorization: Bearer change-me" "http://127.0.0.1:6185/logs/events/injected_1f8f87a90ca84102ba45cc12bb26aada/stream"
```

`event_id` is a plain identifier match against AstrBot log entries. In practice this is most useful for `message_id` values such as `injected_xxx` and `tool_xxx` returned by this gateway.

### Inject Synthetic Inbound Message

WebChat-compatible injection:

```json
{"sender_id":"user-001","conversation_id":"conv-001","creator":"astrbot","display_name":"user-001","message":"hello from rest","selected_provider":"openai-default","selected_model":"gpt-4o-mini","enable_streaming":true,"persist_history":true,"ensure_webui_session":true,"persist_bot_response":true,"response_timeout_seconds":120}
```

True platform pipeline injection:

```json
{"unified_msg_origin":"napcat:GroupMessage:1030223077","sender_id":"3572245467","display_name":"rest-injector","message":"杩欐槸涓€鏉￠€氳繃 REST 浼€犵殑缇ゆ秷鎭紝搴旇杩涘叆 AstrBot pipeline","inject_via_platform":true,"show_in_chat":true}
```

You can also provide `platform_id`, `message_type`, `session_id`, and `group_id` separately instead of `unified_msg_origin`. When platform injection is selected, the gateway builds an `AstrBotMessage` and calls the platform adapter's `handle_msg(...)`, so the synthetic message enters AstrBot's normal inbound pipeline instead of only being pushed into the WebChat queue.

Set `show_in_chat=true` if you also want the injected request to be visible in the target chat:

- If the target platform is `webchat`, the gateway mirrors the injected request into the matching `webchat` conversation history.
- If the target platform is any other message platform such as `napcat`, the gateway performs a direct `send_message(...)` to that target session so the synthetic request becomes visible in the real chat without re-entering the pipeline.

`show_in_webui` is kept as a backward-compatible legacy alias, but `show_in_chat` is the preferred field because it correctly covers both WebChat and non-WebChat platforms.

`creator` defaults to the Dashboard username in AstrBot config. When `ensure_webui_session=true`, the legacy WebChat injection path creates or refreshes the matching `webchat` `platform_session`, so the injected conversation is visible in Dashboard WebUI. When `persist_bot_response=true`, the gateway also drains the `webchat` back-queue for the injected `message_id` and writes the bot reply into `platform_message_history`, so `GET /api/chat/get_session` can read the full exchange.

## Multipart Examples

### Upload Plugin Zip

```bash
curl -X POST "http://127.0.0.1:6185/plugins/install/upload" -H "Authorization: Bearer change-me" -F "file=@plugin.zip"
```

### Upload Plugin Config File

```bash
curl -X POST "http://127.0.0.1:6185/configs/plugins/my_plugin/files/upload?key=assets.logo" -H "Authorization: Bearer change-me" -F "file=@logo.png"
```

### Upload Skill Zip

```bash
curl -X POST "http://127.0.0.1:6185/skills/upload" -H "Authorization: Bearer change-me" -F "file=@skill.zip"
```

## Dashboard Parity Matrix

Current parity against the missing items audited for AstrBot Dashboard routes:

| Dashboard capability | REST gateway status | Endpoint(s) |
| --- | --- | --- |
| Plugin market list | implemented | `GET /plugins/market` |
| Plugin source get/save | implemented | `GET /plugins/sources`, `PUT /plugins/sources` |
| Plugin README and CHANGELOG | implemented | `GET /plugins/readme/{plugin_name}`, `GET /plugins/changelog/{plugin_name}` |
| Plugin upload install | implemented | `POST /plugins/install/upload` |
| Plugin update proxy | implemented | `POST /plugins/{plugin_name}/update` |
| Plugin bulk update | implemented | `POST /plugins/update-all` |
| Plugin reload all | implemented | `POST /plugins/reload` |
| Plugin handlers/logo/failed info | implemented | `GET /plugins`, `GET /plugins/{plugin_name}` |
| Plugin config full-save | implemented | `PUT /configs/plugins/{plugin_name}/full` |
| Plugin config schema validate | implemented | patch and full replace validate before save |
| Plugin config hot reload | implemented | patch and full replace reload plugin after save |
| Plugin config file get/upload/delete | implemented | `GET/POST/DELETE /configs/plugins/{plugin_name}/files...` |
| Skills list/upload/toggle/delete | implemented | `/skills*` |
| MCP server list/add/update/delete/test | implemented | `/tools/mcp/*` |
| MCP provider sync | implemented | `POST /tools/mcp/sync-provider` |
| MCP tools and errlogs in list output | implemented | `GET /tools/mcp/servers` |
| Tool origin metadata | implemented | `GET /tools`, `GET /tools/{tool_name}` |
| Subagent config get/save | implemented | `GET/PUT /subagents/config` |
| Subagent available tools | implemented | `GET /subagents/available-tools` |
| Multipart support | implemented | plugin upload, skill upload, config file upload |
| Demo mode write protection | implemented | all mutating endpoints guarded |
| Skill and MCP docs | implemented | this file |

## Remaining Differences From Dashboard

- The gateway intentionally keeps one normalized envelope: `{"ok": true, "data": ...}`.
- Plugin logo fields expose both `logo_token` and `logo`.
- `PUT /tools/mcp/servers/{server_name}` accepts both `old_name` and Dashboard-style `oldName`.
- Plugin reload-all is exposed as a dedicated REST resource instead of reusing Dashboard's single route semantics.
- The implementation calls AstrBot managers and internal helpers directly. It does not proxy the Dashboard HTTP API.

## Maintenance Notes

- Add or remove endpoints in `gateway/registry.py`.
- Keep request models in `gateway/models.py`.
- Keep AstrBot-internal logic in `gateway/services.py`.
- FastAPI route generation, auth, multipart parsing, and OpenAPI live in `gateway/server.py`.
- If you add a new write endpoint, keep demo mode parity by calling `_ensure_mutation_allowed()`.











## Log Examples

```bash
curl -H "Authorization: Bearer <token>" \
  "http://127.0.0.1:6324/logs/history?limit=100&level=ERROR,WARN&contains=napcat"
```

```bash
curl -H "Authorization: Bearer <token>" \
  "http://127.0.0.1:6324/logs/compact?limit=50&wait_seconds=2"
```

```bash
curl -N -H "Authorization: Bearer <token>" \
  -H "Last-Event-ID: 0" \
  "http://127.0.0.1:6324/logs/stream?replay_history=true&level=INFO"
```

```bash
curl -X POST -H "Authorization: Bearer <token>" \
  "http://127.0.0.1:6324/system/restart-core"
```


