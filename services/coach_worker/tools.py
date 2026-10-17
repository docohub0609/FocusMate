"""Reuse existing isolated tools."""
from focusmate.adapters.tool_storage import SQLiteToolBackend
from focusmate.adapters.tool_ledger import SQLiteToolLedger
from focusmate.adapters.manager_runs import ManagerRunLookup
from focusmate.core.tool_gateway import ToolGateway
from services.oj_worker.tool_public_runs import PublicRunService
from services.oj_worker.local_runs import LocalRunService

def create_tools(service):
    root=service.manager_path.parent/(service.manager_path.stem+'-tools')
    public=PublicRunService(service.path,root/'public')
    local=LocalRunService(root/'local')
    return ToolGateway(SQLiteToolBackend(service.path,public,local),SQLiteToolLedger(root/'calls.sqlite3')),ManagerRunLookup(public,local)
