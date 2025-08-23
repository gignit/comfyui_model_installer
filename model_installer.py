from __future__ import annotations

import os
import urllib
import logging
from urllib.parse import urlparse
from typing import Callable, Any
import asyncio
import aiohttp

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
