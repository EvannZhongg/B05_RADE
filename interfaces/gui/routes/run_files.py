"""Read-only browsing of files belonging to a run."""

import subprocess
import sys

from fastapi import APIRouter, HTTPException

from agent_core import JsonlSessionStore, ListDirectoryTool, Session, ToolExecutionContext, Workspace
from interfaces.gui.windows_folders import open_windows_folder


def run_files(store: JsonlSessionStore, session_id: str) -> Workspace:
    try:
        path = store.session_directory(session_id) / "files"
        if path.resolve() != path or not path.is_dir():
            raise ValueError("Run files are unavailable")
        return Workspace(path)
    except (OSError, ValueError) as error:
        raise HTTPException(status_code=404, detail="运行目录不存在。") from error


def create_run_files_router(store: JsonlSessionStore) -> APIRouter:
    router = APIRouter()

    @router.get("/api/sessions/{session_id}/files")
    def list_files(session_id: str, path: str = ".", cursor: str | None = None) -> dict[str, object]:
        directory = run_files(store, session_id)
        arguments = {"path": path}
        if cursor is not None:
            arguments["cursor"] = cursor
        try:
            return ListDirectoryTool().execute(
                arguments,
                ToolExecutionContext(workspace=directory, session=Session(session_id)),
            )
        except (OSError, ValueError) as error:
            raise HTTPException(status_code=400, detail=str(error)) from error

    @router.post("/api/sessions/{session_id}/files/open", status_code=204)
    def open_folder(session_id: str) -> None:
        directory = run_files(store, session_id).path
        try:
            if sys.platform == "win32":
                open_windows_folder(directory)
            elif sys.platform == "darwin":
                subprocess.Popen(["open", "-a", "Finder", str(directory)])
            else:
                subprocess.Popen(["xdg-open", str(directory)])
        except OSError as error:
            raise HTTPException(status_code=500, detail=str(error)) from error

    return router
