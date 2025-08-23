from __future__ import annotations

import os
import urllib
import logging
from urllib.parse import urlparse
from typing import Callable, Any, Dict, Optional
import asyncio
import aiohttp
import json
import re
from pathlib import Path

import folder_paths


def resolve_folder_name_from_url(url: str) -> str | None:
    """Map URL segments to folder names (fallback when directory not provided)."""
    lowered = url.lower()
    segment_to_folder = {
        "/vae/": "vae",
        "/checkpoints/": "checkpoints",
        "/loras/": "loras",
        "/clip_vision/": "clip_vision",
        "/text_encoders/": "text_encoders",
        "/unet/": "diffusion_models",
        "/diffusion_models/": "diffusion_models",
        "/upscale_models/": "upscale_models",
        "/controlnet/": "controlnet",
    }
    for seg, folder in segment_to_folder.items():
        if seg in lowered:
            return folder
    return None


def get_primary_folder_path(folder_name: str) -> str | None:
    """Get the primary path for a folder name from ComfyUI's folder_paths."""
    entry = folder_paths.folder_names_and_paths.get(folder_name)
    if not entry:
        return None
    paths = entry[0]
    return paths[0] if paths else None


def safe_join(base: str, filename: str) -> str:
    """Safely join base path and filename, preventing directory traversal."""
    dest = os.path.abspath(os.path.join(base, filename))
    if os.path.commonpath((dest, os.path.abspath(base))) != os.path.abspath(base):
        raise ValueError("Unsafe path")
    return dest


class ModelInstaller:
    """Handles model downloading and installation."""
    
    def __init__(self, get_client_session: Callable[[], Any]):
        # Lazy accessor because the aiohttp ClientSession is created after server init
        self._get_client_session = get_client_session
        self._active_downloads: dict[str, int] = {}
        
        # Workflow validation system
        self._workflow_index: Optional[Dict] = None
        self._index_file = Path(__file__).parent / "workflow_model_index.json"

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
            sess = self._get_client_session()
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
        except Exception:
            pass
        sess = self._get_client_session()
        async with sess.get(url, headers=headers or None) as resp:
            resp.raise_for_status()
            if dest_path not in self._active_downloads:
                cl = int(resp.headers.get("Content-Length", 0))
                if cl:
                    self._active_downloads[dest_path] = cl
            os.makedirs(os.path.dirname(dest_path), exist_ok=True)
            total = 0
            with open(dest_path, "wb") as f:
                async for chunk in resp.content.iter_chunked(1024 * 256):
                    if chunk:
                        f.write(chunk)
                        total += len(chunk)
            logging.info(f"[Model Installer] download: completed bytes={total}")
            self._active_downloads.pop(dest_path, None)
            return total

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
            sess = self._get_client_session()
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
        except Exception as e:
            logging.warning(f"[Model Installer] expected_size error: {e}")
        return 0

    def queue_download(self, url: str, dest_path: str) -> None:
        """Start the download in the background."""
        try:
            asyncio.create_task(self.download(url, dest_path))
        except Exception as e:
            logging.warning(f"[Model Installer] failed to queue download: {e}")

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
            if url in known_urls:
                logging.debug(f"[Model Installer] Validated: {model_key} from {url}")
                return True
            else:
                logging.warning(f"[Model Installer] URL mismatch for {model_key}. Got: {url}, Expected one of: {known_urls}")
                return False

        except Exception as e:
            logging.error(f"[Model Installer] Validation error: {e}")
            return False  # Fail secure

    def _get_workflow_index(self) -> Dict:
        """Get workflow index, refresh if needed."""
        if self._workflow_index is None or not self._check_workflow_index():
            logging.info("[Model Installer] Refreshing workflow model index")
            self._workflow_index = self._create_workflow_index()
        return self._workflow_index

    def _check_workflow_index(self) -> bool:
        """Check if current workflow index is still valid."""
        if not self._index_file.exists():
            logging.debug("[Model Installer] Workflow index file does not exist")
            return False

        try:
            # For now, always refresh since we're using the module
            # In the future, we could check module version or timestamp
            return False  # Always refresh for now

        except Exception as e:
            logging.warning(f"[Model Installer] Error checking workflow index: {e}")
            return False  # Refresh on error

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
                    
                    # Extract model URLs and filenames using regex
                    # Look for Hugging Face URLs with .safetensors files
                    hf_pattern = r'https://huggingface\.co/[^/]+/[^/]+/resolve/[^/]+/([^/]+)/([^?\s"]+\.safetensors)'
                    matches = re.findall(hf_pattern, content)
                    
                    models_found = 0
                    for directory, filename in matches:
                        # Map directory names to ComfyUI folder names
                        directory_mapping = {
                            'vae': 'vae',
                            'checkpoints': 'checkpoints', 
                            'diffusion_models': 'diffusion_models',
                            'text_encoders': 'text_encoders',
                            'clip_vision': 'clip_vision',
                            'loras': 'loras',
                            'controlnet': 'controlnet',
                            'upscale_models': 'upscale_models'
                        }
                        
                        mapped_dir = directory_mapping.get(directory, directory)
                        key = f"{mapped_dir}/{filename}"
                        
                        # Find the full URL for this model
                        url_pattern = rf'https://huggingface\.co/[^/]+/[^/]+/resolve/[^/]+/{re.escape(directory)}/{re.escape(filename)}[^?\s"]*'
                        url_matches = re.findall(url_pattern, content)
                        
                        if url_matches:
                            url = url_matches[0]
                            if key not in index:
                                index[key] = {"urls": set(), "workflows": set()}
                            index[key]["urls"].add(url)
                            index[key]["workflows"].add(json_file.name)
                            models_found += 1
                    
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

        # Save index to disk
        try:
            with open(self._index_file, 'w', encoding='utf-8') as f:
                json.dump(serializable_index, f, indent=2)
            logging.info(f"[Model Installer] Saved workflow index with {len(serializable_index)} models to {self._index_file}")
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
