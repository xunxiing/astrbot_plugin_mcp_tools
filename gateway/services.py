from __future__ import annotations

import asyncio
import base64
import copy
import datetime as dt
import hashlib
import json
import mimetypes
import os
import ssl
import tempfile
import uuid
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any

import aiohttp
import certifi
from fastapi import Request
from starlette.responses import FileResponse, StreamingResponse

from astrbot.api import logger, sp
from astrbot.core import DEMO_MODE, file_token_service
from astrbot.core.agent.handoff import HandoffTool
from astrbot.core.agent.mcp_client import MCPTool
from astrbot.core.agent.run_context import ContextWrapper
from astrbot.core.astr_agent_context import AstrAgentContext
from astrbot.core.astr_agent_tool_exec import FunctionToolExecutor
from astrbot.core.db.po import ConversationV2
from astrbot.core.log import LogQueueHandler
from astrbot.core.message.message_event_result import MessageChain
from astrbot.core.platform import AstrBotMessage, Group, MessageMember, PlatformMetadata
from astrbot.core.platform.message_session import MessageSession
from astrbot.core.platform.message_type import MessageType
from astrbot.core.platform.register import platform_registry
from astrbot.core.platform.sources.webchat.message_parts_helper import (
    message_chain_to_storage_message_parts,
)
from astrbot.core.platform.sources.webchat.webchat_event import WebChatMessageEvent
from astrbot.core.platform.sources.webchat.webchat_queue_mgr import webchat_queue_mgr
from astrbot.core.provider.entities import ProviderType
from astrbot.core.provider.provider import (
    EmbeddingProvider,
    Provider,
    RerankProvider,
    STTProvider,
    TTSProvider,
)
from astrbot.core.provider.register import provider_registry
from astrbot.core.skills.skill_manager import SkillManager
from astrbot.core.star.filter.command import CommandFilter
from astrbot.core.star.filter.command_group import CommandGroupFilter
from astrbot.core.star.filter.permission import PermissionTypeFilter
from astrbot.core.star.filter.regex import RegexFilter
from astrbot.core.star.star import star_map
from astrbot.core.star.star_handler import EventType, star_handlers_registry
from astrbot.core.utils.astrbot_path import (
    get_astrbot_data_path,
    get_astrbot_plugin_data_path,
    get_astrbot_temp_path,
)
from astrbot.dashboard.routes.config import save_config, validate_config
from astrbot.dashboard.routes.util import (
    config_key_to_folder,
    get_schema_item,
    normalize_rel_path,
    sanitize_filename,
)

from .models import (
    BatchSortRequest,
    ConfigCreateRequest,
    ConfigFileDeleteRequest,
    ConfigPatchRequest,
    ConversationActivateRequest,
    ConversationCreateRequest,
    ConversationUpdateRequest,
    CronCreateRequest,
    CronUpdateRequest,
    InjectMessageRequest,
    KBSearchRequest,
    KBUploadUrlRequest,
    KnowledgeBaseCreateRequest,
    KnowledgeBaseUpdateRequest,
    MCPConnectionTestRequest,
    MCPServerCreateRequest,
    MCPServerUpdateRequest,
    MCPSyncProviderRequest,
    PersonaCreateRequest,
    PersonaFolderCreateRequest,
    PersonaFolderUpdateRequest,
    PersonaMoveRequest,
    PersonaUpdateRequest,
    PlatformConfigRequest,
    PluginBulkUpdateRequest,
    PluginConfigReplaceRequest,
    PluginInstallRepoRequest,
    PluginInstallZipRequest,
    PluginSourceSaveRequest,
    PluginUninstallRequest,
    PluginUpdateRequest,
    ProviderConfigRequest,
    ProviderSelectRequest,
    RenameRequest,
    SendLastMessageRequest,
    SendMessageRequest,
    SkillToggleRequest,
    SubagentConfigUpdateRequest,
    ToolInvokeRequest,
    ToolToggleRequest,
)
from .utils import (
    GatewayError,
    build_message_chain,
    ensure_mapping,
    get_by_dotpath,
    now_iso,
    serialize_value,
    set_by_dotpath,
    to_plain_message_parts,
)

MAX_CONFIG_FILE_BYTES = 500 * 1024 * 1024


class GatewayServices:
    def __init__(self, plugin: Any):
        self.plugin = plugin

    @property
    def context(self):
        return self.plugin.context

    @property
    def acm(self):
        return self.context.astrbot_config_mgr

    @property
    def db(self):
        return self.context.get_db()

    @property
    def platform_manager(self):
        return self.context.platform_manager

    @property
    def provider_manager(self):
        return self.context.provider_manager

    @property
    def conversation_manager(self):
        return self.context.conversation_manager

    @property
    def plugin_manager(self):
        return self.context._star_manager

    @property
    def persona_manager(self):
        return self.context.persona_manager

    @property
    def kb_manager(self):
        return self.context.kb_manager

    @property
    def cron_manager(self):
        return self.context.cron_manager

    @property
    def message_history_manager(self):
        return self.context.message_history_manager

    @property
    def subagent_orchestrator(self):
        return self.context.subagent_orchestrator

    def _ensure_mutation_allowed(self) -> None:
        if DEMO_MODE:
            raise GatewayError(
                "You are not permitted to do this operation in demo mode",
                status_code=403,
            )

    def _query_int(self, request: Request, name: str, default: int) -> int:
        raw = request.query_params.get(name)
        if raw is None or raw == "":
            return default
        try:
            return int(raw)
        except ValueError as exc:
            raise GatewayError(f"invalid integer query parameter: {name}") from exc

    def _query_csv(self, request: Request, name: str) -> list[str]:
        raw = request.query_params.get(name, "")
        return [item.strip() for item in raw.split(",") if item.strip()]

    def _query_float(self, request: Request, name: str, default: float) -> float:
        raw = request.query_params.get(name)
        if raw is None or raw == "":
            return default
        try:
            return float(raw)
        except ValueError as exc:
            raise GatewayError(f"invalid float query parameter: {name}") from exc

    def _query_bool(self, request: Request, name: str, default: bool) -> bool:
        raw = request.query_params.get(name)
        if raw is None or raw == "":
            return default
        value = raw.strip().lower()
        if value in {"1", "true", "yes", "on"}:
            return True
        if value in {"0", "false", "no", "off"}:
            return False
        raise GatewayError(f"invalid boolean query parameter: {name}")

    def _require_query(self, request: Request, name: str) -> str:
        value = request.query_params.get(name)
        if not value:
            raise GatewayError(f"missing query parameter: {name}")
        return value

    def _resolve_platform_config(self, platform_id: str) -> dict[str, Any]:
        for item in self.acm.default_conf.get("platform", []):
            if item.get("id") == platform_id:
                return item
        raise GatewayError(f"platform not found: {platform_id}", status_code=404)

    def _resolve_platform_inst(self, platform_id: str):
        for platform in self.platform_manager.get_insts():
            try:
                if platform.meta().id == platform_id:
                    return platform
            except Exception:
                continue
        raise GatewayError(f"platform not found: {platform_id}", status_code=404)

    def _resolve_provider_config(self, provider_id: str) -> dict[str, Any]:
        for item in self.acm.default_conf.get("provider", []):
            if item.get("id") == provider_id:
                return item
        raise GatewayError(f"provider not found: {provider_id}", status_code=404)

    def _resolve_plugin(self, plugin_name: str):
        lowered = plugin_name.lower()
        for star in self.context.get_all_stars():
            candidates = {
                str(getattr(star, "name", "")).lower(),
                str(getattr(star, "root_dir_name", "")).lower(),
                str(getattr(star, "display_name", "")).lower(),
            }
            if lowered in candidates:
                return star
        raise GatewayError(f"plugin not found: {plugin_name}", status_code=404)

    def _resolve_abconf(self, conf_id: str):
        if conf_id == "default":
            return self.acm.default_conf
        conf = self.acm.confs.get(conf_id)
        if not conf:
            raise GatewayError(f"abconf not found: {conf_id}", status_code=404)
        return conf

    def _resolve_tool(self, tool_name: str):
        for tool in self.provider_manager.llm_tools.func_list:
            if getattr(tool, "name", None) == tool_name:
                return tool
        raise GatewayError(f"tool not found: {tool_name}", status_code=404)

    def _plugin_dir(self, plugin: Any) -> Path:
        root_dir_name = getattr(plugin, "root_dir_name", None)
        if not root_dir_name:
            raise GatewayError(
                f"plugin directory missing: {plugin.name}", status_code=404
            )
        return Path(self.plugin_manager.plugin_store_path) / root_dir_name

    async def _plugin_logo_token(self, star: Any) -> str | None:
        logo_path = getattr(star, "logo_path", None)
        if not logo_path:
            return None
        try:
            return await file_token_service.register_file(logo_path, timeout=300)
        except Exception:
            logger.warning("Failed to register plugin logo token.", exc_info=True)
            return None

    async def _plugin_logo_fields(self, star: Any) -> dict[str, str | None]:
        token = await self._plugin_logo_token(star)
        return {
            "logo_token": token,
            "logo": f"/api/file/{token}" if token else None,
        }

    def _translated_event_type(self) -> dict[Any, str]:
        return {
            EventType.AdapterMessageEvent: "adapter_message",
            EventType.OnLLMRequestEvent: "llm_request",
            EventType.OnLLMResponseEvent: "llm_response",
            EventType.OnDecoratingResultEvent: "decorate_result",
            EventType.OnCallingFuncToolEvent: "calling_tool",
            EventType.OnAfterMessageSentEvent: "after_message_sent",
        }

    async def _plugin_handlers_info(
        self, handler_full_names: list[str]
    ) -> list[dict[str, Any]]:
        handlers: list[dict[str, Any]] = []
        translated = self._translated_event_type()
        for handler_full_name in handler_full_names:
            handler = star_handlers_registry.star_handlers_map.get(handler_full_name)
            if handler is None:
                continue

            info: dict[str, Any] = {
                "event_type": handler.event_type.name,
                "event_type_h": translated.get(
                    handler.event_type, handler.event_type.name
                ),
                "handler_full_name": handler.handler_full_name,
                "handler_name": handler.handler_name,
                "desc": handler.desc or "no description",
            }
            if handler.event_type == EventType.AdapterMessageEvent:
                has_admin = False
                for filter_ in handler.event_filters:
                    if isinstance(filter_, CommandFilter):
                        info["type"] = "command"
                        info["cmd"] = (
                            f"{filter_.parent_command_names[0]} {filter_.command_name}"
                        ).strip()
                    elif isinstance(filter_, CommandGroupFilter):
                        info["type"] = "command_group"
                        info["cmd"] = filter_.get_complete_command_names()[0].strip()
                        info["sub_command"] = filter_.print_cmd_tree(
                            filter_.sub_command_filters
                        )
                    elif isinstance(filter_, RegexFilter):
                        info["type"] = "regex"
                        info["cmd"] = filter_.regex_str
                    elif isinstance(filter_, PermissionTypeFilter):
                        has_admin = True
                info["has_admin"] = has_admin
                info.setdefault("cmd", "unknown")
                info.setdefault("type", "event_listener")
            else:
                info["cmd"] = "auto"
                info["type"] = "scheduled"
            handlers.append(info)
        return handlers

    def _runtime_skill_manager(self) -> tuple[SkillManager, str]:
        runtime = self.acm.default_conf.get("provider_settings", {}).get(
            "computer_use_runtime", "local"
        )
        return SkillManager(), runtime

    def _tool_origin(self, tool: Any) -> tuple[str, str]:
        if isinstance(tool, MCPTool):
            return "mcp", getattr(tool, "mcp_server_name", "unknown")
        handler_module_path = getattr(tool, "handler_module_path", None)
        if handler_module_path and star_map.get(handler_module_path):
            return "plugin", star_map[handler_module_path].name or "unknown"
        return "unknown", "unknown"

    def _config_file_context(
        self, plugin_name: str, key_path: str
    ) -> tuple[Any, dict[str, Any], Path]:
        plugin = self._resolve_plugin(plugin_name)
        config = getattr(plugin, "config", None)
        if config is None:
            raise GatewayError(f"plugin has no config: {plugin.name}", status_code=404)
        meta = get_schema_item(getattr(config, "schema", None), key_path)
        if not meta or meta.get("type") != "file":
            raise GatewayError(
                "config item not found or not file type", status_code=404
            )
        storage_root_path = Path(get_astrbot_plugin_data_path()).resolve(strict=False)
        plugin_root_path = (storage_root_path / plugin.name).resolve(strict=False)
        try:
            plugin_root_path.relative_to(storage_root_path)
        except ValueError as exc:
            raise GatewayError("invalid plugin storage scope") from exc
        plugin_root_path.mkdir(parents=True, exist_ok=True)
        return plugin, meta, plugin_root_path

    def _validate_plugin_config_snapshot(
        self, plugin: Any, snapshot: dict[str, Any]
    ) -> dict[str, Any]:
        errors, validated = validate_config(
            copy.deepcopy(snapshot), getattr(plugin.config, "schema", {}), False
        )
        if errors:
            raise GatewayError(f"config validation failed: {errors}")
        return validated

    async def _save_plugin_config_and_reload(
        self, plugin: Any, snapshot: dict[str, Any]
    ) -> None:
        validated = self._validate_plugin_config_snapshot(plugin, snapshot)
        plugin.config.save_config(validated)
        success, error = await self.plugin_manager.reload(plugin.name)
        if not success:
            raise GatewayError(str(error or "plugin reload failed"), status_code=500)

    def _plugin_market_cache_file(
        self, custom_url: str | None
    ) -> tuple[list[str], str, str | None]:
        if custom_url:
            url_hash = hashlib.md5(custom_url.encode()).hexdigest()[:8]
            cache_file = os.path.join(
                get_astrbot_data_path(), f"plugins_custom_{url_hash}.json"
            )
            md5_url = (
                custom_url[:-5] + "-md5.json"
                if custom_url.endswith(".json")
                else custom_url + "-md5.json"
            )
            return [custom_url], cache_file, md5_url
        cache_file = os.path.join(get_astrbot_data_path(), "plugins.json")
        return (
            [
                "https://api.soulter.top/astrbot/plugins",
                "https://github.com/AstrBotDevs/AstrBot_Plugins_Collection/raw/refs/heads/main/plugin_cache_original.json",
            ],
            cache_file,
            "https://api.soulter.top/astrbot/plugins-md5",
        )

    def _load_json_cache(self, cache_file: str) -> dict[str, Any] | None:
        if not os.path.exists(cache_file):
            return None
        try:
            with open(cache_file, encoding="utf-8") as file:
                return json.load(file)
        except Exception:
            logger.warning("Failed to load plugin market cache.", exc_info=True)
            return None

    def _save_json_cache(self, cache_file: str, data: Any, md5: str | None) -> None:
        os.makedirs(os.path.dirname(cache_file), exist_ok=True)
        with open(cache_file, "w", encoding="utf-8") as file:
            json.dump(
                {"timestamp": now_iso(), "data": data, "md5": md5 or ""},
                file,
                ensure_ascii=False,
                indent=2,
            )

    async def _fetch_remote_md5(self, md5_url: str | None) -> str | None:
        if not md5_url:
            return None
        ssl_context = ssl.create_default_context(cafile=certifi.where())
        connector = aiohttp.TCPConnector(ssl=ssl_context)
        try:
            async with aiohttp.ClientSession(
                trust_env=True, connector=connector
            ) as session:
                async with session.get(md5_url) as response:
                    if response.status == 200:
                        data = await response.json()
                        return data.get("md5", "")
        except Exception:
            logger.debug("Failed to fetch plugin market md5.", exc_info=True)
        return None

    async def _is_plugin_market_cache_valid(
        self, cache_file: str, md5_url: str | None
    ) -> bool:
        cached = self._load_json_cache(cache_file)
        if not cached or not cached.get("md5"):
            return False
        remote_md5 = await self._fetch_remote_md5(md5_url)
        if remote_md5 is None:
            return True
        return cached.get("md5") == remote_md5

    def _serialize_platform(self, platform: Any) -> dict[str, Any]:
        data = serialize_value(platform.get_stats())
        data["config"] = serialize_value(getattr(platform, "config", {}))
        return data

    def _provider_role(self, provider: Any) -> str:
        if isinstance(provider, Provider):
            return ProviderType.CHAT_COMPLETION.value
        if isinstance(provider, STTProvider):
            return ProviderType.SPEECH_TO_TEXT.value
        if isinstance(provider, TTSProvider):
            return ProviderType.TEXT_TO_SPEECH.value
        if isinstance(provider, EmbeddingProvider):
            return ProviderType.EMBEDDING.value
        if isinstance(provider, RerankProvider):
            return ProviderType.RERANK.value
        return "unknown"

    def _serialize_provider(self, provider: Any) -> dict[str, Any]:
        meta = provider.meta()
        return {
            "id": meta.id,
            "type": meta.type,
            "provider_type": meta.provider_type.value,
            "model": meta.model,
            "role": self._provider_role(provider),
            "config": serialize_value(getattr(provider, "provider_config", {})),
            "is_current": {
                "chat": provider is self.provider_manager.curr_provider_inst,
                "stt": provider is self.provider_manager.curr_stt_provider_inst,
                "tts": provider is self.provider_manager.curr_tts_provider_inst,
            },
        }

    def _serialize_provider_type(self, meta: Any) -> dict[str, Any]:
        return {
            "type": meta.type,
            "provider_type": meta.provider_type.value,
            "display_name": meta.provider_display_name or meta.type,
            "description": meta.desc,
            "default_config_tmpl": serialize_value(meta.default_config_tmpl),
        }

    def _serialize_platform_type(self, meta: Any) -> dict[str, Any]:
        return {
            "id": meta.id,
            "type": meta.name,
            "display_name": meta.adapter_display_name or meta.name,
            "description": meta.description,
            "default_config_tmpl": serialize_value(meta.default_config_tmpl),
            "support_streaming_message": meta.support_streaming_message,
        }

    def _serialize_plugin(self, star: Any) -> dict[str, Any]:
        return {
            "name": star.name,
            "display_name": getattr(star, "display_name", None),
            "author": getattr(star, "author", None),
            "desc": getattr(star, "desc", None),
            "version": getattr(star, "version", None),
            "repo": getattr(star, "repo", None),
            "activated": bool(getattr(star, "activated", False)),
            "reserved": bool(getattr(star, "reserved", False)),
            "root_dir_name": getattr(star, "root_dir_name", None),
            "has_config": getattr(star, "config", None) is not None,
            "module_path": getattr(star, "module_path", None),
            "logo_path": getattr(star, "logo_path", None),
        }

    def _serialize_conversation_v2(self, conv: ConversationV2) -> dict[str, Any]:
        return {
            "conversation_id": conv.conversation_id,
            "platform_id": conv.platform_id,
            "user_id": conv.user_id,
            "content": serialize_value(conv.content),
            "title": conv.title,
            "persona_id": conv.persona_id,
            "token_usage": conv.token_usage,
            "created_at": serialize_value(conv.created_at),
            "updated_at": serialize_value(conv.updated_at),
        }

    def _serialize_persona(self, persona: Any) -> dict[str, Any]:
        return {
            "persona_id": persona.persona_id,
            "system_prompt": persona.system_prompt,
            "begin_dialogs": serialize_value(persona.begin_dialogs),
            "tools": serialize_value(persona.tools),
            "skills": serialize_value(persona.skills),
            "folder_id": persona.folder_id,
            "sort_order": persona.sort_order,
            "created_at": serialize_value(persona.created_at),
            "updated_at": serialize_value(persona.updated_at),
        }

    def _serialize_persona_folder(self, folder: Any) -> dict[str, Any]:
        return {
            "folder_id": folder.folder_id,
            "name": folder.name,
            "parent_id": folder.parent_id,
            "description": folder.description,
            "sort_order": folder.sort_order,
            "created_at": serialize_value(folder.created_at),
            "updated_at": serialize_value(folder.updated_at),
        }

    def _serialize_cron_job(self, job: Any) -> dict[str, Any]:
        return {
            "job_id": job.job_id,
            "name": job.name,
            "description": job.description,
            "job_type": job.job_type,
            "cron_expression": job.cron_expression,
            "timezone": job.timezone,
            "payload": serialize_value(job.payload),
            "enabled": job.enabled,
            "persistent": job.persistent,
            "run_once": job.run_once,
            "status": job.status,
            "last_run_at": serialize_value(job.last_run_at),
            "next_run_time": serialize_value(job.next_run_time),
            "last_error": job.last_error,
            "created_at": serialize_value(job.created_at),
            "updated_at": serialize_value(job.updated_at),
        }

    def _serialize_tool(self, tool: Any) -> dict[str, Any]:
        origin, origin_name = self._tool_origin(tool)
        return {
            "name": getattr(tool, "name", None),
            "description": getattr(tool, "description", None),
            "parameters": serialize_value(getattr(tool, "parameters", None)),
            "active": bool(getattr(tool, "active", True)),
            "handler_module_path": getattr(tool, "handler_module_path", None),
            "type": tool.__class__.__name__,
            "origin": origin,
            "origin_name": origin_name,
        }

    def _serialize_subagent(self, handoff: Any) -> dict[str, Any]:
        agent = getattr(handoff, "agent", None)
        return {
            "name": getattr(handoff, "name", None),
            "tool_description": getattr(
                handoff, "tool_description", getattr(handoff, "description", None)
            ),
            "provider_id": getattr(handoff, "provider_id", None),
            "agent": {
                "name": getattr(agent, "name", None),
                "instructions": getattr(agent, "instructions", None),
                "tools": serialize_value(getattr(agent, "tools", None)),
                "begin_dialogs": serialize_value(getattr(agent, "begin_dialogs", None)),
            },
        }

    def _serialize_kb(self, kb: Any) -> dict[str, Any]:
        return {
            "kb_id": kb.kb_id,
            "kb_name": kb.kb_name,
            "description": kb.description,
            "emoji": kb.emoji,
            "embedding_provider_id": kb.embedding_provider_id,
            "rerank_provider_id": kb.rerank_provider_id,
            "chunk_size": kb.chunk_size,
            "chunk_overlap": kb.chunk_overlap,
            "top_k_dense": kb.top_k_dense,
            "top_k_sparse": kb.top_k_sparse,
            "top_m_final": kb.top_m_final,
            "doc_count": getattr(kb, "doc_count", None),
            "chunk_count": getattr(kb, "chunk_count", None),
            "created_at": serialize_value(getattr(kb, "created_at", None)),
            "updated_at": serialize_value(getattr(kb, "updated_at", None)),
        }

    def _snapshot_config(self, conf: Any, path: str | None = None) -> dict[str, Any]:
        value = get_by_dotpath(conf, path)
        return {"path": path, "value": serialize_value(value)}

    def _apply_config_patch(self, conf: Any, req: ConfigPatchRequest) -> dict[str, Any]:
        set_by_dotpath(conf, req.path, req.value, create_missing=req.create_missing)
        conf.save_config()
        return self._snapshot_config(conf, req.path)

    def _provider_type_from_string(self, value: str) -> ProviderType:
        normalized = value.strip().lower()
        aliases = {
            "chat": ProviderType.CHAT_COMPLETION,
            "chat_completion": ProviderType.CHAT_COMPLETION,
            "stt": ProviderType.SPEECH_TO_TEXT,
            "speech_to_text": ProviderType.SPEECH_TO_TEXT,
            "tts": ProviderType.TEXT_TO_SPEECH,
            "text_to_speech": ProviderType.TEXT_TO_SPEECH,
            "embedding": ProviderType.EMBEDDING,
            "rerank": ProviderType.RERANK,
        }
        if normalized not in aliases:
            raise GatewayError(f"unsupported provider_type: {value}")
        return aliases[normalized]

    def _dashboard_username(self) -> str:
        dashboard = self.acm.default_conf.get("dashboard", {})
        return str(dashboard.get("username") or "astrbot")

    def _resolve_message_type(self, value: MessageType | str | None) -> MessageType:
        if isinstance(value, MessageType):
            return value
        normalized = str(value or "").strip().lower()
        aliases = {
            "group": MessageType.GROUP_MESSAGE,
            "groupmessage": MessageType.GROUP_MESSAGE,
            "group_message": MessageType.GROUP_MESSAGE,
            MessageType.GROUP_MESSAGE.value.lower(): MessageType.GROUP_MESSAGE,
            "friend": MessageType.FRIEND_MESSAGE,
            "private": MessageType.FRIEND_MESSAGE,
            "friendmessage": MessageType.FRIEND_MESSAGE,
            "friend_message": MessageType.FRIEND_MESSAGE,
            MessageType.FRIEND_MESSAGE.value.lower(): MessageType.FRIEND_MESSAGE,
            "other": MessageType.OTHER_MESSAGE,
            "othermessage": MessageType.OTHER_MESSAGE,
            "other_message": MessageType.OTHER_MESSAGE,
            MessageType.OTHER_MESSAGE.value.lower(): MessageType.OTHER_MESSAGE,
        }
        if normalized not in aliases:
            raise GatewayError(f"unsupported message_type: {value}")
        return aliases[normalized]

    def _should_inject_via_platform(self, payload: InjectMessageRequest) -> bool:
        if payload.inject_via_platform:
            return True
        return any(
            value
            for value in (
                payload.unified_msg_origin,
                payload.platform_id,
                payload.message_type,
                payload.session_id,
                payload.group_id,
                payload.self_id,
            )
        )

    def _resolve_injection_target(
        self, payload: InjectMessageRequest
    ) -> dict[str, Any]:
        platform_id = payload.platform_id
        message_type: MessageType | str | None = payload.message_type
        session_id = payload.session_id
        if payload.unified_msg_origin:
            try:
                session = MessageSession.from_str(payload.unified_msg_origin)
            except Exception as exc:
                raise GatewayError(
                    f"invalid unified_msg_origin: {payload.unified_msg_origin}"
                ) from exc
            platform_id = platform_id or session.platform_id
            message_type = message_type or session.message_type
            session_id = session_id or session.session_id
        if not platform_id:
            raise GatewayError("platform_id is required when injecting via platform")
        if not session_id:
            raise GatewayError("session_id is required when injecting via platform")
        resolved_message_type = self._resolve_message_type(message_type)
        group_id = payload.group_id
        if resolved_message_type == MessageType.GROUP_MESSAGE and not group_id:
            group_id = session_id
        return {
            "platform_id": platform_id,
            "message_type": resolved_message_type,
            "session_id": session_id,
            "group_id": group_id,
            "unified_msg_origin": (
                payload.unified_msg_origin
                or f"{platform_id}:{resolved_message_type.value}:{session_id}"
            ),
        }

    def _platform_injection_display_conversation_id(
        self,
        payload: InjectMessageRequest,
        target: dict[str, Any],
    ) -> str:
        explicit = str(payload.conversation_id or "").strip()
        if explicit:
            return explicit
        message_type = target["message_type"]
        message_type_value = (
            message_type.value
            if isinstance(message_type, MessageType)
            else str(message_type or "unknown")
        )
        return (
            f"platform-injection:{target['platform_id']}:"
            f"{message_type_value}:{target['session_id']}"
        )

    async def _mirror_platform_injection_request_to_webui(
        self,
        payload: InjectMessageRequest,
        target: dict[str, Any],
        parts: list[dict[str, Any]],
    ) -> dict[str, Any]:
        conversation_id = self._platform_injection_display_conversation_id(
            payload, target
        )
        creator = payload.creator or self._dashboard_username()
        display_name = payload.display_name or payload.sender_id
        await self._ensure_webchat_platform_session(
            conversation_id=conversation_id,
            creator=creator,
            display_name=display_name,
        )
        await self._save_webchat_history_message(
            conversation_id,
            sender_id=payload.sender_id,
            sender_name=display_name,
            message_parts=parts,
        )
        return {
            "conversation_id": conversation_id,
            "creator": creator,
            "display_name": display_name,
        }

    def _should_show_in_chat(self, payload: InjectMessageRequest) -> bool:
        return bool(payload.show_in_chat or payload.show_in_webui)

    async def _show_platform_injection_in_target_chat(
        self,
        payload: InjectMessageRequest,
        target: dict[str, Any],
        parts: list[dict[str, Any]],
    ) -> dict[str, Any] | None:
        if not self._should_show_in_chat(payload):
            return None

        if target["platform_id"] == "webchat":
            session = await self._mirror_platform_injection_request_to_webui(
                payload,
                target,
                parts,
            )
            return {
                "mode": "webchat_history",
                "sent": True,
                "conversation_id": session["conversation_id"],
                "creator": session["creator"],
                "display_name": session["display_name"],
            }

        visible_chain = (
            build_message_chain(payload.message, payload.message_chain)
            if payload.message_chain
            else build_message_chain(None, parts)
        )
        ok = await self.context.send_message(target["unified_msg_origin"], visible_chain)
        return {
            "mode": "platform_send",
            "sent": bool(ok),
            "session": target["unified_msg_origin"],
        }

    async def _message_event_result_to_storage_parts(
        self,
        result: Any,
    ) -> list[dict[str, Any]]:
        chain = getattr(result, "chain", None)
        if chain is None:
            return []
        if isinstance(chain, list):
            chain = MessageChain(chain=list(chain))
        parts = await message_chain_to_storage_message_parts(
            chain,
            insert_attachment=self.db.insert_attachment,
            attachments_dir=Path(get_astrbot_data_path()) / "attachments",
        )
        return self._history_safe_message_parts(parts)

    async def mirror_injected_platform_response(
        self,
        event: Any,
    ) -> None:
        message_obj = getattr(event, "message_obj", None)
        raw_message = getattr(message_obj, "raw_message", None)
        if not isinstance(raw_message, dict):
            return
        display_meta = raw_message.get("_rest_display")
        if not isinstance(display_meta, dict) or not display_meta.get("enabled"):
            return

        conversation_id = str(display_meta.get("conversation_id") or "").strip()
        if not conversation_id:
            return

        creator = str(display_meta.get("creator") or self._dashboard_username())
        display_name = str(
            display_meta.get("display_name")
            or getattr(message_obj.sender, "nickname", None)
            or getattr(message_obj.sender, "user_id", None)
            or "rest-injector"
        )
        await self._ensure_webchat_platform_session(
            conversation_id=conversation_id,
            creator=creator,
            display_name=display_name,
        )

        result = event.get_result()
        if result is None:
            return

        message_parts = await self._message_event_result_to_storage_parts(result)
        if not message_parts:
            plain_text = ""
            if hasattr(result, "chain") and getattr(result, "chain", None) is not None:
                plain_text = result.chain.get_plain_text(with_other_comps_mark=True)
            if plain_text:
                message_parts = [{"type": "plain", "text": plain_text}]
        if not message_parts:
            return

        await self._save_webchat_history_message(
            conversation_id,
            sender_id="bot",
            sender_name="bot",
            message_parts=message_parts,
        )

    async def _inject_platform_message(
        self,
        payload: InjectMessageRequest,
        message_id: str,
        parts: list[dict[str, Any]],
    ) -> dict[str, Any]:
        target = self._resolve_injection_target(payload)
        display_result = await self._show_platform_injection_in_target_chat(
            payload,
            target,
            parts,
        )
        platform = self._resolve_platform_inst(target["platform_id"])
        handle_msg = getattr(platform, "handle_msg", None)
        if not callable(handle_msg):
            raise GatewayError(
                f"platform does not support synthetic inbound injection: {target['platform_id']}",
                status_code=501,
            )

        message_chain = build_message_chain(None, parts)
        abm = AstrBotMessage()
        abm.type = target["message_type"]
        abm.self_id = (
            payload.self_id
            or getattr(platform, "client_self_id", None)
            or target["platform_id"]
        )
        abm.session_id = target["session_id"]
        abm.message_id = message_id
        abm.sender = MessageMember(
            user_id=payload.sender_id,
            nickname=payload.display_name or payload.sender_id,
        )
        abm.message = list(message_chain.chain)
        abm.message_str = message_chain.get_plain_text(
            with_other_comps_mark=True
        ).strip()
        abm.raw_message = {
            "type": "rest_synthetic_inbound",
            "source": "rest_api",
            "platform_id": target["platform_id"],
            "message_type": target["message_type"].value,
            "session_id": target["session_id"],
            "group_id": target["group_id"],
            "sender_id": payload.sender_id,
            "sender_name": payload.display_name or payload.sender_id,
            "message": payload.message,
            "message_chain": parts,
            "selected_provider": payload.selected_provider,
            "selected_model": payload.selected_model,
            "enable_streaming": payload.enable_streaming,
            "action_type": payload.action_type,
            "creator": payload.creator or self._dashboard_username(),
            "_rest_display": {
                "enabled": bool(
                    self._should_show_in_chat(payload)
                    and target["platform_id"] == "webchat"
                ),
                "conversation_id": (
                    display_result["conversation_id"]
                    if display_result and display_result.get("mode") == "webchat_history"
                    else None
                ),
                "creator": (
                    display_result["creator"]
                    if display_result and display_result.get("mode") == "webchat_history"
                    else None
                ),
                "display_name": (
                    display_result["display_name"]
                    if display_result and display_result.get("mode") == "webchat_history"
                    else None
                ),
            },
        }
        if target["group_id"]:
            abm.group = Group(group_id=str(target["group_id"]))

        await handle_msg(abm)
        return {
            "platform_id": target["platform_id"],
            "message_id": message_id,
            "session_id": target["session_id"],
            "group_id": target["group_id"],
            "message_type": target["message_type"].value,
            "unified_msg_origin": target["unified_msg_origin"],
            "queued": True,
            "dispatch_method": "platform.handle_msg",
            "entered_pipeline": True,
            "show_in_chat": self._should_show_in_chat(payload),
            "show_in_webui": bool(payload.show_in_webui),
            "display_mode": display_result["mode"] if display_result else None,
            "display_sent": display_result["sent"] if display_result else False,
            "display_session": display_result.get("session") if display_result else None,
            "display_conversation_id": (
                display_result["conversation_id"]
                if display_result and display_result.get("mode") == "webchat_history"
                else None
            ),
        }

    async def _ensure_webchat_platform_session(
        self,
        conversation_id: str,
        creator: str,
        display_name: str | None,
    ) -> dict[str, Any]:
        session = await self.db.get_platform_session_by_id(conversation_id)
        if session is None:
            created = await self.db.create_platform_session(
                creator=creator,
                platform_id="webchat",
                session_id=conversation_id,
                display_name=display_name,
                is_group=0,
            )
            return {
                "created": True,
                "creator": created.creator,
                "display_name": created.display_name,
            }

        if session.platform_id != "webchat":
            raise GatewayError(
                f"session_id already exists on another platform: {conversation_id}",
                status_code=409,
            )
        if session.creator != creator:
            raise GatewayError(
                (
                    "session_id already belongs to another dashboard user: "
                    f"{conversation_id}"
                ),
                status_code=409,
            )

        await self.db.update_platform_session(
            session_id=conversation_id,
            display_name=display_name
            if display_name is not None
            else session.display_name,
        )
        return {
            "created": False,
            "creator": session.creator,
            "display_name": display_name
            if display_name is not None
            else session.display_name,
        }

    def _guess_attachment_mime(
        self,
        filename: str,
        attach_type: str,
        file_path: Path | None = None,
    ) -> str:
        if file_path and file_path.exists():
            try:
                with file_path.open("rb") as file:
                    signature = file.read(16)
                if signature.startswith(b"\x89PNG\r\n\x1a\n"):
                    return "image/png"
                if signature.startswith(b"\xff\xd8\xff"):
                    return "image/jpeg"
                if signature.startswith(b"GIF87a") or signature.startswith(b"GIF89a"):
                    return "image/gif"
                if signature.startswith(b"RIFF") and b"WEBP" in signature:
                    return "image/webp"
                if signature.startswith(b"RIFF") and b"WAVE" in signature:
                    return "audio/wav"
                if signature.startswith(b"ID3"):
                    return "audio/mpeg"
            except Exception:
                logger.debug("Failed to sniff attachment mime type.", exc_info=True)

        mime_type, _ = mimetypes.guess_type(filename)
        if mime_type:
            return mime_type
        if attach_type == "image":
            return "image/jpeg"
        if attach_type == "record":
            return "audio/wav"
        if attach_type == "video":
            return "video/mp4"
        return "application/octet-stream"

    def _build_file_service_url(self, token: str | None) -> str | None:
        if not token:
            return None
        callback_api_base = str(
            self.acm.default_conf.get("callback_api_base") or ""
        ).rstrip("/")
        if callback_api_base:
            return f"{callback_api_base}/api/file/{token}"
        return f"/api/file/{token}"

    def _gateway_attachment_path(self, attachment_id: str) -> str:
        return f"/attachments/{attachment_id}/download"

    def _gateway_attachment_url(self, request: Request, attachment_id: str) -> str:
        base_url = str(request.base_url).rstrip("/")
        return f"{base_url}{self._gateway_attachment_path(attachment_id)}"

    def _resolve_webchat_attachment_file(
        self,
        payload_text: str,
        attach_type: str,
    ) -> Path | None:
        prefix_map = {
            "image": "[IMAGE]",
            "record": "[RECORD]",
            "file": "[FILE]",
            "video": "[VIDEO]",
        }
        prefix = prefix_map[attach_type]
        filename = payload_text.removeprefix(prefix).strip()
        if not filename:
            return None

        basename = os.path.basename(filename)
        candidate_paths = [
            Path(get_astrbot_data_path()) / "attachments" / basename,
            Path(get_astrbot_data_path()) / "webchat" / "imgs" / basename,
        ]
        for file_path in candidate_paths:
            if file_path.exists() and file_path.is_file():
                return file_path
        logger.warning("Webchat attachment missing: %s", basename)
        return None

    async def _create_webchat_attachment_part(
        self,
        payload_text: str,
        attach_type: str,
        *,
        include_base64: bool = False,
        base64_max_bytes: int = 2 * 1024 * 1024,
    ) -> dict[str, Any] | None:
        file_path = self._resolve_webchat_attachment_file(payload_text, attach_type)
        if file_path is None:
            return None

        mime_type = self._guess_attachment_mime(
            file_path.name,
            attach_type,
            file_path=file_path,
        )
        attachment = await self.db.insert_attachment(
            path=str(file_path),
            type=attach_type,
            mime_type=mime_type,
        )
        if attachment is None:
            return None

        file_token = None
        try:
            file_token = await file_token_service.register_file(
                str(file_path), timeout=3600
            )
        except Exception:
            logger.warning(
                "Failed to register emitted attachment file token.", exc_info=True
            )

        size = file_path.stat().st_size
        part = {
            "type": attach_type,
            "attachment_id": attachment.attachment_id,
            "filename": file_path.name,
            "mime_type": mime_type,
            "size": size,
            "file_token": file_token,
            "url": self._build_file_service_url(file_token),
            "path": str(file_path),
            "download_path": self._gateway_attachment_path(attachment.attachment_id),
        }
        if include_base64:
            if size <= base64_max_bytes:
                part["base64"] = base64.b64encode(file_path.read_bytes()).decode(
                    "ascii"
                )
            else:
                part["base64_skipped"] = True
                part["base64_skip_reason"] = (
                    f"attachment too large for inline base64: {size} > {base64_max_bytes}"
                )
        return part

    async def _save_webchat_history_message(
        self,
        conversation_id: str,
        *,
        sender_id: str,
        sender_name: str,
        message_parts: list[dict[str, Any]],
        reasoning: str = "",
    ) -> None:
        if not message_parts and not reasoning:
            return

        payload: dict[str, Any] = {
            "type": "bot" if sender_id == "bot" else "user",
            "message": message_parts,
        }
        if reasoning:
            payload["reasoning"] = reasoning

        await self.message_history_manager.insert(
            platform_id="webchat",
            user_id=conversation_id,
            content=payload,
            sender_id=sender_id,
            sender_name=sender_name,
        )
        await self.db.update_platform_session(session_id=conversation_id)

    def _tool_invoke_message_parts(
        self,
        tool_name: str,
        payload: ToolInvokeRequest,
    ) -> list[dict[str, Any]]:
        parts = to_plain_message_parts(payload.message, payload.message_chain)
        if parts:
            return parts
        return [{"type": "plain", "text": f"[tool invoke] {tool_name}"}]

    def _build_tool_invoke_event(
        self,
        tool_name: str,
        payload: ToolInvokeRequest,
        *,
        conversation_id: str,
        message_id: str,
        display_name: str,
    ) -> tuple[WebChatMessageEvent, list[dict[str, Any]]]:
        parts = self._tool_invoke_message_parts(tool_name, payload)
        message_chain = build_message_chain(None, parts)
        session_id = f"webchat!{payload.sender_id}!{conversation_id}"
        message_obj = AstrBotMessage()
        message_obj.type = MessageType.FRIEND_MESSAGE
        message_obj.self_id = "astrbot"
        message_obj.session_id = session_id
        message_obj.message_id = message_id
        message_obj.sender = MessageMember(
            user_id=payload.sender_id,
            nickname=display_name,
        )
        message_obj.message = list(message_chain.chain)
        message_obj.message_str = (
            message_chain.get_plain_text(with_other_comps_mark=True)
            or f"[tool invoke] {tool_name}"
        )
        message_obj.raw_message = {
            "type": "rest_tool_invoke",
            "tool_name": tool_name,
            "arguments": serialize_value(payload.arguments),
        }
        event = WebChatMessageEvent(
            message_obj.message_str,
            message_obj,
            PlatformMetadata(
                name="webchat",
                description="webchat",
                id="webchat",
                support_proactive_message=True,
            ),
            session_id,
        )
        return event, parts

    def _debug_string_value(self, value: str) -> dict[str, Any]:
        escaped = value.encode("unicode_escape").decode("ascii")
        preview_limit = 256
        preview = escaped[:preview_limit]
        return {
            "length": len(value),
            "sha256": hashlib.sha256(value.encode("utf-8")).hexdigest(),
            "unicode_escape": preview,
            "truncated": len(escaped) > preview_limit,
        }

    def _build_argument_debug(
        self,
        value: Any,
        *,
        path: str = "arguments",
    ) -> list[dict[str, Any]]:
        entries: list[dict[str, Any]] = []
        if isinstance(value, str):
            entry = {"path": path}
            entry.update(self._debug_string_value(value))
            entries.append(entry)
            return entries
        if isinstance(value, dict):
            for key, child in value.items():
                child_path = f"{path}.{key}"
                entries.extend(self._build_argument_debug(child, path=child_path))
            return entries
        if isinstance(value, list):
            for index, child in enumerate(value):
                entries.extend(
                    self._build_argument_debug(child, path=f"{path}[{index}]")
                )
        return entries

    def _normalize_tool_result_content_item(self, item: Any) -> dict[str, Any]:
        raw = serialize_value(item)
        item_type = None
        text = None
        if isinstance(raw, dict):
            item_type = raw.get("type")
            text = raw.get("text")
        if item_type is None:
            item_type = getattr(item, "type", type(item).__name__)
        if text is None:
            text = getattr(item, "text", None)

        normalized = {
            "type": item_type,
            "raw": raw,
        }
        if text is not None:
            normalized["text"] = str(text)
        return normalized

    def _normalize_call_tool_result(self, result: Any) -> dict[str, Any]:
        content = []
        text_parts: list[str] = []
        for item in getattr(result, "content", []) or []:
            normalized = self._normalize_tool_result_content_item(item)
            content.append(normalized)
            text = normalized.get("text")
            if isinstance(text, str) and text:
                text_parts.append(text)

        structured_content = getattr(result, "structuredContent", None)
        if structured_content is None:
            structured_content = getattr(result, "structured_content", None)
        is_error = getattr(result, "isError", None)
        if is_error is None:
            is_error = getattr(result, "is_error", False)

        return {
            "content": content,
            "text": "\n".join(text_parts) or None,
            "structured_content": serialize_value(structured_content),
            "is_error": bool(is_error),
            "raw": serialize_value(result),
        }

    def _message_parts_from_tool_results(
        self,
        results: list[dict[str, Any]],
    ) -> list[dict[str, Any]]:
        texts = [
            item.get("text") for item in results if isinstance(item.get("text"), str)
        ]
        merged = "\n".join(text for text in texts if text)
        if not merged:
            return []
        return [{"type": "plain", "text": merged}]

    def _history_safe_message_parts(
        self,
        message_parts: list[dict[str, Any]],
    ) -> list[dict[str, Any]]:
        safe_parts: list[dict[str, Any]] = []
        for part in message_parts:
            if not isinstance(part, dict):
                continue
            item = dict(part)
            item.pop("base64", None)
            safe_parts.append(item)
        return safe_parts

    def _decorate_attachment_links(
        self,
        request: Request,
        message_parts: list[dict[str, Any]],
    ) -> list[dict[str, Any]]:
        decorated: list[dict[str, Any]] = []
        for part in message_parts:
            if not isinstance(part, dict):
                continue
            item = dict(part)
            attachment_id = item.get("attachment_id")
            if isinstance(attachment_id, str) and attachment_id:
                item["download_path"] = self._gateway_attachment_path(attachment_id)
                item["download_url"] = self._gateway_attachment_url(
                    request, attachment_id
                )
            decorated.append(item)
        return decorated

    async def _summarize_tool_emitted_events(
        self,
        events: list[dict[str, Any]],
        *,
        include_base64: bool = False,
        base64_max_bytes: int = 2 * 1024 * 1024,
    ) -> dict[str, Any]:
        message_parts: list[dict[str, Any]] = []
        reasoning = ""
        completed = False
        final_text = ""

        for event in events:
            event_type = str(event.get("type") or "")
            data = event.get("data")
            if event_type == "plain" and isinstance(data, str):
                message_parts.append({"type": "plain", "text": data})
            elif event_type in {"image", "record", "file", "video"} and isinstance(
                data, str
            ):
                part = await self._create_webchat_attachment_part(
                    data,
                    event_type,
                    include_base64=include_base64,
                    base64_max_bytes=base64_max_bytes,
                )
                if part:
                    message_parts.append(part)
                else:
                    message_parts.append({"type": event_type, "data": data})
            elif event_type == "audio_chunk":
                part = {"type": "audio_chunk", "data": data}
                if event.get("text") is not None:
                    part["text"] = event.get("text")
                message_parts.append(part)
            elif event_type == "complete":
                completed = True
                if isinstance(event.get("data"), str) and event.get("data"):
                    final_text = event["data"]
                if isinstance(event.get("reasoning"), str) and event.get("reasoning"):
                    reasoning = event["reasoning"]
            elif event_type == "end":
                completed = True

        if not final_text:
            final_text = "".join(
                part["text"]
                for part in message_parts
                if part.get("type") == "plain" and isinstance(part.get("text"), str)
            )

        return {
            "events": events,
            "message_parts": message_parts,
            "text": final_text or None,
            "reasoning": reasoning or None,
            "completed": completed,
        }

    async def _collect_tool_emitted_events(
        self,
        request_id: str,
        conversation_id: str,
        timeout_seconds: float,
        *,
        include_base64: bool = False,
        base64_max_bytes: int = 2 * 1024 * 1024,
    ) -> dict[str, Any]:
        back_queue = webchat_queue_mgr.get_or_create_back_queue(
            request_id,
            conversation_id,
        )
        loop = asyncio.get_running_loop()
        deadline = loop.time() + max(0.1, timeout_seconds)
        events: list[dict[str, Any]] = []

        while True:
            remaining = deadline - loop.time()
            if remaining <= 0:
                break
            timeout = remaining if not events else min(0.2, remaining)
            try:
                item = await asyncio.wait_for(back_queue.get(), timeout=timeout)
            except asyncio.TimeoutError:
                break
            events.append(serialize_value(item))
            if isinstance(item, dict) and item.get("type") in {"complete", "end"}:
                deadline = min(deadline, loop.time() + 0.05)

        return await self._summarize_tool_emitted_events(
            events,
            include_base64=include_base64,
            base64_max_bytes=base64_max_bytes,
        )

    @asynccontextmanager
    async def _temporary_tool_umop_route(self, umo: str, conf_id: str | None):
        if not conf_id:
            yield
            return

        self._resolve_abconf(conf_id)
        router = getattr(self.acm, "ucr", None)
        if router is None:
            raise GatewayError("umop config router unavailable", status_code=500)

        previous_conf_id = router.umop_to_conf_id.get(umo)
        await router.update_route(umo, conf_id)
        try:
            yield
        finally:
            if previous_conf_id is None:
                await router.delete_route(umo)
            else:
                await router.update_route(umo, previous_conf_id)

    async def _drain_injected_webchat_back_queue(
        self,
        conversation_id: str,
        message_id: str,
        timeout_seconds: float,
    ) -> None:
        back_queue = webchat_queue_mgr.get_or_create_back_queue(conversation_id)
        deadline = asyncio.get_running_loop().time() + timeout_seconds
        buffered_foreign_messages: list[dict[str, Any]] = []
        accumulated_parts: list[dict[str, Any]] = []
        accumulated_text = ""
        accumulated_reasoning = ""

        async def flush_current_message() -> None:
            nonlocal accumulated_parts, accumulated_text, accumulated_reasoning
            parts = list(accumulated_parts)
            if accumulated_text:
                parts.append({"type": "plain", "text": accumulated_text})
            await self._save_webchat_history_message(
                conversation_id,
                sender_id="bot",
                sender_name="bot",
                message_parts=parts,
                reasoning=accumulated_reasoning,
            )
            accumulated_parts = []
            accumulated_text = ""
            accumulated_reasoning = ""

        try:
            while True:
                remaining = deadline - asyncio.get_running_loop().time()
                if remaining <= 0:
                    logger.warning(
                        "Timed out while persisting injected webchat response: %s",
                        message_id,
                    )
                    break

                try:
                    result = await asyncio.wait_for(back_queue.get(), timeout=remaining)
                except asyncio.TimeoutError:
                    logger.warning(
                        "Timed out while waiting for injected webchat response: %s",
                        message_id,
                    )
                    break

                result_message_id = str(result.get("message_id") or "")
                if result_message_id != message_id:
                    buffered_foreign_messages.append(result)
                    continue

                msg_type = str(result.get("type") or "")
                payload_text = str(result.get("data") or "")
                streaming = bool(result.get("streaming", False))

                if msg_type == "plain":
                    chain_type = str(result.get("chain_type") or "")
                    if chain_type == "reasoning":
                        accumulated_reasoning += payload_text
                    elif streaming:
                        accumulated_text += payload_text
                    else:
                        accumulated_text = payload_text
                elif msg_type in {"image", "record", "file", "video"}:
                    part = await self._create_webchat_attachment_part(
                        payload_text, msg_type
                    )
                    if part:
                        accumulated_parts.append(part)
                elif msg_type == "complete":
                    if not accumulated_text:
                        accumulated_text = payload_text
                    reasoning = result.get("reasoning")
                    if reasoning and not accumulated_reasoning:
                        accumulated_reasoning = str(reasoning)
                    await flush_current_message()
                    break
                elif msg_type == "end":
                    await flush_current_message()
                    break

                if not streaming and msg_type in {
                    "plain",
                    "image",
                    "record",
                    "file",
                    "video",
                }:
                    await flush_current_message()
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.warning(
                "Failed to persist injected webchat back-queue response.",
                exc_info=True,
            )
        finally:
            for item in buffered_foreign_messages:
                await back_queue.put(item)

    async def download_attachment(self, request: Request, payload: None = None):
        attachment_id = request.path_params["attachment_id"]
        attachment = await self.db.get_attachment_by_id(attachment_id)
        if not attachment:
            raise GatewayError(
                f"attachment not found: {attachment_id}", status_code=404
            )

        file_path = Path(attachment.path)
        if not file_path.exists() or not file_path.is_file():
            raise GatewayError(
                f"attachment file missing: {attachment_id}", status_code=404
            )

        return FileResponse(
            path=str(file_path),
            media_type=attachment.mime_type or "application/octet-stream",
            filename=file_path.name,
        )

    async def health(self, request: Request, payload: None = None) -> dict[str, Any]:
        return {"status": "ok", "time": now_iso()}

    async def meta(self, request: Request, payload: None = None) -> dict[str, Any]:
        return {
            "plugin": {
                "name": self.plugin.name,
                "host": str(getattr(self.plugin.config, "host", "127.0.0.1")),
                "port": int(getattr(self.plugin.config, "port", 6324)),
            },
            "summary": {
                "platforms": len(self.platform_manager.get_insts()),
                "plugins": len(self.context.get_all_stars()),
                "providers": len(self.provider_manager.inst_map),
                "tools": len(self.provider_manager.llm_tools.func_list),
                "subagents": len(getattr(self.subagent_orchestrator, "handoffs", [])),
                "recent_sessions": len(self.plugin._recent_sessions),
            },
        }

    async def list_platforms(
        self, request: Request, payload: None = None
    ) -> list[dict[str, Any]]:
        return [
            self._serialize_platform(platform)
            for platform in self.platform_manager.get_insts()
        ]

    async def get_platform(
        self, request: Request, payload: None = None
    ) -> dict[str, Any]:
        platform = self._resolve_platform_inst(request.path_params["platform_id"])
        return self._serialize_platform(platform)

    async def list_platform_types(
        self, request: Request, payload: None = None
    ) -> list[dict[str, Any]]:
        return [self._serialize_platform_type(meta) for meta in platform_registry]

    async def platform_stats(
        self, request: Request, payload: None = None
    ) -> dict[str, Any]:
        return serialize_value(self.platform_manager.get_all_stats())

    async def platform_stats_history(
        self, request: Request, payload: None = None
    ) -> list[dict[str, Any]]:
        offset_sec = self._query_int(request, "offset_sec", 86400)
        rows = await self.db.get_platform_stats(offset_sec=offset_sec)
        return serialize_value(rows)

    async def create_platform(
        self, request: Request, payload: PlatformConfigRequest
    ) -> dict[str, Any]:
        self._ensure_mutation_allowed()
        config = ensure_mapping(
            copy.deepcopy(payload.config), "platform config must be an object"
        )
        platform_id = str(config.get("id", "")).strip()
        if not platform_id:
            raise GatewayError("platform config must include id")
        for existing in self.acm.default_conf.get("platform", []):
            if existing.get("id") == platform_id:
                raise GatewayError(f"platform already exists: {platform_id}")
        self.acm.default_conf["platform"].append(config)
        self.acm.default_conf.save_config()
        if config.get("enable", True):
            await self.platform_manager.load_platform(config)
        return {"platform_id": platform_id}

    async def update_platform(
        self, request: Request, payload: PlatformConfigRequest
    ) -> dict[str, Any]:
        self._ensure_mutation_allowed()
        platform_id = request.path_params["platform_id"]
        config = ensure_mapping(
            copy.deepcopy(payload.config), "platform config must be an object"
        )
        if str(config.get("id", platform_id)) != platform_id:
            raise GatewayError("changing platform id is not supported")
        config["id"] = platform_id
        for index, existing in enumerate(self.acm.default_conf.get("platform", [])):
            if existing.get("id") == platform_id:
                self.acm.default_conf["platform"][index] = config
                self.acm.default_conf.save_config()
                await self.platform_manager.reload(config)
                return {"platform_id": platform_id}
        raise GatewayError(f"platform not found: {platform_id}", status_code=404)

    async def delete_platform(
        self, request: Request, payload: None = None
    ) -> dict[str, Any]:
        self._ensure_mutation_allowed()
        platform_id = request.path_params["platform_id"]
        self._resolve_platform_config(platform_id)
        self.acm.default_conf["platform"] = [
            item
            for item in self.acm.default_conf.get("platform", [])
            if item.get("id") != platform_id
        ]
        self.acm.default_conf.save_config()
        await self.platform_manager.terminate_platform(platform_id)
        return {"platform_id": platform_id, "deleted": True}

    async def reload_platform(
        self, request: Request, payload: None = None
    ) -> dict[str, Any]:
        self._ensure_mutation_allowed()
        platform_id = request.path_params["platform_id"]
        config = self._resolve_platform_config(platform_id)
        await self.platform_manager.reload(config)
        return {"platform_id": platform_id, "reloaded": True}

    async def enable_platform(
        self, request: Request, payload: None = None
    ) -> dict[str, Any]:
        self._ensure_mutation_allowed()
        platform_id = request.path_params["platform_id"]
        config = self._resolve_platform_config(platform_id)
        config["enable"] = True
        self.acm.default_conf.save_config()
        await self.platform_manager.reload(config)
        return {"platform_id": platform_id, "enabled": True}

    async def disable_platform(
        self, request: Request, payload: None = None
    ) -> dict[str, Any]:
        self._ensure_mutation_allowed()
        platform_id = request.path_params["platform_id"]
        config = self._resolve_platform_config(platform_id)
        config["enable"] = False
        self.acm.default_conf.save_config()
        await self.platform_manager.terminate_platform(platform_id)
        return {"platform_id": platform_id, "enabled": False}

    async def list_plugins(
        self, request: Request, payload: None = None
    ) -> dict[str, Any]:
        items: list[dict[str, Any]] = []
        for star in self.context.get_all_stars():
            item = self._serialize_plugin(star)
            item["handlers"] = await self._plugin_handlers_info(
                getattr(star, "star_handler_full_names", [])
            )
            item.update(await self._plugin_logo_fields(star))
            items.append(item)
        return {
            "items": items,
            "failed_plugin_info": getattr(
                self.plugin_manager, "failed_plugin_info", ""
            ),
        }

    async def get_plugin(
        self, request: Request, payload: None = None
    ) -> dict[str, Any]:
        plugin = self._resolve_plugin(request.path_params["plugin_name"])
        data = self._serialize_plugin(plugin)
        data["handlers"] = await self._plugin_handlers_info(
            getattr(plugin, "star_handler_full_names", [])
        )
        data.update(await self._plugin_logo_fields(plugin))
        if getattr(plugin, "config", None) is not None:
            data["config"] = serialize_value(plugin.config)
        return data

    async def plugin_market_list(
        self, request: Request, payload: None = None
    ) -> dict[str, Any]:
        custom_registry = request.query_params.get("custom_registry")
        force_refresh = (
            request.query_params.get("force_refresh", "false").lower() == "true"
        )
        urls, cache_file, md5_url = self._plugin_market_cache_file(custom_registry)

        cached_payload = self._load_json_cache(cache_file)
        cached_data = cached_payload.get("data") if cached_payload else None
        if not force_refresh and await self._is_plugin_market_cache_valid(
            cache_file, md5_url
        ):
            if cached_data is not None:
                return {"items": cached_data, "cache": True}

        ssl_context = ssl.create_default_context(cafile=certifi.where())
        connector = aiohttp.TCPConnector(ssl=ssl_context)
        for url in urls:
            try:
                async with aiohttp.ClientSession(
                    trust_env=True, connector=connector
                ) as session:
                    async with session.get(url) as response:
                        if response.status != 200:
                            continue
                        try:
                            remote_data = await response.json()
                        except aiohttp.ContentTypeError:
                            remote_data = json.loads(await response.text())
                        if not remote_data:
                            continue
                        current_md5 = await self._fetch_remote_md5(md5_url)
                        self._save_json_cache(cache_file, remote_data, current_md5)
                        return {"items": remote_data, "cache": False}
            except Exception:
                logger.warning(
                    "Failed to fetch plugin market url %s", url, exc_info=True
                )

        if cached_data is not None:
            return {"items": cached_data, "cache": True}
        raise GatewayError(
            "failed to fetch plugin market and no cache is available", status_code=502
        )

    async def get_plugin_readme(
        self, request: Request, payload: None = None
    ) -> dict[str, Any]:
        plugin = self._resolve_plugin(request.path_params["plugin_name"])
        plugin_dir = self._plugin_dir(plugin)
        readme_path = plugin_dir / "README.md"
        if not readme_path.is_file():
            raise GatewayError(
                f"plugin {plugin.name} has no README.md", status_code=404
            )
        return {
            "plugin": plugin.name,
            "content": readme_path.read_text(encoding="utf-8"),
        }

    async def get_plugin_changelog(
        self, request: Request, payload: None = None
    ) -> dict[str, Any]:
        plugin = self._resolve_plugin(request.path_params["plugin_name"])
        plugin_dir = self._plugin_dir(plugin)
        for name in ("CHANGELOG.md", "changelog.md", "CHANGELOG", "changelog"):
            path = plugin_dir / name
            if path.is_file():
                return {
                    "plugin": plugin.name,
                    "content": path.read_text(encoding="utf-8"),
                }
        return {"plugin": plugin.name, "content": None}

    async def get_plugin_sources(
        self, request: Request, payload: None = None
    ) -> dict[str, Any]:
        return {"sources": await sp.global_get("custom_plugin_sources", [])}

    async def save_plugin_sources(
        self, request: Request, payload: PluginSourceSaveRequest
    ) -> dict[str, Any]:
        self._ensure_mutation_allowed()
        await sp.global_put("custom_plugin_sources", payload.sources)
        return {"sources": payload.sources}

    async def install_plugin_repo(
        self, request: Request, payload: PluginInstallRepoRequest
    ) -> dict[str, Any]:
        self._ensure_mutation_allowed()
        result = await self.plugin_manager.install_plugin(
            payload.repo_url, payload.proxy
        )
        return serialize_value(result)

    async def install_plugin_zip(
        self, request: Request, payload: PluginInstallZipRequest
    ) -> dict[str, Any]:
        self._ensure_mutation_allowed()
        result = await self.plugin_manager.install_plugin_from_file(
            payload.zip_file_path
        )
        return serialize_value(result)

    async def install_plugin_upload(
        self, request: Request, payload: dict[str, Any] | None = None
    ) -> dict[str, Any]:
        self._ensure_mutation_allowed()
        files = (payload or {}).get("files", [])
        if not files:
            raise GatewayError("missing uploaded file")
        file = files[0]
        filename = os.path.basename(getattr(file, "filename", "") or "plugin.zip")
        if not filename.lower().endswith(".zip"):
            raise GatewayError("only .zip files are supported")
        temp_dir = get_astrbot_temp_path()
        os.makedirs(temp_dir, exist_ok=True)
        fd, temp_path = tempfile.mkstemp(
            prefix="plugin-upload-", suffix=".zip", dir=temp_dir
        )
        os.close(fd)
        try:
            with open(temp_path, "wb") as stream:
                while True:
                    chunk = await file.read(1024 * 1024)
                    if not chunk:
                        break
                    stream.write(chunk)
            result = await self.plugin_manager.install_plugin_from_file(temp_path)
            return serialize_value(result)
        finally:
            try:
                os.remove(temp_path)
            except OSError:
                logger.warning("Failed to remove temporary uploaded plugin zip.")

    async def reload_plugins(
        self, request: Request, payload: None = None
    ) -> dict[str, Any]:
        self._ensure_mutation_allowed()
        success, error = await self.plugin_manager.reload(None)
        if not success:
            raise GatewayError(str(error or "plugin reload failed"), status_code=500)
        return {"plugin": None, "reloaded": True}

    async def reload_plugin(
        self, request: Request, payload: None = None
    ) -> dict[str, Any]:
        self._ensure_mutation_allowed()
        plugin = self._resolve_plugin(request.path_params["plugin_name"])
        success, error = await self.plugin_manager.reload(plugin.name)
        if not success:
            raise GatewayError(str(error or "plugin reload failed"), status_code=500)
        return {"plugin": plugin.name, "reloaded": True}

    async def enable_plugin(
        self, request: Request, payload: None = None
    ) -> dict[str, Any]:
        self._ensure_mutation_allowed()
        plugin = self._resolve_plugin(request.path_params["plugin_name"])
        await self.plugin_manager.turn_on_plugin(plugin.name)
        return {"plugin": plugin.name, "enabled": True}

    async def disable_plugin(
        self, request: Request, payload: None = None
    ) -> dict[str, Any]:
        self._ensure_mutation_allowed()
        plugin = self._resolve_plugin(request.path_params["plugin_name"])
        await self.plugin_manager.turn_off_plugin(plugin.name)
        return {"plugin": plugin.name, "enabled": False}

    async def update_plugin(
        self, request: Request, payload: PluginUpdateRequest
    ) -> dict[str, Any]:
        self._ensure_mutation_allowed()
        plugin = self._resolve_plugin(request.path_params["plugin_name"])
        await self.plugin_manager.update_plugin(plugin.name, payload.proxy)
        return {"plugin": plugin.name, "updated": True, "proxy": payload.proxy}

    async def update_all_plugins(
        self, request: Request, payload: PluginBulkUpdateRequest
    ) -> dict[str, Any]:
        self._ensure_mutation_allowed()
        if not payload.names:
            raise GatewayError("plugin list cannot be empty")
        results: list[dict[str, Any]] = []
        for name in payload.names:
            try:
                await self.plugin_manager.update_plugin(name, payload.proxy)
                results.append({"name": name, "status": "ok"})
            except Exception as exc:
                results.append({"name": name, "status": "error", "message": str(exc)})
        return {"results": results}

    async def uninstall_plugin(
        self, request: Request, payload: PluginUninstallRequest
    ) -> dict[str, Any]:
        self._ensure_mutation_allowed()
        plugin = self._resolve_plugin(request.path_params["plugin_name"])
        await self.plugin_manager.uninstall_plugin(
            plugin.name,
            delete_config=payload.delete_config,
            delete_data=payload.delete_data,
        )
        return {"plugin": plugin.name, "deleted": True}

    async def get_core_config(
        self, request: Request, payload: None = None
    ) -> dict[str, Any]:
        return self._snapshot_config(
            self.acm.default_conf, request.query_params.get("path")
        )

    async def patch_core_config(
        self, request: Request, payload: ConfigPatchRequest
    ) -> dict[str, Any]:
        self._ensure_mutation_allowed()
        return self._apply_config_patch(self.acm.default_conf, payload)

    async def list_abconfs(
        self, request: Request, payload: None = None
    ) -> list[dict[str, Any]]:
        return serialize_value(self.acm.get_conf_list())

    async def create_abconf(
        self, request: Request, payload: ConfigCreateRequest
    ) -> dict[str, Any]:
        self._ensure_mutation_allowed()
        if payload.data is None:
            conf_id = self.acm.create_conf(name=payload.name)
        else:
            conf_id = self.acm.create_conf(config=payload.data, name=payload.name)
        for info in self.acm.get_conf_list():
            if info["id"] == conf_id:
                return serialize_value(info)
        return {"id": conf_id}

    async def get_abconf(
        self, request: Request, payload: None = None
    ) -> dict[str, Any]:
        conf = self._resolve_abconf(request.path_params["conf_id"])
        return self._snapshot_config(conf, request.query_params.get("path"))

    async def patch_abconf(
        self, request: Request, payload: ConfigPatchRequest
    ) -> dict[str, Any]:
        self._ensure_mutation_allowed()
        conf = self._resolve_abconf(request.path_params["conf_id"])
        return self._apply_config_patch(conf, payload)

    async def rename_abconf(
        self, request: Request, payload: RenameRequest
    ) -> dict[str, Any]:
        self._ensure_mutation_allowed()
        conf_id = request.path_params["conf_id"]
        ok = self.acm.update_conf_info(conf_id, name=payload.name)
        if not ok:
            raise GatewayError(f"abconf not found: {conf_id}", status_code=404)
        return {"id": conf_id, "name": payload.name}

    async def delete_abconf(
        self, request: Request, payload: None = None
    ) -> dict[str, Any]:
        self._ensure_mutation_allowed()
        conf_id = request.path_params["conf_id"]
        ok = self.acm.delete_conf(conf_id)
        if not ok:
            raise GatewayError(f"abconf not found: {conf_id}", status_code=404)
        return {"id": conf_id, "deleted": True}

    async def list_plugin_configs(
        self, request: Request, payload: None = None
    ) -> list[dict[str, Any]]:
        items: list[dict[str, Any]] = []
        for star in self.context.get_all_stars():
            items.append(
                {
                    "plugin": star.name,
                    "root_dir_name": star.root_dir_name,
                    "has_config": getattr(star, "config", None) is not None,
                }
            )
        return items

    async def get_plugin_config(
        self, request: Request, payload: None = None
    ) -> dict[str, Any]:
        plugin = self._resolve_plugin(request.path_params["plugin_name"])
        if getattr(plugin, "config", None) is None:
            raise GatewayError(f"plugin has no config: {plugin.name}", status_code=404)
        data = self._snapshot_config(plugin.config, request.query_params.get("path"))
        data["schema"] = serialize_value(getattr(plugin.config, "schema", None))
        data["plugin"] = plugin.name
        return data

    async def patch_plugin_config(
        self, request: Request, payload: ConfigPatchRequest
    ) -> dict[str, Any]:
        self._ensure_mutation_allowed()
        plugin = self._resolve_plugin(request.path_params["plugin_name"])
        if getattr(plugin, "config", None) is None:
            raise GatewayError(f"plugin has no config: {plugin.name}", status_code=404)
        snapshot = copy.deepcopy(dict(plugin.config))
        set_by_dotpath(
            snapshot, payload.path, payload.value, create_missing=payload.create_missing
        )
        await self._save_plugin_config_and_reload(plugin, snapshot)
        result = {
            "path": payload.path,
            "value": serialize_value(get_by_dotpath(snapshot, payload.path)),
        }
        result["plugin"] = plugin.name
        return result

    async def replace_plugin_config(
        self, request: Request, payload: PluginConfigReplaceRequest
    ) -> dict[str, Any]:
        self._ensure_mutation_allowed()
        plugin = self._resolve_plugin(request.path_params["plugin_name"])
        if getattr(plugin, "config", None) is None:
            raise GatewayError(f"plugin has no config: {plugin.name}", status_code=404)
        await self._save_plugin_config_and_reload(plugin, payload.config)
        return {
            "plugin": plugin.name,
            "config": serialize_value(payload.config),
            "reloaded": True,
        }

    async def list_plugin_config_files(
        self, request: Request, payload: None = None
    ) -> dict[str, Any]:
        plugin_name = request.path_params["plugin_name"]
        key_path = self._require_query(request, "key")
        plugin, _meta, plugin_root_path = self._config_file_context(
            plugin_name, key_path
        )
        folder = config_key_to_folder(key_path)
        target_dir = (plugin_root_path / "files" / folder).resolve(strict=False)
        files: list[str] = []
        if target_dir.exists() and target_dir.is_dir():
            for path in target_dir.rglob("*"):
                if path.is_file():
                    try:
                        files.append(path.relative_to(plugin_root_path).as_posix())
                    except ValueError:
                        continue
        return {"plugin": plugin.name, "key": key_path, "files": sorted(files)}

    async def upload_plugin_config_files(
        self, request: Request, payload: dict[str, Any] | None = None
    ) -> dict[str, Any]:
        self._ensure_mutation_allowed()
        plugin_name = request.path_params["plugin_name"]
        key_path = request.query_params.get("key") or (payload or {}).get(
            "form", {}
        ).get("key")
        if not isinstance(key_path, str) or not key_path:
            raise GatewayError("missing query parameter: key")
        plugin, meta, plugin_root_path = self._config_file_context(
            plugin_name, key_path
        )
        files = (payload or {}).get("files", [])
        if not files:
            raise GatewayError("no files uploaded")
        allowed_exts = [
            str(ext).lstrip(".").lower()
            for ext in meta.get("file_types", [])
            if str(ext).strip()
        ]
        uploaded: list[str] = []
        errors: list[str] = []
        folder = config_key_to_folder(key_path)
        for file in files:
            filename = sanitize_filename(getattr(file, "filename", "") or "")
            if not filename:
                errors.append("invalid filename")
                continue
            ext = os.path.splitext(filename)[1].lstrip(".").lower()
            if allowed_exts and ext not in allowed_exts:
                errors.append(f"unsupported file type: {filename}")
                continue
            rel_path = f"files/{folder}/{filename}"
            save_path = (plugin_root_path / rel_path).resolve(strict=False)
            try:
                save_path.relative_to(plugin_root_path)
            except ValueError:
                errors.append(f"invalid path: {filename}")
                continue
            save_path.parent.mkdir(parents=True, exist_ok=True)
            size = 0
            with open(save_path, "wb") as stream:
                while True:
                    chunk = await file.read(1024 * 1024)
                    if not chunk:
                        break
                    size += len(chunk)
                    if size > MAX_CONFIG_FILE_BYTES:
                        break
                    stream.write(chunk)
            if size > MAX_CONFIG_FILE_BYTES:
                try:
                    save_path.unlink()
                except OSError:
                    pass
                errors.append(f"file too large: {filename}")
                continue
            uploaded.append(rel_path)
        return {
            "plugin": plugin.name,
            "key": key_path,
            "uploaded": uploaded,
            "errors": errors,
        }

    async def delete_plugin_config_file(
        self, request: Request, payload: ConfigFileDeleteRequest
    ) -> dict[str, Any]:
        self._ensure_mutation_allowed()
        plugin_name = request.path_params["plugin_name"]
        key_path = self._require_query(request, "key")
        plugin, _meta, plugin_root_path = self._config_file_context(
            plugin_name, key_path
        )
        rel_path = normalize_rel_path(payload.path)
        if not rel_path or not rel_path.startswith("files/"):
            raise GatewayError("invalid path parameter")
        target_path = (plugin_root_path / rel_path).resolve(strict=False)
        try:
            target_path.relative_to(plugin_root_path)
        except ValueError as exc:
            raise GatewayError("invalid path parameter") from exc
        if target_path.is_file():
            target_path.unlink()
        return {"plugin": plugin.name, "path": rel_path, "deleted": True}

    async def list_providers(
        self, request: Request, payload: None = None
    ) -> list[dict[str, Any]]:
        items: list[dict[str, Any]] = []
        for provider_id, provider in self.provider_manager.inst_map.items():
            item = self._serialize_provider(provider)
            item["provider_id"] = provider_id
            items.append(item)
        return items

    async def get_provider(
        self, request: Request, payload: None = None
    ) -> dict[str, Any]:
        provider_id = request.path_params["provider_id"]
        provider = await self.provider_manager.get_provider_by_id(provider_id)
        if not provider:
            raise GatewayError(f"provider not found: {provider_id}", status_code=404)
        item = self._serialize_provider(provider)
        item["provider_id"] = provider_id
        return item

    async def list_provider_types(
        self, request: Request, payload: None = None
    ) -> list[dict[str, Any]]:
        return [self._serialize_provider_type(meta) for meta in provider_registry]

    async def get_current_providers(
        self, request: Request, payload: None = None
    ) -> dict[str, Any]:
        umo = request.query_params.get("umo")
        chat = self.provider_manager.get_using_provider(
            ProviderType.CHAT_COMPLETION, umo
        )
        stt = self.provider_manager.get_using_provider(ProviderType.SPEECH_TO_TEXT, umo)
        tts = self.provider_manager.get_using_provider(ProviderType.TEXT_TO_SPEECH, umo)
        return {
            "umo": umo,
            "chat": self._serialize_provider(chat) if chat else None,
            "stt": self._serialize_provider(stt) if stt else None,
            "tts": self._serialize_provider(tts) if tts else None,
        }

    async def set_current_provider(
        self, request: Request, payload: ProviderSelectRequest
    ) -> dict[str, Any]:
        self._ensure_mutation_allowed()
        provider_type = self._provider_type_from_string(payload.provider_type)
        await self.provider_manager.set_provider(
            provider_id=payload.provider_id,
            provider_type=provider_type,
            umo=payload.umo,
        )
        return {
            "provider_id": payload.provider_id,
            "provider_type": provider_type.value,
            "umo": payload.umo,
        }

    async def create_provider(
        self, request: Request, payload: ProviderConfigRequest
    ) -> dict[str, Any]:
        self._ensure_mutation_allowed()
        config = ensure_mapping(
            copy.deepcopy(payload.config), "provider config must be an object"
        )
        await self.provider_manager.create_provider(config)
        return {"provider_id": config.get("id")}

    async def update_provider(
        self, request: Request, payload: ProviderConfigRequest
    ) -> dict[str, Any]:
        self._ensure_mutation_allowed()
        provider_id = request.path_params["provider_id"]
        config = ensure_mapping(
            copy.deepcopy(payload.config), "provider config must be an object"
        )
        config["id"] = provider_id
        await self.provider_manager.update_provider(provider_id, config)
        return {"provider_id": provider_id}

    async def delete_provider(
        self, request: Request, payload: None = None
    ) -> dict[str, Any]:
        self._ensure_mutation_allowed()
        provider_id = request.path_params["provider_id"]
        await self.provider_manager.delete_provider(provider_id=provider_id)
        return {"provider_id": provider_id, "deleted": True}

    async def reload_provider(
        self, request: Request, payload: None = None
    ) -> dict[str, Any]:
        self._ensure_mutation_allowed()
        provider_id = request.path_params["provider_id"]
        config = self._resolve_provider_config(provider_id)
        await self.provider_manager.reload(config)
        return {"provider_id": provider_id, "reloaded": True}

    async def get_provider_models(
        self, request: Request, payload: None = None
    ) -> dict[str, Any]:
        provider_id = request.path_params["provider_id"]
        provider = await self.provider_manager.get_provider_by_id(provider_id)
        if not provider or not isinstance(provider, Provider):
            raise GatewayError(
                f"chat provider not found: {provider_id}", status_code=404
            )
        models = await provider.get_models()
        return {"provider_id": provider_id, "models": serialize_value(models)}

    async def recent_sessions(
        self, request: Request, payload: None = None
    ) -> list[dict[str, Any]]:
        limit = max(1, min(self._query_int(request, "limit", 20), 200))
        keys = list(self.plugin._recent_sessions_order)[:limit]
        return [
            serialize_value(self.plugin._recent_sessions[key])
            for key in keys
            if key in self.plugin._recent_sessions
        ]

    def _get_log_broker(self) -> Any:
        for handler in logger.handlers:
            if isinstance(handler, LogQueueHandler):
                return handler.log_broker
        return None

    def _publish_event_log(self, event_id: str, action: str, **fields: Any) -> None:
        entry = {
            "type": "gateway_event",
            "level": "INFO",
            "time": dt.datetime.now(dt.timezone.utc).timestamp(),
            "event_id": event_id,
            "action": action,
            "fields": serialize_value(fields),
            "data": f"gateway_event event_id={event_id} action={action}",
        }
        broker = self._get_log_broker()
        if broker is not None:
            broker.publish(entry)
        else:
            logger.info(entry["data"])

    def _log_entry_matches_event_id(self, log_entry: dict[str, Any], event_id: str) -> bool:
        needle = str(event_id or "").strip()
        if not needle:
            return False
        data = log_entry.get("data")
        if isinstance(data, str) and needle in data:
            return True
        try:
            return needle in json.dumps(log_entry, ensure_ascii=False, default=str)
        except Exception:
            return False

    def _filter_event_logs(
        self,
        entries: list[dict[str, Any]],
        event_id: str,
        *,
        since: float | None = None,
        limit: int = 200,
    ) -> list[dict[str, Any]]:
        matched: list[dict[str, Any]] = []
        for entry in entries:
            if since is not None and float(entry.get("time", 0) or 0) <= since:
                continue
            if self._log_entry_matches_event_id(entry, event_id):
                matched.append(serialize_value(entry))
        if limit > 0:
            matched = matched[-limit:]
        return matched

    async def event_logs(
        self, request: Request, payload: None = None
    ) -> dict[str, Any]:
        event_id = request.path_params["event_id"]
        limit = max(1, min(self._query_int(request, "limit", 200), 5000))
        wait_seconds = max(
            0.0,
            min(self._query_float(request, "wait_seconds", 0.0), 600.0),
        )
        broker = self._get_log_broker()
        if broker is None:
            raise GatewayError("log broker is not available", status_code=503)

        history = self._filter_event_logs(list(broker.log_cache), event_id, limit=limit)
        live: list[dict[str, Any]] = []
        if wait_seconds > 0:
            queue = broker.register()
            loop = asyncio.get_running_loop()
            deadline = loop.time() + wait_seconds
            try:
                while True:
                    remaining = deadline - loop.time()
                    if remaining <= 0:
                        break
                    try:
                        entry = await asyncio.wait_for(queue.get(), timeout=remaining)
                    except asyncio.TimeoutError:
                        break
                    if self._log_entry_matches_event_id(entry, event_id):
                        live.append(serialize_value(entry))
                        if len(live) >= limit:
                            live = live[-limit:]
            finally:
                broker.unregister(queue)

        return {
            "event_id": event_id,
            "mode": "watch" if wait_seconds > 0 else "history",
            "wait_seconds": wait_seconds,
            "history": history,
            "live": live,
            "logs": [*(history[-limit:]), *live][-limit:],
            "count": len(history) + len(live),
        }

    async def stream_event_logs(
        self, request: Request, payload: None = None
    ) -> StreamingResponse:
        event_id = request.path_params["event_id"]
        limit = max(1, min(self._query_int(request, "limit", 200), 5000))
        replay_history = self._query_bool(request, "replay_history", True)
        broker = self._get_log_broker()
        if broker is None:
            raise GatewayError("log broker is not available", status_code=503)

        last_event_id = request.headers.get("Last-Event-ID")
        since = None
        if last_event_id:
            try:
                since = float(last_event_id)
            except ValueError:
                since = None

        async def stream():
            if replay_history:
                cached = self._filter_event_logs(
                    list(broker.log_cache),
                    event_id,
                    since=since,
                    limit=limit,
                )
                for entry in cached:
                    ts = float(entry.get("time", 0) or 0)
                    payload = {"type": "log", "event_id": event_id, **entry}
                    yield f"id: {ts}\ndata: {json.dumps(payload, ensure_ascii=False)}\n\n"

            queue = broker.register()
            try:
                while True:
                    try:
                        entry = await asyncio.wait_for(queue.get(), timeout=15.0)
                    except asyncio.TimeoutError:
                        yield ": keep-alive\n\n"
                        continue
                    if not self._log_entry_matches_event_id(entry, event_id):
                        continue
                    ts = float(entry.get("time", 0) or 0)
                    payload = {
                        "type": "log",
                        "event_id": event_id,
                        **serialize_value(entry),
                    }
                    yield f"id: {ts}\ndata: {json.dumps(payload, ensure_ascii=False)}\n\n"
            finally:
                broker.unregister(queue)

        return StreamingResponse(
            stream(),
            media_type="text/event-stream",
            headers={
                "Cache-Control": "no-cache",
                "Connection": "keep-alive",
                "X-Accel-Buffering": "no",
            },
        )

    async def send_message(
        self, request: Request, payload: SendMessageRequest
    ) -> dict[str, Any]:
        ok = await self.context.send_message(
            payload.session,
            build_message_chain(payload.message, payload.message_chain),
        )
        return {"session": payload.session, "sent": bool(ok)}

    async def send_last_message(
        self, request: Request, payload: SendLastMessageRequest
    ) -> dict[str, Any]:
        if not self.plugin._last_session:
            raise GatewayError("no recent session captured yet")
        ok = await self.context.send_message(
            self.plugin._last_session,
            build_message_chain(payload.message, payload.message_chain),
        )
        return {"session": self.plugin._last_session, "sent": bool(ok)}

    async def message_history(
        self, request: Request, payload: None = None
    ) -> list[dict[str, Any]]:
        platform_id = self._require_query(request, "platform_id")
        user_id = self._require_query(request, "user_id")
        page = max(1, self._query_int(request, "page", 1))
        page_size = max(1, min(self._query_int(request, "page_size", 50), 500))
        history = await self.message_history_manager.get(
            platform_id=platform_id,
            user_id=user_id,
            page=page,
            page_size=page_size,
        )
        return serialize_value(history)

    async def list_conversations(
        self, request: Request, payload: None = None
    ) -> dict[str, Any]:
        page = max(1, self._query_int(request, "page", 1))
        page_size = max(1, min(self._query_int(request, "page_size", 20), 100))
        platform_ids = self._query_csv(request, "platforms")
        message_types = self._query_csv(request, "message_types")
        exclude_ids = self._query_csv(request, "exclude_ids")
        exclude_platforms = self._query_csv(request, "exclude_platforms")
        search = request.query_params.get("search", "")
        (
            conversations,
            total,
        ) = await self.conversation_manager.get_filtered_conversations(
            page=page,
            page_size=page_size,
            platform_ids=platform_ids,
            search_query=search,
            message_types=message_types,
            exclude_ids=exclude_ids,
            exclude_platforms=exclude_platforms,
        )
        return {
            "conversations": serialize_value(conversations),
            "pagination": {
                "page": page,
                "page_size": page_size,
                "total": total,
                "total_pages": ((total + page_size - 1) // page_size) if total else 1,
            },
        }

    async def create_conversation(
        self, request: Request, payload: ConversationCreateRequest
    ) -> dict[str, Any]:
        self._ensure_mutation_allowed()
        conversation_id = await self.conversation_manager.new_conversation(
            payload.unified_msg_origin,
            platform_id=payload.platform_id,
            title=payload.title,
            persona_id=payload.persona_id,
        )
        conv = await self.db.get_conversation_by_id(conversation_id)
        return self._serialize_conversation_v2(conv)

    async def get_conversation(
        self, request: Request, payload: None = None
    ) -> dict[str, Any]:
        conversation_id = request.path_params["conversation_id"]
        conv = await self.db.get_conversation_by_id(conversation_id)
        if not conv:
            raise GatewayError(
                f"conversation not found: {conversation_id}", status_code=404
            )
        return self._serialize_conversation_v2(conv)

    async def update_conversation(
        self, request: Request, payload: ConversationUpdateRequest
    ) -> dict[str, Any]:
        self._ensure_mutation_allowed()
        conversation_id = request.path_params["conversation_id"]
        conv = await self.db.get_conversation_by_id(conversation_id)
        if not conv:
            raise GatewayError(
                f"conversation not found: {conversation_id}", status_code=404
            )
        await self.conversation_manager.update_conversation(
            conv.user_id,
            conversation_id=conversation_id,
            history=payload.history,
            title=payload.title,
            persona_id=payload.persona_id,
            token_usage=payload.token_usage,
        )
        updated = await self.db.get_conversation_by_id(conversation_id)
        return self._serialize_conversation_v2(updated)

    async def delete_conversation(
        self, request: Request, payload: None = None
    ) -> dict[str, Any]:
        self._ensure_mutation_allowed()
        conversation_id = request.path_params["conversation_id"]
        conv = await self.db.get_conversation_by_id(conversation_id)
        if not conv:
            raise GatewayError(
                f"conversation not found: {conversation_id}", status_code=404
            )
        await self.conversation_manager.delete_conversation(
            conv.user_id, conversation_id
        )
        return {"conversation_id": conversation_id, "deleted": True}

    async def activate_conversation(
        self, request: Request, payload: ConversationActivateRequest
    ) -> dict[str, Any]:
        self._ensure_mutation_allowed()
        conversation_id = request.path_params["conversation_id"]
        await self.conversation_manager.switch_conversation(
            payload.unified_msg_origin,
            conversation_id,
        )
        return {
            "conversation_id": conversation_id,
            "unified_msg_origin": payload.unified_msg_origin,
        }

    async def list_kbs(
        self, request: Request, payload: None = None
    ) -> list[dict[str, Any]]:
        kbs = await self.kb_manager.list_kbs()
        return [self._serialize_kb(kb) for kb in kbs]

    async def create_kb(
        self, request: Request, payload: KnowledgeBaseCreateRequest
    ) -> dict[str, Any]:
        self._ensure_mutation_allowed()
        helper = await self.kb_manager.create_kb(**payload.model_dump())
        return self._serialize_kb(helper.kb)

    async def get_kb(self, request: Request, payload: None = None) -> dict[str, Any]:
        kb_id = request.path_params["kb_id"]
        helper = await self.kb_manager.get_kb(kb_id)
        if not helper:
            raise GatewayError(f"knowledge base not found: {kb_id}", status_code=404)
        return self._serialize_kb(helper.kb)

    async def update_kb(
        self, request: Request, payload: KnowledgeBaseUpdateRequest
    ) -> dict[str, Any]:
        self._ensure_mutation_allowed()
        kb_id = request.path_params["kb_id"]
        helper = await self.kb_manager.get_kb(kb_id)
        if not helper:
            raise GatewayError(f"knowledge base not found: {kb_id}", status_code=404)
        updated = await self.kb_manager.update_kb(
            kb_id=kb_id,
            kb_name=payload.kb_name or helper.kb.kb_name,
            description=payload.description,
            emoji=payload.emoji,
            embedding_provider_id=payload.embedding_provider_id,
            rerank_provider_id=payload.rerank_provider_id,
            chunk_size=payload.chunk_size,
            chunk_overlap=payload.chunk_overlap,
            top_k_dense=payload.top_k_dense,
            top_k_sparse=payload.top_k_sparse,
            top_m_final=payload.top_m_final,
        )
        if not updated:
            raise GatewayError(f"knowledge base not found: {kb_id}", status_code=404)
        return self._serialize_kb(updated.kb)

    async def delete_kb(self, request: Request, payload: None = None) -> dict[str, Any]:
        self._ensure_mutation_allowed()
        kb_id = request.path_params["kb_id"]
        ok = await self.kb_manager.delete_kb(kb_id)
        if not ok:
            raise GatewayError(f"knowledge base not found: {kb_id}", status_code=404)
        return {"kb_id": kb_id, "deleted": True}

    async def list_kb_documents(
        self, request: Request, payload: None = None
    ) -> list[dict[str, Any]]:
        kb_id = request.path_params["kb_id"]
        helper = await self.kb_manager.get_kb(kb_id)
        if not helper:
            raise GatewayError(f"knowledge base not found: {kb_id}", status_code=404)
        limit = max(1, min(self._query_int(request, "limit", 200), 500))
        docs = await self.kb_manager.kb_db.list_documents_by_kb(
            kb_id, offset=0, limit=limit
        )
        return serialize_value(docs)

    async def search_kb(
        self, request: Request, payload: KBSearchRequest
    ) -> dict[str, Any]:
        return serialize_value(
            await self.kb_manager.retrieve(
                query=payload.query,
                kb_names=payload.kb_names,
                top_k_fusion=payload.top_k_fusion,
                top_m_final=payload.top_m_final,
            )
            or {"context_text": "", "results": []}
        )

    async def upload_kb_url(
        self, request: Request, payload: KBUploadUrlRequest
    ) -> dict[str, Any]:
        self._ensure_mutation_allowed()
        kb_id = request.path_params["kb_id"]
        doc = await self.kb_manager.upload_from_url(
            kb_id=kb_id,
            url=payload.url,
            chunk_size=payload.chunk_size,
            chunk_overlap=payload.chunk_overlap,
            batch_size=payload.batch_size,
            tasks_limit=payload.tasks_limit,
            max_retries=payload.max_retries,
        )
        return serialize_value(doc)

    async def list_personas(
        self, request: Request, payload: None = None
    ) -> list[dict[str, Any]]:
        folder_id = request.query_params.get("folder_id")
        personas = (
            await self.persona_manager.get_personas_by_folder(folder_id)
            if folder_id is not None
            else await self.persona_manager.get_all_personas()
        )
        return [self._serialize_persona(persona) for persona in personas]

    async def get_persona(
        self, request: Request, payload: None = None
    ) -> dict[str, Any]:
        persona = await self.persona_manager.get_persona(
            request.path_params["persona_id"]
        )
        return self._serialize_persona(persona)

    async def create_persona(
        self, request: Request, payload: PersonaCreateRequest
    ) -> dict[str, Any]:
        self._ensure_mutation_allowed()
        persona = await self.persona_manager.create_persona(**payload.model_dump())
        return self._serialize_persona(persona)

    async def update_persona(
        self, request: Request, payload: PersonaUpdateRequest
    ) -> dict[str, Any]:
        self._ensure_mutation_allowed()
        persona = await self.persona_manager.update_persona(
            request.path_params["persona_id"],
            system_prompt=payload.system_prompt,
            begin_dialogs=payload.begin_dialogs,
            tools=payload.tools,
            skills=payload.skills,
        )
        if not persona:
            raise GatewayError("persona update failed", status_code=500)
        return self._serialize_persona(persona)

    async def delete_persona(
        self, request: Request, payload: None = None
    ) -> dict[str, Any]:
        self._ensure_mutation_allowed()
        persona_id = request.path_params["persona_id"]
        await self.persona_manager.delete_persona(persona_id)
        return {"persona_id": persona_id, "deleted": True}

    async def move_persona(
        self, request: Request, payload: PersonaMoveRequest
    ) -> dict[str, Any]:
        self._ensure_mutation_allowed()
        persona = await self.persona_manager.move_persona_to_folder(
            request.path_params["persona_id"],
            payload.folder_id,
        )
        if not persona:
            raise GatewayError("persona not found", status_code=404)
        return self._serialize_persona(persona)

    async def list_persona_folders(
        self, request: Request, payload: None = None
    ) -> list[dict[str, Any]]:
        parent_id = request.query_params.get("parent_id")
        folders = await self.persona_manager.get_folders(parent_id)
        return [self._serialize_persona_folder(folder) for folder in folders]

    async def persona_folder_tree(
        self, request: Request, payload: None = None
    ) -> list[dict[str, Any]]:
        return serialize_value(await self.persona_manager.get_folder_tree())

    async def create_persona_folder(
        self,
        request: Request,
        payload: PersonaFolderCreateRequest,
    ) -> dict[str, Any]:
        self._ensure_mutation_allowed()
        folder = await self.persona_manager.create_folder(**payload.model_dump())
        return self._serialize_persona_folder(folder)

    async def update_persona_folder(
        self,
        request: Request,
        payload: PersonaFolderUpdateRequest,
    ) -> dict[str, Any]:
        self._ensure_mutation_allowed()
        folder = await self.persona_manager.update_folder(
            request.path_params["folder_id"],
            **payload.model_dump(),
        )
        if not folder:
            raise GatewayError("persona folder not found", status_code=404)
        return self._serialize_persona_folder(folder)

    async def delete_persona_folder(
        self, request: Request, payload: None = None
    ) -> dict[str, Any]:
        self._ensure_mutation_allowed()
        folder_id = request.path_params["folder_id"]
        await self.persona_manager.delete_folder(folder_id)
        return {"folder_id": folder_id, "deleted": True}

    async def batch_update_persona_sort_order(
        self,
        request: Request,
        payload: BatchSortRequest,
    ) -> dict[str, Any]:
        self._ensure_mutation_allowed()
        await self.persona_manager.batch_update_sort_order(payload.items)
        return {"updated": len(payload.items)}

    async def list_cron_jobs(
        self, request: Request, payload: None = None
    ) -> list[dict[str, Any]]:
        job_type = request.query_params.get("job_type")
        jobs = await self.cron_manager.list_jobs(job_type=job_type)
        return [self._serialize_cron_job(job) for job in jobs]

    async def get_cron_job(
        self, request: Request, payload: None = None
    ) -> dict[str, Any]:
        job_id = request.path_params["job_id"]
        for job in await self.cron_manager.list_jobs():
            if job.job_id == job_id:
                return self._serialize_cron_job(job)
        raise GatewayError(f"cron job not found: {job_id}", status_code=404)

    async def create_cron_job(
        self, request: Request, payload: CronCreateRequest
    ) -> dict[str, Any]:
        self._ensure_mutation_allowed()
        run_at = dt.datetime.fromisoformat(payload.run_at) if payload.run_at else None
        job = await self.cron_manager.add_active_job(
            name=payload.name,
            cron_expression=payload.cron_expression,
            payload={
                "session": payload.session,
                "note": payload.note or payload.name,
            },
            description=payload.description,
            timezone=payload.timezone,
            enabled=payload.enabled,
            persistent=payload.persistent,
            run_once=payload.run_once,
            run_at=run_at,
        )
        return self._serialize_cron_job(job)

    async def update_cron_job(
        self, request: Request, payload: CronUpdateRequest
    ) -> dict[str, Any]:
        self._ensure_mutation_allowed()
        data = {
            key: value
            for key, value in payload.model_dump().items()
            if value is not None
        }
        job = await self.cron_manager.update_job(request.path_params["job_id"], **data)
        if not job:
            raise GatewayError("cron job not found", status_code=404)
        return self._serialize_cron_job(job)

    async def delete_cron_job(
        self, request: Request, payload: None = None
    ) -> dict[str, Any]:
        self._ensure_mutation_allowed()
        job_id = request.path_params["job_id"]
        await self.cron_manager.delete_job(job_id)
        return {"job_id": job_id, "deleted": True}

    async def list_tools(
        self, request: Request, payload: None = None
    ) -> list[dict[str, Any]]:
        return [
            self._serialize_tool(tool)
            for tool in self.provider_manager.llm_tools.func_list
        ]

    async def get_tool(self, request: Request, payload: None = None) -> dict[str, Any]:
        return self._serialize_tool(
            self._resolve_tool(request.path_params["tool_name"])
        )

    async def invoke_tool(
        self, request: Request, payload: ToolInvokeRequest
    ) -> dict[str, Any]:
        tool_name = request.path_params["tool_name"]
        tool = self._resolve_tool(tool_name)
        if not bool(getattr(tool, "active", True)):
            raise GatewayError(f"tool is disabled: {tool_name}", status_code=409)

        tool_args = ensure_mapping(
            copy.deepcopy(payload.arguments),
            "arguments must be an object",
        )
        argument_debug = (
            self._build_argument_debug(tool_args) if payload.debug_arguments else None
        )
        conversation_id = payload.conversation_id or f"tool-{payload.sender_id}"
        message_id = payload.message_id or f"tool_{uuid.uuid4().hex}"
        creator = payload.creator or self._dashboard_username()
        display_name = payload.display_name or payload.sender_id
        capture_messages = payload.capture_messages or payload.persist_history

        session_info: dict[str, Any] | None = None
        if payload.ensure_webui_session or payload.persist_history:
            session_info = await self._ensure_webchat_platform_session(
                conversation_id=conversation_id,
                creator=creator,
                display_name=display_name,
            )

        event, request_parts = self._build_tool_invoke_event(
            tool_name,
            payload,
            conversation_id=conversation_id,
            message_id=message_id,
            display_name=display_name,
        )
        run_context = ContextWrapper(
            context=AstrAgentContext(context=self.context, event=event),
            tool_call_timeout=payload.tool_call_timeout,
        )

        if payload.persist_history:
            await self._save_webchat_history_message(
                conversation_id,
                sender_id=payload.sender_id,
                sender_name=display_name,
                message_parts=request_parts,
            )

        results: list[dict[str, Any]] = []
        emitted: dict[str, Any] = {
            "events": [],
            "message_parts": [],
            "text": None,
            "reasoning": None,
            "completed": False,
        }

        if capture_messages:
            webchat_queue_mgr.get_or_create_back_queue(message_id, conversation_id)

        try:
            async with self._temporary_tool_umop_route(
                event.unified_msg_origin,
                payload.config_id,
            ):
                async for result in FunctionToolExecutor.execute(
                    tool,
                    run_context,
                    **tool_args,
                ):
                    if result is None:
                        continue
                    results.append(self._normalize_call_tool_result(result))

            if capture_messages:
                emitted = await self._collect_tool_emitted_events(
                    message_id,
                    conversation_id,
                    payload.response_timeout_seconds,
                    include_base64=payload.include_base64,
                    base64_max_bytes=payload.base64_max_bytes,
                )
        finally:
            if capture_messages:
                webchat_queue_mgr.remove_back_queue(message_id)

        emitted["message_parts"] = self._decorate_attachment_links(
            request,
            emitted.get("message_parts") or [],
        )

        stored_parts = emitted.get("message_parts") or []
        stored_reasoning = emitted.get("reasoning") or ""
        if payload.persist_history and not stored_parts:
            stored_parts = self._message_parts_from_tool_results(results)
        if payload.persist_history and stored_parts:
            await self._save_webchat_history_message(
                conversation_id,
                sender_id="bot",
                sender_name="bot",
                message_parts=self._history_safe_message_parts(stored_parts),
                reasoning=stored_reasoning,
            )

        response = {
            "tool": self._serialize_tool(tool),
            "arguments": serialize_value(tool_args),
            "conversation_id": conversation_id,
            "message_id": message_id,
            "unified_msg_origin": event.unified_msg_origin,
            "config_id": payload.config_id,
            "webui_session": session_info,
            "results": results,
            "emitted": emitted,
        }
        if argument_debug is not None:
            response["debug"] = {
                "arguments": argument_debug,
            }
        return response

    async def toggle_tool(
        self, request: Request, payload: ToolToggleRequest
    ) -> dict[str, Any]:
        self._ensure_mutation_allowed()
        tool_name = request.path_params["tool_name"]
        ok = (
            self.context.activate_llm_tool(tool_name)
            if payload.active
            else self.context.deactivate_llm_tool(tool_name)
        )
        if not ok:
            raise GatewayError(f"tool not found: {tool_name}", status_code=404)
        return {"tool_name": tool_name, "active": payload.active}

    async def list_subagents(
        self, request: Request, payload: None = None
    ) -> list[dict[str, Any]]:
        orchestrator = self.subagent_orchestrator
        if not orchestrator:
            return []
        return [self._serialize_subagent(handoff) for handoff in orchestrator.handoffs]

    async def get_subagent_config(
        self, request: Request, payload: None = None
    ) -> dict[str, Any]:
        data = copy.deepcopy(self.acm.default_conf.get("subagent_orchestrator"))
        if not isinstance(data, dict):
            data = {
                "main_enable": False,
                "remove_main_duplicate_tools": False,
                "agents": [],
            }
        if "main_enable" not in data and "enable" in data:
            data["main_enable"] = bool(data.get("enable", False))
        data.setdefault("main_enable", False)
        data.setdefault("remove_main_duplicate_tools", False)
        data.setdefault("agents", [])
        for agent in data.get("agents", []):
            if isinstance(agent, dict):
                agent.setdefault("provider_id", None)
                agent.setdefault("persona_id", None)
        return data

    async def update_subagent_config(
        self, request: Request, payload: SubagentConfigUpdateRequest
    ) -> dict[str, Any]:
        self._ensure_mutation_allowed()
        self.acm.default_conf["subagent_orchestrator"] = payload.config
        save_config(self.acm.default_conf, self.acm.default_conf, is_core=True)
        orchestrator = self.subagent_orchestrator
        if orchestrator is not None:
            await orchestrator.reload_from_config(payload.config)
        return {"saved": True}

    async def list_subagent_available_tools(
        self, request: Request, payload: None = None
    ) -> list[dict[str, Any]]:
        tools: list[dict[str, Any]] = []
        for tool in self.provider_manager.llm_tools.func_list:
            if isinstance(tool, HandoffTool):
                continue
            if (
                getattr(tool, "handler_module_path", None)
                == "core.subagent_orchestrator"
            ):
                continue
            tools.append(self._serialize_tool(tool))
        return tools

    async def list_skills(
        self, request: Request, payload: None = None
    ) -> dict[str, Any]:
        skill_mgr, runtime = self._runtime_skill_manager()
        skills = skill_mgr.list_skills(
            active_only=False, runtime=runtime, show_sandbox_path=False
        )
        return {"skills": [serialize_value(skill) for skill in skills]}

    async def upload_skill(
        self, request: Request, payload: dict[str, Any] | None = None
    ) -> dict[str, Any]:
        self._ensure_mutation_allowed()
        files = (payload or {}).get("files", [])
        if not files:
            raise GatewayError("missing file")
        file = files[0]
        filename = os.path.basename(getattr(file, "filename", "") or "skill.zip")
        if not filename.lower().endswith(".zip"):
            raise GatewayError("only .zip files are supported")
        temp_dir = get_astrbot_temp_path()
        os.makedirs(temp_dir, exist_ok=True)
        fd, temp_path = tempfile.mkstemp(
            prefix="skill-upload-", suffix=".zip", dir=temp_dir
        )
        os.close(fd)
        try:
            with open(temp_path, "wb") as stream:
                while True:
                    chunk = await file.read(1024 * 1024)
                    if not chunk:
                        break
                    stream.write(chunk)
            skill_name = SkillManager().install_skill_from_zip(
                temp_path, overwrite=True
            )
            return {"name": skill_name}
        finally:
            try:
                os.remove(temp_path)
            except OSError:
                logger.warning("Failed to remove temporary uploaded skill zip.")

    async def toggle_skill(
        self, request: Request, payload: SkillToggleRequest
    ) -> dict[str, Any]:
        self._ensure_mutation_allowed()
        skill_name = request.path_params["skill_name"]
        SkillManager().set_skill_active(skill_name, payload.active)
        return {"name": skill_name, "active": payload.active}

    async def delete_skill(
        self, request: Request, payload: None = None
    ) -> dict[str, Any]:
        self._ensure_mutation_allowed()
        skill_name = request.path_params["skill_name"]
        SkillManager().delete_skill(skill_name)
        return {"name": skill_name, "deleted": True}

    async def list_mcp_servers(
        self, request: Request, payload: None = None
    ) -> list[dict[str, Any]]:
        tool_mgr = self.provider_manager.llm_tools
        config = tool_mgr.load_mcp_config()
        servers: list[dict[str, Any]] = []
        for name, server_config in config.get("mcpServers", {}).items():
            server_info = {"name": name, "active": server_config.get("active", True)}
            for key, value in server_config.items():
                if key != "active":
                    server_info[key] = serialize_value(value)
            client = tool_mgr.mcp_client_dict.get(name)
            server_info["tools"] = (
                [tool.name for tool in client.tools] if client else []
            )
            server_info["errlogs"] = (
                serialize_value(client.server_errlogs) if client else []
            )
            servers.append(server_info)
        return servers

    async def add_mcp_server(
        self, request: Request, payload: MCPServerCreateRequest
    ) -> dict[str, Any]:
        self._ensure_mutation_allowed()
        tool_mgr = self.provider_manager.llm_tools
        config = tool_mgr.load_mcp_config()
        if payload.name in config.get("mcpServers", {}):
            raise GatewayError(f"mcp server already exists: {payload.name}")
        server_config = {"active": payload.active, **payload.config}
        config.setdefault("mcpServers", {})[payload.name] = server_config
        if not tool_mgr.save_mcp_config(config):
            raise GatewayError("failed to save mcp config", status_code=500)
        if server_config.get("active", True):
            await tool_mgr.enable_mcp_server(payload.name, server_config, timeout=30)
        return {"name": payload.name, "created": True}

    async def update_mcp_server(
        self, request: Request, payload: MCPServerUpdateRequest
    ) -> dict[str, Any]:
        self._ensure_mutation_allowed()
        tool_mgr = self.provider_manager.llm_tools
        config = tool_mgr.load_mcp_config()
        old_name = payload.old_name or request.path_params["server_name"]
        new_name = payload.name or request.path_params["server_name"]
        if old_name not in config.get("mcpServers", {}):
            raise GatewayError(f"mcp server not found: {old_name}", status_code=404)
        old_config = copy.deepcopy(config["mcpServers"][old_name])
        active = (
            payload.active
            if payload.active is not None
            else old_config.get("active", True)
        )
        server_config = {**old_config, "active": active}
        if payload.config:
            server_config.update(payload.config)
        if new_name != old_name:
            config["mcpServers"].pop(old_name, None)
        config["mcpServers"][new_name] = server_config
        if not tool_mgr.save_mcp_config(config):
            raise GatewayError("failed to save mcp config", status_code=500)
        if old_name in tool_mgr.mcp_client_dict:
            await tool_mgr.disable_mcp_server(old_name, timeout=10)
        if server_config.get("active", True):
            await tool_mgr.enable_mcp_server(new_name, server_config, timeout=30)
        return {"name": new_name, "updated": True}

    async def delete_mcp_server(
        self, request: Request, payload: None = None
    ) -> dict[str, Any]:
        self._ensure_mutation_allowed()
        tool_mgr = self.provider_manager.llm_tools
        server_name = request.path_params["server_name"]
        config = tool_mgr.load_mcp_config()
        if server_name not in config.get("mcpServers", {}):
            raise GatewayError(f"mcp server not found: {server_name}", status_code=404)
        config["mcpServers"].pop(server_name, None)
        if not tool_mgr.save_mcp_config(config):
            raise GatewayError("failed to save mcp config", status_code=500)
        if server_name in tool_mgr.mcp_client_dict:
            await tool_mgr.disable_mcp_server(server_name, timeout=10)
        return {"name": server_name, "deleted": True}

    async def test_mcp_connection(
        self, request: Request, payload: MCPConnectionTestRequest
    ) -> dict[str, Any]:
        tools_name = await self.provider_manager.llm_tools.test_mcp_server_connection(
            payload.mcp_server_config
        )
        return {"tools": tools_name}

    async def sync_mcp_provider(
        self, request: Request, payload: MCPSyncProviderRequest
    ) -> dict[str, Any]:
        self._ensure_mutation_allowed()
        if payload.name != "modelscope":
            raise GatewayError(f"unknown provider: {payload.name}")
        await self.provider_manager.llm_tools.sync_modelscope_mcp_servers(
            payload.access_token
        )
        return {"provider": payload.name, "synced": True}

    async def inject_message(
        self, request: Request, payload: InjectMessageRequest
    ) -> dict[str, Any]:
        parts = to_plain_message_parts(payload.message, payload.message_chain)
        if not parts:
            raise GatewayError("message or message_chain is required")

        sender_id = payload.sender_id
        message_id = payload.message_id or f"injected_{uuid.uuid4().hex}"
        if self._should_inject_via_platform(payload):
            return await self._inject_platform_message(payload, message_id, parts)

        conversation_id = payload.conversation_id or f"rest-{sender_id}"
        creator = payload.creator or self._dashboard_username()
        display_name = payload.display_name or sender_id

        session_info: dict[str, Any] | None = None
        if (
            payload.ensure_webui_session
            or payload.persist_history
            or payload.persist_bot_response
        ):
            session_info = await self._ensure_webchat_platform_session(
                conversation_id=conversation_id,
                creator=creator,
                display_name=display_name,
            )

        queue = webchat_queue_mgr.get_or_create_queue(conversation_id)
        await queue.put(
            (
                sender_id,
                conversation_id,
                {
                    "message": parts,
                    "selected_provider": payload.selected_provider,
                    "selected_model": payload.selected_model,
                    "enable_streaming": payload.enable_streaming,
                    "message_id": message_id,
                    "action_type": payload.action_type,
                    "injected": True,
                    "injection_source": "rest_api",
                },
            )
        )

        if payload.persist_history:
            await self._save_webchat_history_message(
                conversation_id,
                sender_id=sender_id,
                sender_name=sender_id,
                message_parts=parts,
            )

        bot_response_persistence = False
        if payload.persist_bot_response:
            self.plugin.create_background_task(
                self._drain_injected_webchat_back_queue(
                    conversation_id=conversation_id,
                    message_id=message_id,
                    timeout_seconds=payload.response_timeout_seconds,
                ),
                name=f"inject-webchat-{conversation_id}-{message_id}",
            )
            bot_response_persistence = True

        return {
            "platform_id": "webchat",
            "conversation_id": conversation_id,
            "message_id": message_id,
            "unified_msg_origin": (
                f"webchat:{MessageType.FRIEND_MESSAGE.value}:webchat!{sender_id}!{conversation_id}"
            ),
            "queued": True,
            "dispatch_method": "webchat.queue",
            "entered_pipeline": True,
            "creator": creator,
            "display_name": display_name,
            "webui_session": session_info,
            "persist_bot_response": bot_response_persistence,
        }
















