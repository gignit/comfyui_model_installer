"""Configuration for ComfyUI Model Installer."""

# Future: this will be loaded from a config.yaml file
ALLOW_UNINSTALL = False

# Download configuration
DOWNLOAD_CONFIG = {
    # Timeout settings (for ClientTimeout)
    "timeout_connect": 15,  # 15 seconds to establish connection
    "timeout_total": None,  # No total timeout - let large files download as long as needed
    
    # Connector settings (for TCPConnector) - using aiohttp parameter names
    "limit": 100,           # Max total connections in pool (aiohttp default)
    "limit_per_host": 30,   # Max connections per host (aiohttp default) 
    # "ttl_dns_cache": 10,    # DNS cache TTL in seconds (aiohttp default)
    "use_dns_cache": True,  # Enable DNS caching (aiohttp default)
    
    # Download settings (for our code)
    "chunk_size": 8192,     # Download chunk size in bytes (aiohttp default)
}


def is_uninstall_enabled() -> bool:
    """
    Check if uninstall functionality is enabled.
    
    Currently returns a hardcoded False value.
    In the future, this will load from a configuration file.
    
    Returns:
        bool: True if uninstall is enabled, False otherwise
    """
    return ALLOW_UNINSTALL


def get_download_config() -> dict:
    """
    Get download configuration settings.
    
    Returns:
        dict: Download configuration parameters
    """
    return DOWNLOAD_CONFIG.copy()
