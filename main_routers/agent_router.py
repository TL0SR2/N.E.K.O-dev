# -*- coding: utf-8 -*-
"""
Agent Router

Handles agent-related endpoints including:
- Agent flags
- Health checks
- Task status
- Admin control
"""

import logging
import asyncio
import json

from fastapi import APIRouter, Request, Body
from fastapi.responses import JSONResponse
import httpx
from .shared_state import get_session_manager, get_config_manager
from config import TOOL_SERVER_PORT, USER_PLUGIN_SERVER_PORT, MAIN_AGENT_EVENT_PORT

router = APIRouter(prefix="/api/agent", tags=["agent"])
logger = logging.getLogger("Main")
_mq_server = None


async def _broadcast_ws_message(payload: dict, lanlan_name: str | None = None):
    session_manager = get_session_manager()
    targets = []
    if lanlan_name and lanlan_name in session_manager:
        targets = [session_manager[lanlan_name]]
    else:
        targets = list(session_manager.values())
    for mgr in targets:
        try:
            if mgr.websocket and hasattr(mgr.websocket, "client_state") and mgr.websocket.client_state == mgr.websocket.client_state.CONNECTED:
                await mgr.websocket.send_json(payload)
        except Exception:
            continue


async def _handle_main_event_client(reader: asyncio.StreamReader, writer: asyncio.StreamWriter):
    try:
        while True:
            raw = await reader.readline()
            if not raw:
                break
            try:
                event = json.loads(raw.decode("utf-8"))
            except Exception:
                continue
            if not isinstance(event, dict):
                continue

            etype = event.get("type")
            if etype == "agent_task_status":
                payload = event.get("payload") or {}
                # 主动推送任务状态到前端
                await _broadcast_ws_message({"type": "agent_task_status", "data": payload})
            elif etype == "task_result":
                text = (event.get("text") or "").strip()
                if not text:
                    continue
                lanlan = event.get("lanlan_name")
                _config_manager = get_config_manager()
                if not lanlan:
                    _, her_name_current, _, _, _, _, _, _, _, _ = _config_manager.get_character_data()
                    lanlan = her_name_current
                session_manager = get_session_manager()
                mgr = session_manager.get(lanlan)
                if mgr:
                    mgr.pending_extra_replies.append(text)
                await _broadcast_ws_message({"type": "agent_task_result", "text": text, "lanlan_name": lanlan}, lanlan)
    finally:
        writer.close()
        try:
            await writer.wait_closed()
        except Exception:
            pass


@router.on_event("startup")
async def _startup_agent_router():
    global _mq_server
    _mq_server = await asyncio.start_server(
        _handle_main_event_client,
        host="127.0.0.1",
        port=MAIN_AGENT_EVENT_PORT,
    )


@router.on_event("shutdown")
async def _shutdown_agent_router():
    global _mq_server
    if _mq_server is not None:
        _mq_server.close()
        try:
            await _mq_server.wait_closed()
        except Exception:
            pass
        _mq_server = None


@router.post('/flags')
async def update_agent_flags(request: Request):
    """来自前端的Agent开关更新，级联到各自的session manager。"""
    try:
        data = await request.json()
        _config_manager = get_config_manager()
        session_manager = get_session_manager()
        _, her_name_current, _, _, _, _, _, _, _, _ = _config_manager.get_character_data()
        lanlan = data.get('lanlan_name') or her_name_current
        flags = data.get('flags') or {}
        mgr = session_manager.get(lanlan)
        if not mgr:
            return JSONResponse({"success": False, "error": "lanlan not found"}, status_code=404)
        # Update core flags first
        mgr.update_agent_flags(flags)
        # Forward to tool server for MCP/Computer-Use flags
        try:
            forward_payload = {}
            if 'mcp_enabled' in flags:
                forward_payload['mcp_enabled'] = bool(flags['mcp_enabled'])
            if 'computer_use_enabled' in flags:
                forward_payload['computer_use_enabled'] = bool(flags['computer_use_enabled'])
            # Forward user_plugin_enabled as well so agent_server receives UI toggles
            if 'user_plugin_enabled' in flags:
                forward_payload['user_plugin_enabled'] = bool(flags['user_plugin_enabled'])
            if forward_payload:
                async with httpx.AsyncClient(timeout=0.7) as client:
                    r = await client.post(f"http://localhost:{TOOL_SERVER_PORT}/agent/flags", json=forward_payload)
                    if not r.is_success:
                        raise Exception(f"tool_server responded {r.status_code}")
        except Exception as e:
            # On failure, reset flags in core to safe state (include user_plugin flag)
            mgr.update_agent_flags({'agent_enabled': False, 'computer_use_enabled': False, 'mcp_enabled': False, 'user_plugin_enabled': False})
            return JSONResponse({"success": False, "error": f"tool_server forward failed: {e}"}, status_code=502)
        return {"success": True}
    except Exception as e:
        return JSONResponse({"success": False, "error": str(e)}, status_code=500)



@router.get('/flags')
async def get_agent_flags():
    """获取当前 agent flags 状态（供前端同步）"""
    try:
        async with httpx.AsyncClient(timeout=0.7) as client:
            r = await client.get(f"http://localhost:{TOOL_SERVER_PORT}/agent/flags")
            if not r.is_success:
                return JSONResponse({"success": False, "error": "tool_server down"}, status_code=502)
            return r.json()
    except Exception as e:
        return JSONResponse({"success": False, "error": str(e)}, status_code=502)



@router.get('/health')
async def agent_health():
    """Check tool_server health via main_server proxy."""
    try:
        async with httpx.AsyncClient(timeout=0.7) as client:
            r = await client.get(f"http://localhost:{TOOL_SERVER_PORT}/health")
            if not r.is_success:
                return JSONResponse({"status": "down"}, status_code=502)
            data = {}
            try:
                data = r.json()
            except Exception:
                pass
            return {"status": "ok", **({"tool": data} if isinstance(data, dict) else {})}
    except Exception:
        return JSONResponse({"status": "down"}, status_code=502)



@router.get('/computer_use/availability')
async def proxy_cu_availability():
    try:
        async with httpx.AsyncClient(timeout=1.5) as client:
            r = await client.get(f"http://localhost:{TOOL_SERVER_PORT}/computer_use/availability")
            if not r.is_success:
                return JSONResponse({"ready": False, "reasons": [f"tool_server responded {r.status_code}"]}, status_code=502)
            return r.json()
    except Exception as e:
        return JSONResponse({"ready": False, "reasons": [f"proxy error: {e}"]}, status_code=502)



@router.get('/mcp/availability')
async def proxy_mcp_availability():
    try:
        async with httpx.AsyncClient(timeout=1.5) as client:
            r = await client.get(f"http://localhost:{TOOL_SERVER_PORT}/mcp/availability")
            if not r.is_success:
                return JSONResponse({"ready": False, "reasons": [f"tool_server responded {r.status_code}"]}, status_code=502)
            return r.json()
    except Exception as e:
        return JSONResponse({"ready": False, "reasons": [f"proxy error: {e}"]}, status_code=502)


@router.get('/user_plugin/availability')
async def proxy_up_availability():
    try:
        async with httpx.AsyncClient(timeout=1.5) as client:
            r = await client.get(f"http://localhost:{USER_PLUGIN_SERVER_PORT}/available")
            if r.is_success:
                return JSONResponse({"ready": True, "reasons": ["user_plugin server reachable"]}, status_code=200)
            else:
                return JSONResponse({"ready": False, "reasons": [f"user_plugin server responded {r.status_code}"]}, status_code=502)
    except Exception as e:
        return JSONResponse({"ready": False, "reasons": [f"proxy error: {e}"]}, status_code=502)



@router.get('/tasks')
async def proxy_tasks():
    """Get all tasks from tool server via main_server proxy."""
    try:
        async with httpx.AsyncClient(timeout=2.5) as client:
            r = await client.get(f"http://localhost:{TOOL_SERVER_PORT}/tasks")
            if not r.is_success:
                return JSONResponse({"tasks": [], "error": f"tool_server responded {r.status_code}"}, status_code=502)
            return r.json()
    except Exception as e:
        return JSONResponse({"tasks": [], "error": f"proxy error: {e}"}, status_code=502)



@router.get('/tasks/{task_id}')
async def proxy_task_detail(task_id: str):
    """Get specific task details from tool server via main_server proxy."""
    try:
        async with httpx.AsyncClient(timeout=1.5) as client:
            r = await client.get(f"http://localhost:{TOOL_SERVER_PORT}/tasks/{task_id}")
            if not r.is_success:
                return JSONResponse({"error": f"tool_server responded {r.status_code}"}, status_code=502)
            return r.json()
    except Exception as e:
        return JSONResponse({"error": f"proxy error: {e}"}, status_code=502)


@router.post('/admin/control')
async def proxy_admin_control(payload: dict = Body(...)):
    """Proxy admin control commands to tool server."""
    try:
        import httpx
        async with httpx.AsyncClient(timeout=5.0) as client:
            r = await client.post(f"http://localhost:{TOOL_SERVER_PORT}/admin/control", json=payload)
            if not r.is_success:
                return JSONResponse({"success": False, "error": f"tool_server responded {r.status_code}"}, status_code=502)
            
            result = r.json()
            logger.info(f"Admin control result: {result}")
            return result
        
    except Exception as e:
        return JSONResponse({
            "success": False,
            "error": f"Failed to execute admin control: {str(e)}"
        }, status_code=500)

@router.post('/notify_task_result')
async def notify_task_result(request: Request):
    """供工具/任务服务回调：在下一次正常回复之后，插入一条任务完成提示。"""
    try:
        _config_manager = get_config_manager()
        session_manager = get_session_manager()
        data = await request.json()
        # 如果未显式提供，则使用当前默认角色
        _, her_name_current, _, _, _, _, _, _, _, _ = _config_manager.get_character_data()
        lanlan = data.get('lanlan_name') or her_name_current
        text = (data.get('text') or '').strip()
        if not text:
            return JSONResponse({"success": False, "error": "text required"}, status_code=400)
        mgr = session_manager.get(lanlan)
        if not mgr:
            return JSONResponse({"success": False, "error": "lanlan not found"}, status_code=404)
        # 将提示加入待插入队列
        mgr.pending_extra_replies.append(text)
        return {"success": True}
    except Exception as e:
        return JSONResponse({"success": False, "error": str(e)}, status_code=500)

