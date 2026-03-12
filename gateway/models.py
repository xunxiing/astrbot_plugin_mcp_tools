from __future__ import annotations

from typing import Any

from pydantic import BaseModel, ConfigDict, Field


class GatewayModel(BaseModel):
    model_config = ConfigDict(extra="allow")


class MessagePayloadMixin(GatewayModel):
    message: str | None = None
    message_chain: list[dict[str, Any]] | None = None


class SendMessageRequest(MessagePayloadMixin):
    session: str


class SendLastMessageRequest(MessagePayloadMixin):
    pass


class InjectMessageRequest(MessagePayloadMixin):
    sender_id: str
    unified_msg_origin: str | None = None
    platform_id: str | None = None
    message_type: str | None = None
    session_id: str | None = None
    group_id: str | None = None
    self_id: str | None = None
    inject_via_platform: bool = False
    show_in_chat: bool = False
    show_in_webui: bool = False
    conversation_id: str | None = None
    creator: str | None = None
    display_name: str | None = None
    selected_provider: str | None = None
    selected_model: str | None = None
    enable_streaming: bool = True
    message_id: str | None = None
    action_type: str | None = None
    persist_history: bool = True
    ensure_webui_session: bool = True
    persist_bot_response: bool = True
    response_timeout_seconds: float = Field(default=120.0, ge=1.0, le=600.0)


class ConfigPatchRequest(GatewayModel):
    path: str
    value: Any
    create_missing: bool = True


class ConfigCreateRequest(GatewayModel):
    name: str | None = None
    data: dict[str, Any] | None = None


class RenameRequest(GatewayModel):
    name: str


class PlatformConfigRequest(GatewayModel):
    config: dict[str, Any]


class PluginInstallRepoRequest(GatewayModel):
    repo_url: str
    proxy: str = ""


class PluginInstallZipRequest(GatewayModel):
    zip_file_path: str


class PluginUninstallRequest(GatewayModel):
    delete_config: bool = False
    delete_data: bool = False


class PluginUpdateRequest(GatewayModel):
    proxy: str = ""


class PluginBulkUpdateRequest(GatewayModel):
    names: list[str]
    proxy: str = ""


class PluginSourceSaveRequest(GatewayModel):
    sources: list[Any]


class PluginConfigReplaceRequest(GatewayModel):
    config: dict[str, Any]


class ProviderSelectRequest(GatewayModel):
    provider_id: str
    provider_type: str
    umo: str | None = None


class ProviderConfigRequest(GatewayModel):
    config: dict[str, Any]


class ConversationCreateRequest(GatewayModel):
    unified_msg_origin: str
    platform_id: str = "rest"
    title: str | None = None
    persona_id: str | None = None


class ConversationActivateRequest(GatewayModel):
    unified_msg_origin: str


class ConversationUpdateRequest(GatewayModel):
    title: str | None = None
    persona_id: str | None = None
    history: list[dict[str, Any]] | None = None
    token_usage: int | None = None


class KnowledgeBaseCreateRequest(GatewayModel):
    kb_name: str
    description: str | None = None
    emoji: str | None = None
    embedding_provider_id: str
    rerank_provider_id: str | None = None
    chunk_size: int | None = None
    chunk_overlap: int | None = None
    top_k_dense: int | None = None
    top_k_sparse: int | None = None
    top_m_final: int | None = None


class KnowledgeBaseUpdateRequest(GatewayModel):
    kb_name: str | None = None
    description: str | None = None
    emoji: str | None = None
    embedding_provider_id: str | None = None
    rerank_provider_id: str | None = None
    chunk_size: int | None = None
    chunk_overlap: int | None = None
    top_k_dense: int | None = None
    top_k_sparse: int | None = None
    top_m_final: int | None = None


class KBSearchRequest(GatewayModel):
    query: str
    kb_names: list[str]
    top_k_fusion: int = 20
    top_m_final: int = 5


class KBUploadUrlRequest(GatewayModel):
    url: str
    chunk_size: int = 512
    chunk_overlap: int = 50
    batch_size: int = 32
    tasks_limit: int = 3
    max_retries: int = 3


class PersonaCreateRequest(GatewayModel):
    persona_id: str
    system_prompt: str
    begin_dialogs: list[str] | None = None
    tools: list[str] | None = None
    skills: list[str] | None = None
    folder_id: str | None = None
    sort_order: int = 0


class PersonaUpdateRequest(GatewayModel):
    system_prompt: str | None = None
    begin_dialogs: list[str] | None = None
    tools: list[str] | None = None
    skills: list[str] | None = None


class PersonaMoveRequest(GatewayModel):
    folder_id: str | None = None


class PersonaFolderCreateRequest(GatewayModel):
    name: str
    parent_id: str | None = None
    description: str | None = None
    sort_order: int = 0


class PersonaFolderUpdateRequest(GatewayModel):
    name: str | None = None
    parent_id: str | None = None
    description: str | None = None
    sort_order: int | None = None


class CronCreateRequest(GatewayModel):
    name: str
    session: str
    note: str | None = None
    cron_expression: str | None = None
    description: str | None = None
    timezone: str | None = None
    enabled: bool = True
    persistent: bool = True
    run_once: bool = False
    run_at: str | None = None


class CronUpdateRequest(GatewayModel):
    name: str | None = None
    description: str | None = None
    cron_expression: str | None = None
    timezone: str | None = None
    payload: dict[str, Any] | None = None
    enabled: bool | None = None
    persistent: bool | None = None
    run_once: bool | None = None
    status: str | None = None
    last_error: str | None = None
    next_run_time: str | None = None


class ToolToggleRequest(GatewayModel):
    active: bool


class ToolInvokeRequest(MessagePayloadMixin):
    arguments: dict[str, Any] = Field(default_factory=dict)
    sender_id: str = "rest-tool"
    conversation_id: str | None = None
    creator: str | None = None
    display_name: str | None = None
    config_id: str | None = None
    message_id: str | None = None
    tool_call_timeout: int = Field(default=60, ge=1, le=600)
    capture_messages: bool = True
    ensure_webui_session: bool = False
    persist_history: bool = False
    debug_arguments: bool = False
    include_base64: bool = False
    base64_max_bytes: int = Field(default=2 * 1024 * 1024, ge=1024, le=20 * 1024 * 1024)
    response_timeout_seconds: float = Field(default=3.0, ge=0.1, le=600.0)


class ConfigFileDeleteRequest(GatewayModel):
    path: str


class SkillToggleRequest(GatewayModel):
    active: bool


class SubagentConfigUpdateRequest(GatewayModel):
    config: dict[str, Any]


class MCPServerCreateRequest(GatewayModel):
    name: str
    active: bool = True
    config: dict[str, Any]


class MCPServerUpdateRequest(GatewayModel):
    name: str | None = None
    old_name: str | None = Field(default=None, validation_alias="oldName")
    active: bool | None = None
    config: dict[str, Any] | None = None


class MCPConnectionTestRequest(GatewayModel):
    mcp_server_config: dict[str, Any]


class MCPSyncProviderRequest(GatewayModel):
    name: str
    access_token: str = ""


class BatchSortRequest(GatewayModel):
    items: list[dict[str, Any]] = Field(default_factory=list)

