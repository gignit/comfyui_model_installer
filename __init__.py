"""
ComfyUI Model Installer Custom Node

Automatically installs missing model files from workflow templates.
Provides Install/Uninstall buttons in the Missing Models dialog with progress tracking.
"""

# Required by ComfyUI-Manager
NODE_ID = "comfyui_model_installer"
NODE_CLASS_MAPPINGS = {}
NODE_DISPLAY_NAME_MAPPINGS = {}
WEB_DIRECTORY = "./web"

# Register server routes when ComfyUI is running (safe to import during CLI install)
try:
    from .routes import try_register_routes
    try_register_routes()
except Exception:
    pass  # Avoid blocking CLI or install if Comfy isn't running

__all__ = [
    "NODE_ID",
    "NODE_CLASS_MAPPINGS",
    "NODE_DISPLAY_NAME_MAPPINGS",
    "WEB_DIRECTORY",
]

