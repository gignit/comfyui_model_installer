"""HTTP routes for the Model Installer custom node."""

import os
import urllib
import logging
from aiohttp import web
import aiohttp
from server import PromptServer

from .model_installer import (
    ModelInstaller, 
    resolve_folder_name_from_url, 
    get_primary_folder_path, 
    safe_join
)


def register_routes():
    """Register all Model Installer HTTP routes with ComfyUI server."""
    routes = PromptServer.instance.routes
    
    # Get client session accessor
    def get_session():
        return PromptServer.instance.client_session
    
    installer = ModelInstaller(get_session)

    @routes.get("/model_installer/health")
    async def health_check(request):
        """Health check endpoint for frontend feature detection."""
        stats = installer.get_workflow_validation_stats()
        return web.json_response({
            "ok": True,
            "version": "1.0.0",
            "name": "ComfyUI Model Installer",
            "validator_stats": stats
        })

    @routes.get("/models/expected_size")
    async def get_expected_size(request):
        """Get expected file size for a URL."""
        url = request.rel_url.query.get("url")
        if not url:
            return web.json_response({"error": "missing url"}, status=400)
        expected = await installer.expected_size(url)
        return web.json_response({"expected": expected})

    @routes.get("/models/status")
    async def get_model_status(request):
        """Get status of a model file (present, downloading, etc.)."""
        url = request.rel_url.query.get("url")
        folder_hint = request.rel_url.query.get("directory")
        filename = request.rel_url.query.get("filename")
        if not url and not (folder_hint and filename):
            return web.json_response({"error": "missing url or directory+filename"}, status=400)
        folder_name = folder_hint or resolve_folder_name_from_url(url or "")
        if not folder_name:
            return web.json_response({"present": False, "reason": "unknown_folder"})
        base = get_primary_folder_path(folder_name)
        if not base:
            return web.json_response({"present": False, "reason": "missing_base_dir"})
        if not filename and url:
            filename = os.path.basename(urllib.parse.urlparse(url).path)
        if not filename:
            return web.json_response({"present": False, "reason": "unknown_filename"})
        try:
            target = safe_join(base, filename)
        except Exception:
            return web.json_response({"present": False, "reason": "unsafe_path"})
        present = os.path.isfile(target)
        size = os.path.getsize(target) if present else 0
        expected = installer.active_expected(target)
        if expected > 0:
            state = "downloading" if size < expected else "installed"
        else:
            state = "installed" if present else "absent"
        logging.debug(f"[Model Installer] status: folder={folder_name} filename={filename} present={present} size={size} expected={expected} state={state}")
        return web.json_response({"present": present, "size": size, "expected": expected, "state": state, "folder": folder_name, "path": target})

    @routes.post("/models/install")
    async def post_model_install(request):
        """Install (download) a model file."""
        try:
            body = await request.json()
        except Exception:
            return web.json_response({"error": "invalid_json"}, status=400)
        url = body.get("url")
        folder_hint = body.get("directory")
        filename = body.get("filename")
        if not url:
            logging.warning("[Model Installer] install: missing url in request")
            return web.json_response({"error": "missing url"}, status=400)
        folder_name = folder_hint or resolve_folder_name_from_url(url)
        if not folder_name:
            logging.warning(f"[Model Installer] install: unknown folder for url={url}")
            return web.json_response({"error": "unknown_folder"}, status=400)
        base = get_primary_folder_path(folder_name)
        if not base:
            logging.warning(f"[Model Installer] install: missing base dir for folder={folder_name}")
            return web.json_response({"error": "missing_base_dir"}, status=400)
        if not filename:
            filename = os.path.basename(urllib.parse.urlparse(url or '').path)
        if not filename:
            logging.warning("[Model Installer] install: missing filename after parsing url")
            return web.json_response({"error": "missing filename"}, status=400)
        try:
            dest = safe_join(base, filename)
        except Exception:
            logging.warning(f"[Model Installer] install: unsafe path base={base} filename={filename}")
            return web.json_response({"error": "unsafe_path"}, status=400)
        # SECURITY: Validate model against workflow templates
        if not installer.validate_model_request(url, folder_name, filename):
            logging.warning(f"[Model Installer] install: model not found in workflows - {folder_name}/{filename} from {url}")
            return web.json_response({"error": "Model not found in any workflow template"}, status=403)
        
        try:
            logging.info(f"[Model Installer] install: starting folder={folder_name} filename={filename}")
            # For HF URLs, if token missing or unauthorized, return 401 early
            if installer._is_hf_url(url):
                ok = await installer.check_auth(url)
                if not ok:
                    return web.json_response(
                        {
                            "error_code": "auth_required",
                            "provider": "huggingface",
                            "message": "Authentication required to download this file.",
                            "cli_hint": "hf auth login --token <token> --add-to-git-credential",
                        },
                        status=401,
                    )
            # Queue download asynchronously and return immediately
            installer.queue_download(url, dest)
            # Initialize expected size for immediate UI progress if possible
            try:
                expected = await installer.expected_size(url)
            except Exception:
                expected = 0
            if expected > 0:
                installer._active_downloads[dest] = expected
            return web.json_response({"status": "queued", "folder": folder_name, "path": dest, "expected": expected})
        except aiohttp.ClientResponseError as cre:
            # Specific authentication handling for Hugging Face
            if cre.status in (401, 403) and "huggingface.co" in (url or ""):
                return web.json_response(
                    {
                        "error_code": "auth_required",
                        "provider": "huggingface",
                        "message": "Authentication required to download this file.",
                        "cli_hint": "hf auth login --token <token> --add-to-git-credential",
                    },
                    status=401,
                )
            status = cre.status if isinstance(cre.status, int) else 500
            logging.error(f"[Model Installer] install http error: {cre}")
            return web.json_response({"error": str(cre), "status": status}, status=500)
        except Exception as e:
            logging.error(f"[Model Installer] install error: {e}")
            return web.json_response({"error": str(e)}, status=500)

    @routes.post("/models/uninstall")
    async def post_model_uninstall(request):
        """Uninstall (delete) a model file."""
        try:
            body = await request.json()
        except Exception:
            return web.json_response({"error": "invalid_json"}, status=400)
        folder_name = body.get("directory") or resolve_folder_name_from_url(body.get("url") or "")
        filename = body.get("filename")
        url = body.get("url")
        if not filename and url:
            filename = os.path.basename(urllib.parse.urlparse(url).path)
        if not (folder_name and filename):
            return web.json_response({"error": "missing directory/filename"}, status=400)
        base = get_primary_folder_path(folder_name)
        if not base:
            return web.json_response({"error": "missing_base_dir"}, status=400)
        try:
            target = safe_join(base, filename)
        except Exception:
            return web.json_response({"error": "unsafe_path"}, status=400)
        if os.path.isfile(target):
            try:
                logging.info(f"[Model Installer] uninstall: removing folder={folder_name} filename={filename}")
                os.remove(target)
                logging.info(f"[Model Installer] uninstall: removed {target}")
                return web.json_response({"status": "uninstalled", "folder": folder_name, "path": target})
            except Exception as e:
                logging.error(f"[Model Installer] uninstall error: {e}")
                return web.json_response({"error": str(e)}, status=500)
        else:
            logging.info(f"[Model Installer] uninstall: file not present folder={folder_name} filename={filename}")
            return web.json_response({"status": "absent", "folder": folder_name, "path": target})

    # --- Hugging Face authentication endpoints ---
    @routes.get("/auth/hf_status")
    async def get_hf_status(request):
        """Get Hugging Face authentication status."""
        try:
            from huggingface_hub import HfFolder
            token = HfFolder.get_token()
            return web.json_response({"authenticated": bool(token)})
        except Exception:
            return web.json_response({"authenticated": False})

    @routes.post("/auth/hf_login")
    async def post_hf_login(request):
        """Save Hugging Face authentication token."""
        try:
            body = await request.json()
        except Exception:
            return web.json_response({"error": "invalid_json"}, status=400)
        token = (body or {}).get("token", "").strip()
        if not token:
            return web.json_response({"error": "missing token"}, status=400)
        try:
            from huggingface_hub import HfFolder
        except Exception as e:
            return web.json_response({"error": f"huggingface_hub not installed: {e}"}, status=500)
        try:
            HfFolder.save_token(token)
            return web.json_response({"status": "authenticated"})
        except Exception as e:
            return web.json_response({"error": str(e)}, status=500)

    # Initialize the workflow validation system
    installer.initialize_workflow_validation()
    logging.info("[Model Installer] Routes registered successfully")


def try_register_routes():
    """Safely try to register routes if running inside ComfyUI."""
    try:
        register_routes()
    except Exception as e:
        logging.warning(f"[Model Installer] Route registration skipped: {e}")
