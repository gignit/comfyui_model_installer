"""Configuration for ComfyUI Model Installer."""

# Future: this will be loaded from a config.yaml file
ALLOW_UNINSTALL = False


def is_uninstall_enabled() -> bool:
    """
    Check if uninstall functionality is enabled.
    
    Currently returns a hardcoded False value.
    In the future, this will load from a configuration file.
    
    Returns:
        bool: True if uninstall is enabled, False otherwise
    """
    return ALLOW_UNINSTALL
