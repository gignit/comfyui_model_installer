"""
ComfyUI Model Installer Custom Node

Automatically installs missing model files from workflow templates.
Provides Install/Uninstall buttons in the Missing Models dialog with progress tracking.
"""

import logging
from .routes import register_routes

# Standard custom node exports
NODE_CLASS_MAPPINGS = {}
NODE_DISPLAY_NAME_MAPPINGS = {}
WEB_DIRECTORY = "./web"

# Self-register routes - ComfyUI-Manager handles enable/disable
try:
    register_routes()
    logging.info("[Model Installer] Custom node loaded successfully")
except Exception as e:
    logging.error(f"[Model Installer] Failed to register routes: {e}")

__all__ = ["NODE_CLASS_MAPPINGS", "NODE_DISPLAY_NAME_MAPPINGS", "WEB_DIRECTORY"]
