"""HTTP routes for the Model Installer custom node."""

import os
import urllib
import logging
import asyncio
from aiohttp import web
import aiohttp
from server import PromptServer

from .model_installer import ModelInstaller
from .config import is_uninstall_enabled


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
        storage_info = ModelInstaller.get_storage_info()
        return web.json_response({
            "ok": True,
            "version": "1.0.0",
            "name": "ComfyUI Model Installer",
            "validator_stats": stats,
            "storage_info": storage_info,
            "features": {
                "allow_uninstall": is_uninstall_enabled(),
                "multi_path_selection": True
            }
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
        folder_name = folder_hint
        if not folder_name:
            return web.json_response({"present": False, "reason": "directory_required"})
        if not filename and url:
            filename = os.path.basename(urllib.parse.urlparse(url).path)
        if not filename:
            return web.json_response({"present": False, "reason": "unknown_filename"})
        
        # Use ComfyUI's native function to find the model
        import folder_paths
        model_path = folder_paths.get_full_path(folder_name, filename)
        
        if model_path:
            # Model found - return status with path
            size = os.path.getsize(model_path)
            expected = installer.active_expected(model_path)
            if expected > 0:
                state = "downloading" if size < expected else "installed"
            else:
                state = "installed"
            logging.debug(f"[Model Installer] status: folder={folder_name} filename={filename} present=True size={size} expected={expected} state={state}")
            return web.json_response({
                "present": True,
                "size": size,
                "expected": expected,
                "state": state,
                "folder": folder_name,
                "path": model_path
            })
        else:
            # Model not found - check if there's a download failure
            # Try to construct the expected download path to check for failures
            try:
                # Check all possible paths for this folder type for download failures
                paths = installer.get_model_paths(folder_name)
                for path_info in paths:
                    expected_dest = ModelInstaller.safe_join(path_info["path"], filename)
                    failure_msg = installer.get_download_failure(expected_dest)
                    if failure_msg:
                        logging.debug(f"[Model Installer] status: folder={folder_name} filename={filename} present=False state=failed")
                        return web.json_response({
                            "present": False,
                            "size": 0,
                            "expected": 0,
                            "state": "failed",
                            "folder": folder_name,
                            "error": failure_msg,
                            "storage_info": ModelInstaller.get_storage_info(folder_name)
                        })
            except Exception:
                pass  # If we can't construct path, continue with normal absent response
            
            # Model not found - return storage info for this folder type
            storage_info = ModelInstaller.get_storage_info(folder_name)
            logging.debug(f"[Model Installer] status: folder={folder_name} filename={filename} present=False")
            return web.json_response({
                "present": False,
                "size": 0,
                "expected": 0,
                "state": "absent",
                "folder": folder_name,
                "storage_info": storage_info
            })

    @routes.post("/models/install")
    async def post_model_install(request):
        """Install (download) a model file."""
        try:
            body = await request.json()
        except Exception:
            return web.json_response({"error": "invalid_json"}, status=400)
        
        # Extract structured request fields
        name = body.get("name")           # e.g., "model.safetensors"
        directory = body.get("directory") # e.g., "text_encoders" 
        url = body.get("url")
        user_path = body.get("path")      # Optional user selection
        
        # Validate required fields
        if not all([name, directory, url]):
            logging.warning("[Model Installer] install: missing required fields")
            return web.json_response({"error": "Missing required fields: name, directory, url"}, status=400)
        
        # Zero-trust: Validate all client info against templates
        if not installer.validate_model_request(url, directory, name):
            logging.warning(f"[Model Installer] install: model not found in workflows - {directory}/{name} from {url}")
            return web.json_response({"error": "Model not found in workflow templates"}, status=403)
        
        # Use directory as folder_name (ComfyUI terminology)
        folder_name = directory
        
        # Get expected size for storage validation
        try:
            expected_size = await installer.expected_size(url)
        except (asyncio.TimeoutError, aiohttp.ConnectionTimeoutError):
            logging.error(f"[Model Installer] install: timeout getting file size for {url}")
            return web.json_response({"error": "Connection timeout - unable to get file size. Check your network connection or proxy settings."}, status=408)
        except aiohttp.ClientConnectorError as e:
            logging.error(f"[Model Installer] install: connection failed getting file size for {url}: {e}")
            return web.json_response({"error": f"Connection failed: {str(e)}. Check your network connection or proxy settings."}, status=503)
        except Exception as e:
            logging.warning(f"[Model Installer] install: could not get expected size for {url}: {e}")
            expected_size = 0  # Continue without size validation
        
        # Path selection and validation
        if user_path:
            # Validate user-selected path
            valid, error_msg = installer.validate_install_path(folder_name, user_path, expected_size)
            if not valid:
                logging.warning(f"[Model Installer] install: {error_msg}")
                return web.json_response({"error": error_msg}, status=400)
            base = user_path
        else:
            # Auto-select best path (most free space)
            base = ModelInstaller.choose_free_path(folder_name)
            if not base:
                logging.warning(f"[Model Installer] install: no valid paths found for {folder_name}")
                return web.json_response({"error": f"No valid paths found for {folder_name}"}, status=400)
            
            # Check storage on auto-selected path
            valid, error_msg = installer.validate_install_path(folder_name, base, expected_size)
            if not valid:
                logging.warning(f"[Model Installer] install: {error_msg}")
                return web.json_response({"error": error_msg}, status=400)
        
        # Continue with existing download logic
        try:
            dest = ModelInstaller.safe_join(base, name)
        except Exception:
            logging.warning(f"[Model Installer] install: unsafe path base={base} name={name}")
            return web.json_response({"error": "unsafe_path"}, status=400)
        
        try:
            logging.info(f"[Model Installer] install: starting folder={folder_name} name={name}")
            # For HF URLs, if token missing or unauthorized, return 401 early
            if installer._is_hf_url(url):
                try:
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
                except (asyncio.TimeoutError, aiohttp.ConnectionTimeoutError):
                    logging.error(f"[Model Installer] install: timeout during auth check for {url}")
                    return web.json_response({"error": "Connection timeout during authentication check. Check your network connection or proxy settings."}, status=408)
                except aiohttp.ClientConnectorError as e:
                    logging.error(f"[Model Installer] install: connection failed during auth check for {url}: {e}")
                    return web.json_response({"error": f"Connection failed during authentication check: {str(e)}. Check your network connection or proxy settings."}, status=503)
            # Queue download asynchronously and return immediately
            installer.queue_download(url, dest)
            # Use the expected_size we already got (avoid second network call)
            if expected_size > 0:
                installer._active_downloads[dest] = expected_size
            return web.json_response({"status": "queued", "folder": folder_name, "path": dest, "expected": expected_size})
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
        # Check if uninstall feature is enabled
        if not is_uninstall_enabled():
            return web.json_response({"error": "Uninstall not enabled on this server."}, status=403)
        
        try:
            body = await request.json()
        except Exception:
            return web.json_response({"error": "invalid_json"}, status=400)
        folder_name = body.get("directory")
        if not folder_name:
            return web.json_response({"error": "directory field required"}, status=400)
        filename = body.get("filename")
        url = body.get("url")
        if not filename and url:
            filename = os.path.basename(urllib.parse.urlparse(url).path)
        if not (folder_name and filename):
            return web.json_response({"error": "missing directory/filename"}, status=400)
        base = get_best_folder_path(folder_name)
        if not base:
            return web.json_response({"error": "missing_base_dir"}, status=400)
        try:
            target = ModelInstaller.safe_join(base, filename)
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
    
    # Note: ComfyUI doesn't have a standard feature_flags system
    # The frontend will detect uninstall capability by trying the health endpoint
    
    logging.info("[Model Installer] Routes registered successfully")


def try_register_routes():
    """Safely try to register routes if running inside ComfyUI."""
    try:
        register_routes()
    except Exception as e:
        logging.warning(f"[Model Installer] Route registration skipped: {e}")
