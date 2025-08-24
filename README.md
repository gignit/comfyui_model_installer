# ComfyUI Model Installer

Automatically installs missing model files from workflow templates with Install/Uninstall buttons and progress tracking.

## Features

- **Workflow-Based Security**: Only allows installation of models defined in workflow templates
- **One-Click Installation**: Install missing models directly from the Missing Models dialog
- **Smart Directory Detection**: Automatically determines correct installation directories
- **Multi-Path Support**: Downloads files to path with most available storage (v1.1.2 - requested by BrknSoul)
- **Progress Tracking**: Real-time download progress with speed, ETA, and completion percentage
- **Hugging Face Support**: Seamless authentication for gated models
- **Multiple Sources**: Supports Hugging Face, Civitai, and direct download URLs
- **Safe Installation**: Validates paths and prevents unauthorized downloads

## Installation

### Via ComfyUI-Manager (Recommended)
1. Open ComfyUI-Manager in your ComfyUI interface
2. Search for "Model Installer"
3. Click Install
4. Restart ComfyUI

### Manual Installation
1. Clone this repository to your ComfyUI custom_nodes directory:
   ```bash
   cd ComfyUI/custom_nodes
   git clone https://github.com/gignit/comfyui_model_installer
   ```
2. Install dependencies:
   ```bash
   pip install -r comfyui_model_installer/requirements.txt
   ```
3. Restart ComfyUI

## Usage

1. Load a workflow that requires missing models
2. Open the Missing Models dialog (appears automatically or via menu)
3. Click the **Install** button next to any missing model

![Missing Models](images/step1-missing-models.png)

4. Watch the progress in the top-right download panel

![Downloading](images/step2-downloading.png)

5. The button changes to **Uninstall** when the model is installed

![Complete](images/step3-complete.png)


### Hugging Face Authentication

For gated models on Hugging Face:
1. Click Install on a gated model
2. Enter your Hugging Face token in the dialog that appears
3. Get your token from: https://huggingface.co/settings/tokens
4. The token is saved for future downloads

## How It Works

The extension automatically determines the correct installation directory by:
1. **Primary Method**: Reading directory info from workflow templates (`"text_encoders / clip_l.safetensors"`)
2. **Fallback Method**: Mapping URL patterns to model directories

### Supported Model Types
- Checkpoints → `models/checkpoints/`
- LoRAs → `models/loras/`
- VAE → `models/vae/`
- ControlNet → `models/controlnet/`
- Text Encoders → `models/text_encoders/`
- CLIP Vision → `models/clip_vision/`
- Upscale Models → `models/upscale_models/`

## Configuration

### Enable/Disable Extension
Use ComfyUI-Manager to enable or disable this extension:
- ComfyUI-Manager → Custom Nodes → Enable/Disable "Model Installer"

**Important**: The directory name must use underscores (`comfyui_model_installer`) not hyphens for Python import compatibility.

### Uninstall Feature
The uninstall feature is disabled by default. To enable it, modify the configuration:

1. Edit `custom_nodes/comfyui_model_installer/config.py`
2. Change `ALLOW_UNINSTALL = False` to `ALLOW_UNINSTALL = True`
3. Restart ComfyUI

When disabled, the client will still be presented with a button that indicates 'uninstall' but clicking this will gracefully fail indicating the feature is disabled.

## Troubleshooting

### Install buttons not visible
- Check that the extension is enabled in ComfyUI-Manager
- Hard refresh your browser (Ctrl+F5)
- Check browser console for errors

### Downloads not working
- Verify internet connection
- Check ComfyUI logs for error messages
- For Hugging Face models, ensure you have a valid token

### Permission errors
- Ensure ComfyUI has write permissions to model directories
- Check available disk space

## Security

- **Workflow Validation**: Only models defined in workflow templates can be installed
- **Zero-Trust Model**: Backend validates all requests against cached workflow index
- **Path Protection**: Prevents directory traversal attacks with safe path joining
- **Secure Authentication**: Hugging Face tokens stored using standard `huggingface_hub` library

## Proxy Support

The model installer automatically uses the same proxy settings as ComfyUI. If you need to configure proxy settings for your environment:

### Environment Variables
Set these environment variables before starting ComfyUI:

```bash
# For HTTP and HTTPS proxies
export HTTP_PROXY=http://proxy.company.com:8080
export HTTPS_PROXY=http://proxy.company.com:8080

# For authenticated proxies
export HTTP_PROXY=http://username:password@proxy.company.com:8080
export HTTPS_PROXY=http://username:password@proxy.company.com:8080

# Exclude local addresses from proxy
export NO_PROXY=localhost,127.0.0.1,.local

# Then start ComfyUI
python main.py --listen 0.0.0.0 --port 8189
```

### Windows Example
```cmd
set HTTP_PROXY=http://proxy.company.com:8080
set HTTPS_PROXY=http://proxy.company.com:8080
python main.py --listen 0.0.0.0 --port 8189
```

The model installer uses the same HTTP client as ComfyUI's registry downloads, so if ComfyUI can fetch updates and registry data, model downloads will work through the same proxy configuration.

## License

MIT License - see LICENSE file for details
