"""Tool handler modules for the Shipyard Neo MCP server."""

from shipyard_neo_mcp.handlers.sandbox import (
    handle_create_sandbox,
    handle_delete_sandbox,
)
from shipyard_neo_mcp.handlers.execution import (
    handle_execute_python,
    handle_execute_shell,
)
from shipyard_neo_mcp.handlers.filesystem import (
    handle_read_file,
    handle_write_file,
    handle_list_files,
    handle_delete_file,
    handle_upload_file,
    handle_download_file,
)
from shipyard_neo_mcp.handlers.history import (
    handle_get_execution_history,
    handle_get_execution,
    handle_get_last_execution,
    handle_annotate_execution,
)
from shipyard_neo_mcp.handlers.skills import (
    handle_create_skill_payload,
    handle_get_skill_payload,
    handle_create_skill_candidate,
    handle_evaluate_skill_candidate,
    handle_promote_skill_candidate,
    handle_list_skill_candidates,
    handle_list_skill_releases,
    handle_delete_skill_release,
    handle_delete_skill_candidate,
    handle_rollback_skill_release,
    handle_declare_skill_goal,
    handle_get_active_skill,
    handle_report_skill_outcome,
)
from shipyard_neo_mcp.handlers.browser import (
    handle_execute_browser,
    handle_execute_browser_batch,
)
from shipyard_neo_mcp.handlers.profiles import handle_list_profiles

__all__ = [
    "handle_create_sandbox",
    "handle_delete_sandbox",
    "handle_execute_python",
    "handle_execute_shell",
    "handle_read_file",
    "handle_write_file",
    "handle_list_files",
    "handle_delete_file",
    "handle_upload_file",
    "handle_download_file",
    "handle_get_execution_history",
    "handle_get_execution",
    "handle_get_last_execution",
    "handle_annotate_execution",
    "handle_create_skill_payload",
    "handle_get_skill_payload",
    "handle_create_skill_candidate",
    "handle_evaluate_skill_candidate",
    "handle_promote_skill_candidate",
    "handle_list_skill_candidates",
    "handle_list_skill_releases",
    "handle_delete_skill_release",
    "handle_delete_skill_candidate",
    "handle_rollback_skill_release",
    "handle_declare_skill_goal",
    "handle_get_active_skill",
    "handle_report_skill_outcome",
    "handle_execute_browser",
    "handle_execute_browser_batch",
    "handle_list_profiles",
]

# Handler dispatch table: tool name -> handler function
TOOL_HANDLERS = {
    "create_sandbox": handle_create_sandbox,
    "delete_sandbox": handle_delete_sandbox,
    "execute_python": handle_execute_python,
    "execute_shell": handle_execute_shell,
    "read_file": handle_read_file,
    "write_file": handle_write_file,
    "list_files": handle_list_files,
    "delete_file": handle_delete_file,
    "get_execution_history": handle_get_execution_history,
    "get_execution": handle_get_execution,
    "get_last_execution": handle_get_last_execution,
    "annotate_execution": handle_annotate_execution,
    "create_skill_payload": handle_create_skill_payload,
    "get_skill_payload": handle_get_skill_payload,
    "create_skill_candidate": handle_create_skill_candidate,
    "evaluate_skill_candidate": handle_evaluate_skill_candidate,
    "promote_skill_candidate": handle_promote_skill_candidate,
    "list_skill_candidates": handle_list_skill_candidates,
    "list_skill_releases": handle_list_skill_releases,
    "delete_skill_release": handle_delete_skill_release,
    "delete_skill_candidate": handle_delete_skill_candidate,
    "rollback_skill_release": handle_rollback_skill_release,
    "declare_skill_goal": handle_declare_skill_goal,
    "get_active_skill": handle_get_active_skill,
    "report_skill_outcome": handle_report_skill_outcome,
    "execute_browser": handle_execute_browser,
    "execute_browser_batch": handle_execute_browser_batch,
    "upload_file": handle_upload_file,
    "download_file": handle_download_file,
    "list_profiles": handle_list_profiles,
}
