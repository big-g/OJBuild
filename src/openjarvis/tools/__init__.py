"""Tools primitive — tool system with ABC interface and built-in tools."""

from __future__ import annotations

from openjarvis.tools._stubs import BaseTool, ToolExecutor, ToolSpec

# Import built-in tools to trigger @ToolRegistry.register() decorators.
# Each is wrapped in try/except so the package loads even before the
# individual tool modules are created.
try:
    import openjarvis.tools.calculator  # noqa: F401
except ImportError:
    pass

try:
    import openjarvis.tools.think  # noqa: F401
except ImportError:
    pass

try:
    import openjarvis.tools.retrieval  # noqa: F401
except ImportError:
    pass

try:
    import openjarvis.tools.llm_tool  # noqa: F401
except ImportError:
    pass

try:
    import openjarvis.tools.file_read  # noqa: F401
except ImportError:
    pass

try:
    import openjarvis.tools.web_search  # noqa: F401
except ImportError:
    pass

try:
    import openjarvis.tools.code_interpreter  # noqa: F401
except ImportError:
    pass

try:
    import openjarvis.tools.code_interpreter_docker  # noqa: F401
except ImportError:
    pass

try:
    import openjarvis.tools.repl  # noqa: F401
except ImportError:
    pass

try:
    import openjarvis.tools.storage_tools  # noqa: F401
except ImportError:
    pass

try:
    import openjarvis.tools.mcp_adapter  # noqa: F401
except ImportError:
    pass

try:
    import openjarvis.tools.channel_tools  # noqa: F401
except ImportError:
    pass

try:
    import openjarvis.tools.http_request  # noqa: F401
except ImportError:
    pass

try:
    import openjarvis.tools.docker_shell_exec  # noqa: F401
    import openjarvis.tools.shell_exec  # noqa: F401
except ImportError:
    pass

try:
    import openjarvis.tools.memory_manage  # noqa: F401
except ImportError:
    pass
try:
    import openjarvis.tools.user_profile_manage  # noqa: F401
except ImportError:
    pass

try:
    import openjarvis.tools.skill_manage  # noqa: F401
except ImportError:
    pass

try:
    import openjarvis.tools.file_write  # noqa: F401
except ImportError:
    pass

try:
    import openjarvis.tools.apply_patch  # noqa: F401
except ImportError:
    pass

try:
    import openjarvis.tools.git_tool  # noqa: F401
except ImportError:
    pass

try:
    import openjarvis.tools.db_query  # noqa: F401
except ImportError:
    pass

try:
    import openjarvis.tools.pdf_tool  # noqa: F401
except ImportError:
    pass

try:
    import openjarvis.tools.image_tool  # noqa: F401
except ImportError:
    pass

try:
    import openjarvis.tools.audio_tool  # noqa: F401
except ImportError:
    pass

try:
    import openjarvis.tools.knowledge_tools  # noqa: F401
except ImportError:
    pass

try:
    import openjarvis.tools.text_to_speech  # noqa: F401
except ImportError:
    pass

try:
    import openjarvis.tools.digest_collect  # noqa: F401
except ImportError:
    pass

try:
    import openjarvis.tools.scan_chunks  # noqa: F401
except ImportError:
    pass

try:
    import openjarvis.tools.knowledge_sql  # noqa: F401
except ImportError:
    pass

try:
    import openjarvis.tools.apple_calendar  # noqa: F401
except ImportError:
    pass

# Canonical modules that define the global built-in tool catalog.
#
# Registry restoration must use this explicit list rather than reloading every
# openjarvis.tools.* module that happens to be present in sys.modules. Some
# specialized tools (for example knowledge_search) are imported only by
# particular agent/runtime paths and must not leak into the global built-in
# catalog merely because another test or feature imported them earlier.
BUILTIN_TOOL_MODULES = (
    "openjarvis.tools.calculator",
    "openjarvis.tools.think",
    "openjarvis.tools.retrieval",
    "openjarvis.tools.llm_tool",
    "openjarvis.tools.file_read",
    "openjarvis.tools.web_search",
    "openjarvis.tools.code_interpreter",
    "openjarvis.tools.code_interpreter_docker",
    "openjarvis.tools.repl",
    "openjarvis.tools.storage_tools",
    "openjarvis.tools.mcp_adapter",
    "openjarvis.tools.channel_tools",
    "openjarvis.tools.http_request",
    "openjarvis.tools.docker_shell_exec",
    "openjarvis.tools.shell_exec",
    "openjarvis.tools.memory_manage",
    "openjarvis.tools.user_profile_manage",
    "openjarvis.tools.skill_manage",
    "openjarvis.tools.file_write",
    "openjarvis.tools.apply_patch",
    "openjarvis.tools.git_tool",
    "openjarvis.tools.db_query",
    "openjarvis.tools.pdf_tool",
    "openjarvis.tools.image_tool",
    "openjarvis.tools.audio_tool",
    "openjarvis.tools.knowledge_tools",
    "openjarvis.tools.text_to_speech",
    "openjarvis.tools.digest_collect",
    "openjarvis.tools.scan_chunks",
    "openjarvis.tools.knowledge_sql",
    "openjarvis.tools.apple_calendar",
)

__all__ = [
    "BUILTIN_TOOL_MODULES",
    "BaseTool",
    "ToolExecutor",
    "ToolSpec",
]
