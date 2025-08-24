from __future__ import annotations

import os
import urllib
import logging
from urllib.parse import urlparse
from typing import Callable, Any, Dict, Optional
import asyncio
import aiohttp
import json

from pathlib import Path

import folder_paths
from .config import get_download_config


class ModelInstaller:
    """Handles model downloading and installation."""
    
    def __init__(self, get_client_session: Callable[[], Any]):
        # Lazy accessor because the aiohttp ClientSession is created after server init
        self._get_client_session = get_client_session
        self._active_downloads: dict[str, int] = {}
        self._download_failures: dict[str, str] = {}  # dest_path -> error_message
        self._download_session: Optional[aiohttp.ClientSession] = None
        
        # Workflow validation system
        self._workflow_index: Optional[Dict] = None
        self._index_file = Path(__file__).parent / "workflow_model_index.json"
        self._index_loaded_this_session = False

    def _get_download_session(self) -> aiohttp.ClientSession:
        """Get or create a properly configured download session with timeouts and proxy support."""
        if self._download_session is None or self._download_session.closed:
            # Get configuration
            config = get_download_config()
            
            # Create timeout configuration
            timeout = aiohttp.ClientTimeout(
                total=config["timeout_total"],  # None = no total timeout for large files
                connect=config["timeout_connect"]
            )
            
            # Create connector with configured settings - extract only TCPConnector parameters
            connector_params = {k: v for k, v in config.items() 
                              if k in ['limit', 'limit_per_host', 'ttl_dns_cache', 'use_dns_cache']}
            connector = aiohttp.TCPConnector(**connector_params)
            
            # Create session with timeout and connector (use aiohttp default user-agent)
            self._download_session = aiohttp.ClientSession(
                timeout=timeout,
                connector=connector,
                trust_env=config["trust_env"]  # Use proxy settings from environment variables
                # No custom headers - let aiohttp use its default user-agent
            )
            
        return self._download_session

    async def cleanup(self):
        """Clean up resources (close download session)."""
        if self._download_session and not self._download_session.closed:
            await self._download_session.close()
            self._download_session = None



    @staticmethod
    def get_model_paths() -> Dict[str, tuple[list[str], set[str]]]:
        """
        Filter ComfyUI's native folder paths to exclude output directories and non-existent legacy paths.
        
        Applies intelligent filtering:
        1. For single paths: Always keeps them (must have at least one option)
        2. For multiple paths: Excludes output directories and non-existent legacy paths
        3. Legacy paths (via map_legacy) are only excluded if they don't exist on disk
        
        Returns:
            Dict mapping folder names to (paths_list, extensions) tuples
        """
        
        # Get ComfyUI's models directory and output directory
        models_dir = folder_paths.models_dir
        output_dir = folder_paths.get_output_directory()
        output_dir_normalized = os.path.normpath(output_dir)
        
        filtered_paths = {}
        
        # Use ComfyUI's native folder_names_and_paths
        for folder_name, folder_info in folder_paths.folder_names_and_paths.items():
            paths = folder_info[0]  # (paths_list, extensions)
            extensions = folder_info[1]
            
            if not paths:
                continue
                
            # If there's only one path, return it
            if len(paths) == 1:
                filtered_paths[folder_name] = folder_info
                continue
                
            # For multiple paths, apply legacy and output filtering
            filtered_path_list = []
            for path in paths:
                path_normalized = os.path.normpath(path)
                
                # Skip paths under output directory
                if path_normalized.startswith(output_dir_normalized):
                    continue
                
                # Check if this is a legacy path that doesn't exist
                path_dir_name = os.path.basename(path_normalized)
                
                # Check if this directory name maps to the current folder_name via legacy mapping
                mapped_folder_name = folder_paths.map_legacy(path_dir_name)
                is_legacy_path = (mapped_folder_name == folder_name and path_dir_name != folder_name)
                
                if is_legacy_path:
                    # This is a legacy path - only include if it exists on disk
                    if os.path.exists(path):
                        filtered_path_list.append(path)
                    # If legacy path doesn't exist, skip it (don't include in list)
                else:
                    # Not a legacy path (modern or custom) - always include
                    filtered_path_list.append(path)
            
            # Only include if we have paths remaining after filtering
            if filtered_path_list:
                filtered_paths[folder_name] = (filtered_path_list, extensions)
        
        return filtered_paths

    @staticmethod
    def choose_free_path(folder_name: str) -> str | None:
        """
        Get the best path for a folder name, choosing the one with most free disk space.
        
        This function uses ComfyUI's native path building logic, excluding output directories,
        and respects extra_model_paths.yaml by checking all available paths for the given 
        model type and selecting the one with the most available space.
        
        Args:
            folder_name: The folder type (e.g., "checkpoints", "vae", "loras")
            
        Returns:
            Path with most free space, or None if no valid paths found
        """
        import shutil
        
        # Use filtered model paths (excludes output paths)
        model_paths = ModelInstaller.get_model_paths()
        entry = model_paths.get(folder_name)
        if not entry:
            logging.warning(f"[Model Installer] No folder paths found for '{folder_name}'")
            return None
        
        paths = entry[0]
        if not paths:
            logging.warning(f"[Model Installer] Empty path list for '{folder_name}'")
            return None
        
        best_path = None
        most_free = -1
        
        for path in paths:
            try:
                # Find closest existing parent for storage check (no makedirs)
                check_path = path
                while not os.path.exists(check_path) and check_path != os.path.dirname(check_path):
                    check_path = os.path.dirname(check_path)
                
                if not os.path.exists(check_path):
                    logging.warning(f"[Model Installer] Cannot find existing parent directory for {path}")
                    continue
                
                # Get free disk space from existing parent
                free_bytes = shutil.disk_usage(check_path).free
                free_gb = free_bytes / (1024**3)  # Convert to GB for logging
                
                logging.debug(f"[Model Installer] Path '{path}' has {free_gb:.1f} GB free")
                
                if free_bytes > most_free:
                    best_path = path
                    most_free = free_bytes
                    
            except Exception as e:
                logging.warning(f"[Model Installer] Cannot access path '{path}': {e}")
                continue
        
        if best_path:
            free_gb = most_free / (1024**3)
            logging.info(f"[Model Installer] Selected path '{best_path}' with {free_gb:.1f} GB free space")
        else:
            logging.error(f"[Model Installer] No accessible paths found for '{folder_name}'")
        
        return best_path

    @staticmethod
    def get_storage_info(folder_name: str = None) -> Dict[str, list[Dict]]:
        """Get storage info for ComfyUI folder types, excluding output directories.
        
        Args:
            folder_name: If provided, only return storage info for this folder type.
                        If None, return info for all folder types.
        """
        import shutil
        result = {}
        
        # Use filtered model paths (excludes output paths)
        model_paths = ModelInstaller.get_model_paths()
        
        # Filter to specific folder_name if provided
        if folder_name:
            if folder_name not in model_paths:
                return {}
            items_to_process = {folder_name: model_paths[folder_name]}
        else:
            items_to_process = model_paths
        
        for current_folder_name, folder_info in items_to_process.items():
            paths = folder_info[0]  # (paths_list, extensions)
            if not paths:
                continue
                
            path_info = []
            for path in paths:
                try:
                    # Find closest existing parent for storage check (no makedirs)
                    check_path = path
                    while not os.path.exists(check_path) and check_path != os.path.dirname(check_path):
                        check_path = os.path.dirname(check_path)
                    
                    if os.path.exists(check_path):
                        usage = shutil.disk_usage(check_path)
                        path_info.append({
                            "path": path,                           # Full intended path
                            "total_bytes": usage.total,
                            "used_bytes": usage.total - usage.free,
                            "available_bytes": usage.free
                        })
                except Exception as e:
                    logging.warning(f"[Model Installer] Cannot get storage for {path}: {e}")
                    continue
            
            if path_info:
                # Preserve ComfyUI's native path order (no sorting by available space)
                # The frontend can display storage info, but order follows ComfyUI's preferences
                result[current_folder_name] = path_info  # Key is folder_name (e.g., "text_encoders")
        
        return result

    @staticmethod
    def safe_join(base: str, filename: str) -> str:
        """Safely join base path and filename, preventing directory traversal."""
        dest = os.path.abspath(os.path.join(base, filename))
        if os.path.commonpath((dest, os.path.abspath(base))) != os.path.abspath(base):
            raise ValueError("Unsafe path")
        return dest

    async def expected_size(self, url: str) -> int:
        """Get expected file size for a URL."""
        headers = {}
        if self._is_hf_url(url):
            token = self._get_hf_token()
            if token:
                headers["Authorization"] = f"Bearer {token}"
        return await self._expected_size_http(url, headers or None)

    async def check_auth(self, url: str) -> bool:
        """For HF URLs, verify that current token (if any) has access.
        Returns True if authorized or not HF; False if 401/403.
        """
        if not self._is_hf_url(url):
            return True
        headers = {}
        token = self._get_hf_token()
        if token:
            headers["Authorization"] = f"Bearer {token}"
        try:
            sess = self._get_download_session()
            async with sess.head(url, headers=headers or None) as resp:
                if resp.status in (401, 403):
                    return False
                if resp.status // 100 == 2:
                    return True
            # Fallback to range GET for some endpoints
            req_headers = {"Range": "bytes=0-0"}
            if headers:
                req_headers.update(headers)
            async with sess.get(url, headers=req_headers) as resp:
                if resp.status in (401, 403):
                    return False
                return True
        except Exception:
            # If network error, allow queueing and let background task surface issues
            return True

    async def download(self, url: str, dest_path: str) -> int:
        """Download a file from URL to destination path."""
        logging.info(f"[Model Installer] download: requesting url={url} -> {dest_path}")
        
        try:
            headers = {}
            if self._is_hf_url(url):
                token = self._get_hf_token()
                if token:
                    headers["Authorization"] = f"Bearer {token}"
                
            # Try to prefetch expected bytes for progress tracking
            try:
                expected = await self._expected_size_http(url, headers or None)
                if expected > 0:
                    self._active_downloads[dest_path] = expected
                    logging.info(f"[Model Installer] download: expected_size={expected} bytes")
            except Exception as e:
                logging.debug(f"[Model Installer] download: could not get expected size: {e}")
                
            sess = self._get_download_session()
            async with sess.get(url, headers=headers or None) as resp:
                resp.raise_for_status()
                    
                if dest_path not in self._active_downloads:
                    cl = int(resp.headers.get("Content-Length", 0))
                    if cl:
                        self._active_downloads[dest_path] = cl
                    
                os.makedirs(os.path.dirname(dest_path), exist_ok=True)
                total = 0
                    
                with open(dest_path, "wb") as f:
                    # Use configured chunk size
                    config = get_download_config()
                    async for chunk in resp.content.iter_chunked(config["chunk_size"]):
                        if chunk:
                            f.write(chunk)
                            total += len(chunk)
                    
                logging.info(f"[Model Installer] download: completed bytes={total}")
                self._active_downloads.pop(dest_path, None)
                return total
                
        except (asyncio.TimeoutError, aiohttp.ConnectionTimeoutError):
            config = get_download_config()
            timeout_msg = f"{config['timeout_connect']}s connection timeout" if config['timeout_total'] is None else f"{config['timeout_total']}s total timeout"
            error_msg = f"Download timeout: {timeout_msg}"
            logging.error(f"[Model Installer] download failed: {error_msg} for {url}")
            self._active_downloads.pop(dest_path, None)
            # Clean up partial file
            if os.path.exists(dest_path):
                try:
                    os.remove(dest_path)
                except Exception:
                    pass
            raise Exception(error_msg)  # Use simple Exception instead of ClientResponseError
            
        except aiohttp.ClientConnectorError as e:
            error_msg = f"Connection failed: {str(e)}"
            logging.error(f"[Model Installer] download failed: {error_msg} for {url}")
            self._active_downloads.pop(dest_path, None)
            # Clean up partial file
            if os.path.exists(dest_path):
                try:
                    os.remove(dest_path)
                except Exception:
                    pass
            raise Exception(error_msg)  # Use simple Exception instead of ClientResponseError
            
        except Exception as e:
            error_msg = f"Download failed: {str(e)}"
            logging.error(f"[Model Installer] download failed: {error_msg} for {url}")
            self._active_downloads.pop(dest_path, None)
            # Clean up partial file
            if os.path.exists(dest_path):
                try:
                    os.remove(dest_path)
                except Exception:
                    pass
            raise

    def _is_hf_url(self, url: str) -> bool:
        """Check if URL is from Hugging Face."""
        try:
            host = urlparse(url).hostname or ""
            return host.endswith("huggingface.co")
        except Exception:
            return False

    async def _download_with_hf_cli(self, url: str, dest_path: str) -> int:
        # No longer used; kept for reference only.
        raise aiohttp.ClientResponseError(
            request_info=None,
            history=(),
            status=501,
            message="HF CLI download disabled; streaming with token is used instead.",
            headers=None,
        )

    def _parse_hf(self, url: str) -> tuple[str, str, str]:
        """Parse Hugging Face URL to extract repo_id, file_path, and revision."""
        parsed = urlparse(url)
        parts = [p for p in parsed.path.split("/") if p]
        if len(parts) < 2:
            raise aiohttp.ClientResponseError(
                request_info=None, history=(), status=400, message="Invalid Hugging Face URL", headers=None
            )
        org, repo = parts[0], parts[1]
        repo_id = f"{org}/{repo}"
        revision = "main"
        file_path = None
        if "resolve" in parts:
            idx = parts.index("resolve")
            if idx + 1 < len(parts):
                revision = parts[idx + 1]
            file_path = "/".join(parts[idx + 2 :])
        elif "blob" in parts:
            idx = parts.index("blob")
            if idx + 1 < len(parts):
                revision = parts[idx + 1]
            file_path = "/".join(parts[idx + 2 :])
        elif "raw" in parts:
            idx = parts.index("raw")
            if idx + 1 < len(parts):
                revision = parts[idx + 1]
            file_path = "/".join(parts[idx + 2 :])
        else:
            file_path = parts[-1] if len(parts) >= 3 else parts[-1]
        if not file_path:
            raise aiohttp.ClientResponseError(
                request_info=None, history=(), status=400, message="Invalid Hugging Face URL (file path)", headers=None
            )
        return repo_id, file_path, revision

    def active_expected(self, dest_path: str) -> int:
        """Get expected size for an active download."""
        return self._active_downloads.get(dest_path, 0)
    
    def get_download_failure(self, dest_path: str) -> Optional[str]:
        """Get error message for a failed download."""
        return self._download_failures.get(dest_path)
    
    def clear_download_failure(self, dest_path: str) -> None:
        """Clear error message for a download (for retry)."""
        self._download_failures.pop(dest_path, None)
    
    def clear_all_download_failures(self) -> None:
        """Clear all download failure messages."""
        self._download_failures.clear()

    def _get_hf_token(self) -> str | None:
        """Get Hugging Face token from standard location."""
        try:
            from huggingface_hub import HfFolder
            return HfFolder.get_token()
        except Exception:
            return None

    async def _expected_size_http(self, url: str, headers: dict | None) -> int:
        """Get expected file size via HTTP HEAD or range request."""
        try:
            sess = self._get_download_session()
            async with sess.head(url, headers=headers or None) as resp:
                if resp.status // 100 == 2:
                    cl = resp.headers.get("Content-Length")
                    if cl and cl.isdigit():
                        return int(cl)
            req_headers = {"Range": "bytes=0-0"}
            if headers:
                req_headers.update(headers)
            async with sess.get(url, headers=req_headers) as resp:
                cr = resp.headers.get("Content-Range")
                if cr and "/" in cr:
                    total = cr.split("/")[-1]
                    if total.isdigit():
                        return int(total)
        except (asyncio.TimeoutError, aiohttp.ConnectionTimeoutError):
            # Let timeout exceptions propagate to caller for proper error handling
            raise
        except Exception as e:
            logging.warning(f"[Model Installer] expected_size error: {e}")
        return 0

    def queue_download(self, url: str, dest_path: str) -> None:
        """Start the download in the background."""
        # Clear any previous failure for retry
        self.clear_download_failure(dest_path)
        
        async def download_task():
            try:
                await self.download(url, dest_path)
                logging.info(f"[Model Installer] download completed successfully: {dest_path}")
            except Exception as e:
                error_msg = str(e)
                if hasattr(e, 'message'):
                    error_msg = e.message
                self._download_failures[dest_path] = error_msg
                logging.error(f"[Model Installer] download failed: {error_msg} for {dest_path}")
        
        try:
            asyncio.create_task(download_task())
        except Exception as e:
            error_msg = f"Failed to queue download: {e}"
            self._download_failures[dest_path] = error_msg
            logging.warning(f"[Model Installer] {error_msg}")

    # Workflow validation methods
    def validate_model_request(self, url: str, directory: str, filename: str) -> bool:
        """
        Validate model request against workflow templates.

        Args:
            url: The download URL for the model
            directory: The target directory (e.g., "vae", "checkpoints")
            filename: The model filename (e.g., "model.safetensors")

        Returns:
            True if the model is found in any workflow template, False otherwise
        """
        try:
            index = self._get_workflow_index()

            # Check if model exists in index
            model_key = f"{directory}/{filename}"
            if model_key not in index:
                logging.debug(f"[Model Installer] Model key '{model_key}' not found in workflow index")
                return False

            # Check if URL matches any known URL for this model
            known_urls = set(index[model_key].get("urls", []))
            
            # Remove query parameters from the request URL for comparison
            parsed_url = urlparse(url)
            clean_url = f"{parsed_url.scheme}://{parsed_url.netloc}{parsed_url.path}"
            
            # Check both the original URL and the clean URL
            if url in known_urls or clean_url in known_urls:
                logging.debug(f"[Model Installer] Validated: {model_key} from {url}")
                return True
            else:
                logging.warning(f"[Model Installer] URL mismatch for {model_key}. Got: {url} (clean: {clean_url}), Expected one of: {known_urls}")
                return False

        except Exception as e:
            logging.error(f"[Model Installer] Validation error: {e}")
            return False  # Fail secure

    def validate_install_path(self, folder_name: str, path: str, expected_size: int = 0) -> tuple[bool, str]:
        """Validate user-selected path and check storage using native path building."""
        import shutil
        
        # Check if path is valid for this folder type using filtered model paths
        model_paths = ModelInstaller.get_model_paths()
        entry = model_paths.get(folder_name)
        if not entry:
            return False, f"Unknown folder type: {folder_name}"
        
        valid_paths = entry[0]
        if path not in valid_paths:
            return False, f"Invalid path for {folder_name}. Valid paths: {valid_paths}"
        
        # Check available storage
        try:
            # Find closest existing parent for storage check (no makedirs)
            check_path = path
            while not os.path.exists(check_path) and check_path != os.path.dirname(check_path):
                check_path = os.path.dirname(check_path)
            
            if not os.path.exists(check_path):
                return False, f"Cannot find existing parent directory for {path}"
            
            available_space = shutil.disk_usage(check_path).free
            if expected_size > 0 and expected_size > available_space:
                gb_needed = expected_size / (1024**3)
                gb_available = available_space / (1024**3)
                return False, f"Not enough storage. Need {gb_needed:.1f}GB, available {gb_available:.1f}GB"
        except Exception as e:
            return False, f"Cannot check storage for path {path}: {e}"
        
        return True, ""

    def _get_workflow_index(self) -> Dict:
        """Get workflow index, refresh if needed."""
        if self._workflow_index is None or not self._check_workflow_index():
            logging.info("[Model Installer] Refreshing workflow model index")
            self._workflow_index = self._create_workflow_index()
        return self._workflow_index

    def _check_workflow_index(self) -> bool:
        """Check if current workflow index is still valid."""
        # If we've already loaded the index this session, use the cached version
        if self._index_loaded_this_session and self._workflow_index is not None:
            return True
            
        # If index file exists and we haven't loaded it this session, load it
        if self._index_file.exists():
            try:
                with open(self._index_file, 'r', encoding='utf-8') as f:
                    self._workflow_index = json.load(f)
                self._index_loaded_this_session = True
                logging.debug("[Model Installer] Loaded existing workflow index from disk")
                return True
            except Exception as e:
                logging.warning(f"[Model Installer] Error loading existing index: {e}")
                return False
        
        # No index file exists, need to create it
        return False

    def _create_workflow_index(self) -> Dict:
        """Create new workflow index from comfyui_workflow_templates module."""
        index = {}

        try:
            import comfyui_workflow_templates
            import importlib.resources
            
            logging.info("[Model Installer] Loading workflow templates from comfyui_workflow_templates module")
            
            # Get templates directory from module
            templates_path = importlib.resources.files(comfyui_workflow_templates) / 'templates'
            
            json_files = [f for f in templates_path.iterdir() if f.name.endswith('.json') and f.name != 'index.json']
            logging.info(f"[Model Installer] Found {len(json_files)} workflow template files")
            
            for json_file in json_files:
                try:
                    content = json_file.read_text(encoding='utf-8')
                    workflow_data = json.loads(content)
                    
                    models_found = 0
                    
                    # Parse structured model metadata from properties.models arrays
                    def extract_models_from_node(node_data):
                        nonlocal models_found
                        if isinstance(node_data, dict):
                            # Check if this node has properties.models
                            if "properties" in node_data and isinstance(node_data["properties"], dict):
                                if "models" in node_data["properties"] and isinstance(node_data["properties"]["models"], list):
                                    for model in node_data["properties"]["models"]:
                                        if isinstance(model, dict) and "name" in model and "url" in model and "directory" in model:
                                            filename = model["name"]
                                            url = model["url"]
                                            directory = model["directory"]
                                            
                                            key = f"{directory}/{filename}"
                                            if key not in index:
                                                index[key] = {"urls": set(), "workflows": set()}
                                            index[key]["urls"].add(url)
                                            index[key]["workflows"].add(json_file.name)
                                            models_found += 1
                                            logging.debug(f"[Model Installer] Found structured model: {key} -> {url}")
                            
                            # Recursively check all nested objects
                            for value in node_data.values():
                                if isinstance(value, (dict, list)):
                                    extract_models_from_node(value)
                        elif isinstance(node_data, list):
                            for item in node_data:
                                extract_models_from_node(item)
                    
                    # Extract from structured data
                    extract_models_from_node(workflow_data)
                    
                    if models_found > 0:
                        logging.debug(f"[Model Installer] Found {models_found} models in {json_file.name}")
                        
                except Exception as e:
                    logging.warning(f"[Model Installer] Failed to parse workflow {json_file.name}: {e}")
            
        except ImportError:
            logging.warning("[Model Installer] comfyui_workflow_templates module not available")
            return {}
        except Exception as e:
            logging.error(f"[Model Installer] Error loading workflow templates: {e}")
            return {}

        # Convert sets to lists for JSON serialization
        serializable_index = {}
        for key, data in index.items():
            serializable_index[key] = {
                "urls": list(data["urls"]),
                "workflows": list(data["workflows"])
            }

        try:
            with open(self._index_file, 'w', encoding='utf-8') as f:
                json.dump(serializable_index, f, indent=2)
            logging.info(f"[Model Installer] Saved workflow index with {len(serializable_index)} models to {self._index_file}")
            self._index_loaded_this_session = True
        except Exception as e:
            logging.warning(f"[Model Installer] Failed to save workflow index: {e}")

        return serializable_index

    def get_workflow_validation_stats(self) -> Dict:
        """Get statistics about the current workflow validation index."""
        try:
            index = self._get_workflow_index()
            total_models = len(index)
            total_urls = sum(len(data["urls"]) for data in index.values())
            workflows = set()
            for data in index.values():
                workflows.update(data["workflows"])

            return {
                "total_models": total_models,
                "total_urls": total_urls,
                "workflows_count": len(workflows),
                "workflows": sorted(list(workflows)),
                "index_file_exists": self._index_file.exists(),
                "index_current": self._check_workflow_index() if self._workflow_index else False
            }
        except Exception as e:
            return {"error": str(e)}

    def initialize_workflow_validation(self):
        """Initialize workflow validation on startup."""
        try:
            stats = self.get_workflow_validation_stats()
            logging.info(f"[Model Installer] Initialized workflow validation with {stats['total_models']} models from {stats['workflows_count']} workflows")
        except Exception as e:
            logging.error(f"[Model Installer] Failed to initialize workflow validation: {e}")
