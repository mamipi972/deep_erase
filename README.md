# 🪄 Deep Erase for GIMP 3.0

**Deep Erase** is an advanced retouching plugin for **GIMP 3.0** that uses Artificial Intelligence to remove unwanted objects from your photos (inpainting) and rebuild the background in a hyper-realistic way.

Powered by the **LaMa** model (Large Mask Inpainting) via **ONNX Runtime**, this plugin uses a *"crash-proof"* architecture: the AI computation runs in a fully isolated environment, so GIMP itself never crashes — even if something goes wrong with the model.

> 🇫🇷 A French version of this document is available in [`README.fr.md`](README.fr.md).
<img width="2554" height="853" alt="image" src="https://github.com/user-attachments/assets/c0b9c0cd-d593-49d5-aa17-63746b2f2569" />

---

## ✨ Features

- **State-of-the-art generative AI** — uses the LaMa model for intelligent reconstruction of complex textures (water, sand, brick, horizons...).
- **Isolated "submarine" architecture** — heavy computation runs outside GIMP's own process, in a dedicated Python virtual environment. A crash or error in the AI engine can never bring GIMP down with it.
- **Automatic safe fallback** — if the AI model is missing, corrupted, or fails for any reason, the plugin silently switches to OpenCV's Navier-Stokes inpainting algorithm so you never lose your work.
- **Zero manual configuration** — the plugin creates its own virtual environment and downloads its Python dependencies automatically, working around Linux's PEP 668 ("externally-managed-environment") restriction natively.
- **Hardened by design** — see the [Security](#-security-notes) section below for what is done to keep a malicious or corrupted AI model file from harming your system.

---

## 🛠️ Requirements

- **GIMP 3.0** (Release Candidate or final release).
- **A standard 64-bit Python 3 installation**, separate from the one bundled inside GIMP. **CPython 3.10 to 3.12 from [python.org](https://www.python.org/) is recommended.**
  - 32-bit or ARM builds of Python are **not** supported: the scientific libraries this plugin relies on (NumPy, OpenCV, ONNX Runtime) do not ship pre-built binaries for those architectures, and the plugin deliberately refuses to compile them from source (see [Security](#-security-notes)).
  - Very recent or pre-release Python versions (e.g. a version released in the last few months) may not yet have compatible wheels published by NumPy/OpenCV/ONNX Runtime. If you hit this, install a slightly older stable Python 3.10–3.12 instead.
- **An internet connection on first use only** — the plugin downloads about 150–200 MB of Python packages the first time it runs. Subsequent runs work fully offline.

---

## 🚀 Installation

GIMP 3.0 is very strict about plugin folder structure. Follow these steps carefully.

### Step 1 — Prepare the plugin folder

1. Open GIMP and go to **Edit ▸ Preferences ▸ Folders ▸ Plug-Ins**.
2. Open your user plugin folder (the path usually ending in `plug-ins`).
3. Create a new folder named **exactly** `deep_erase` (lowercase, with the underscore).
4. Place `deep_erase.py` inside this folder.

Your folder structure should look like this:

```
.../plug-ins/deep_erase/deep_erase.py
```

> ⚠️ **GIMP 3.0 requires the folder name to match the script filename (without extension) exactly**, and this is **case-sensitive on Linux**. If your browser renamed the downloaded file to something like `deep_erase (1).py`, GIMP will silently ignore it.

### Step 2 — Install the AI "brain" (the LaMa model)

1. Go to the official model repository on Hugging Face: [**Carve/LaMa-ONNX**](https://huggingface.co/Carve/LaMa-ONNX).
2. Download the file **`lama_fp32.onnx`** (about 200 MB).

   > 🚨 **Important — read carefully:** this repository contains **two different files**: `lama_fp32.onnx` (**recommended**, stable) and `lama.onnx` (**not recommended** — it uses a different export pipeline with a known bug in its Fourier-transform operator, and will fail with an error mentioning `DFT` / `one-sided DFT`). **Make sure you download `lama_fp32.onnx`, not the file already named `lama.onnx`.**

3. Place this file inside your `deep_erase` folder.
4. **Rename it to `lama.onnx`** (all lowercase).

Your final folder structure must be:

```
plug-ins/
└── deep_erase/
    ├── deep_erase.py
    └── lama.onnx        (this is lama_fp32.onnx, renamed)
```

### Step 3 — Platform-specific permissions (Linux & macOS only)

On UNIX systems, GIMP silently ignores scripts that are not marked executable, **without any error message**.

```bash
cd path/to/plug-ins/deep_erase
chmod +x deep_erase.py
```

> ⚠️ If you downloaded the `.py` file on Windows and then moved it to Linux/macOS, make sure its line endings are **LF**, not **CRLF** — otherwise GIMP will fail to run it, again without any visible error.

---

## 🎨 Usage

On the very first run, the plugin will take a few minutes to build its virtual environment and download its dependencies (NumPy, OpenCV, ONNX Runtime, ONNX). A progress bar will keep you informed. Every run after that takes only 5–15 seconds.

1. Open your image in GIMP.
2. Use a selection tool (Lasso, Rectangle, etc.) to select the object you want to remove — let the selection slightly overflow onto the surrounding background to help the AI.
3. Make sure the correct layer is active in the Layers panel.
4. Go to **Filters ▸ Enhance ▸ Deep Erase...**
5. Wait. The plugin will create a new layer:
   - **`Deep Erase Result (IA LaMa)`** if the AI model ran successfully.
   - **`Resultat (Mode Secours OpenCV)`** if the plugin fell back to the classic algorithm (see [Troubleshooting](#-troubleshooting)).

---

## 🔒 Security notes

Since this plugin runs a third-party AI model file on your machine, it includes several layers of protection against a corrupted or malicious `lama.onnx` file:

| Protection | What it does |
|---|---|
| **Wheels-only installs** | Python dependencies are installed with `--only-binary`, meaning **no compiler is ever invoked** on your machine. This closes off a whole class of source-build supply-chain attacks and also means you never need Visual Studio / Xcode / build-essential installed. |
| **Structural graph validation** | Before the model is ever loaded into the inference engine, its structure is checked (via the official `onnx` package): tensor dimensions, node count, and declared operator domains are all validated against sane limits. |
| **No custom operators, ever** | The plugin never calls ONNX Runtime's custom-operator-loading API, so a model file cannot cause an external `.dll`/`.so` to be loaded — this is enforced structurally, not just by omission. |
| **Hard inference timeout** | AI computation is capped at 15 minutes. If exceeded (e.g. a malformed model causing runaway computation), the process is killed and the plugin automatically retries in a safe OpenCV-only mode that never touches the model file again. |
| **Memory ceiling** | The worker subprocess runs under a best-effort memory limit (4 GB) on both Windows and POSIX systems, to contain runaway memory use. |
| **Process isolation** | AI computation always runs in a separate subprocess, in a dedicated virtual environment, using an interpreter that is never GIMP's own bundled Python. A crash there cannot crash GIMP. |
| **Trust-on-first-use model check** | The plugin remembers the SHA-256 hash of the model file the first time it's used successfully, and warns you if that file changes unexpectedly on a later run. |

**None of this replaces downloading the model only from the official [Carve/LaMa-ONNX](https://huggingface.co/Carve/LaMa-ONNX) repository.** Please don't use `.onnx` files from untrusted or unofficial sources.

---

## 🚨 Troubleshooting

### The plugin doesn't appear in the Filters menu

- **Windows:** double-check the folder is named exactly `deep_erase` and the script `deep_erase.py`. GIMP 3.0 requires an exact match.
- **Linux/macOS:** did you run `chmod +x`? To see the real error, launch GIMP from a terminal (`gimp` or `gimp-2.99`) — GIMP prints the actual Python startup error to the console, which never appears in the graphical interface.
- Check that the file wasn't renamed by your browser (e.g. `deep_erase (1).py`) or saved with CRLF line endings after a Windows → Linux transfer.

### Installing NumPy / OpenCV / ONNX Runtime fails

If the error output mentions a missing compiler (`cl.exe`, `gcc`, `Meson`, "Unknown compiler(s)"...), it means no pre-built package (*wheel*) was found for your exact Python interpreter — usually because:
- Your Python is **32-bit** or **ARM**, or
- Your Python version is **very recent** and the libraries haven't published compatible wheels yet.

The error message includes a diagnostic block with your Python version and architecture — check it, and if needed, install a standard **64-bit CPython 3.10–3.12** from [python.org](https://www.python.org/).

### The installation seems stuck, or times out after several minutes

This usually means your network connection, firewall, or a corporate proxy is blocking access to PyPI (`pip`'s package index). Check your connection and any proxy settings.

### The result layer is named "Resultat (Mode Secours OpenCV)" instead of the AI name

This means the AI model failed to run and the plugin safely fell back to the classic OpenCV algorithm. Read the detailed warning message GIMP shows — it explains the specific reason (see below for the most common one).

### Error mentioning `DFT` / "one-sided DFT requires real input"

You very likely downloaded the wrong file from the Hugging Face repository. There are two files there — `lama_fp32.onnx` (correct) and `lama.onnx` (already named that way, but **not** the one you want). Re-download `lama_fp32.onnx` specifically and rename it to `lama.onnx` yourself. See [Step 2](#step-2--install-the-ai-brain-the-lama-model) above.

### A message says "the model file has changed since the last successful use"

This is expected and harmless if you intentionally replaced `lama.onnx` (for example, to fix the issue above). If you did **not** replace the file, it could indicate corruption — re-download it from the official source.

### Processing takes a very long time or hits the 15-minute timeout

Large images or very large selections can be slow on CPU-only inference. Try a smaller selection, or downscale the image before running the filter.

### Starting over from a clean state

If you're stuck, you can force a full reset:
1. Delete the `deep_erase_venv` folder inside `deep_erase/`.
2. Delete `deep_erase_python_cache.txt` and `deep_erase_model_trust.json` in GIMP's user configuration directory (**Edit ▸ Preferences ▸ Folders**, root folder shown there).
3. Relaunch GIMP and try the filter again.

---

## Credits

- Original LaMa model and paper: [advimman/lama](https://github.com/advimman/lama)
- ONNX export used by this plugin: [Carve-Photos/lama](https://github.com/Carve-Photos/lama) and [Carve/LaMa-ONNX](https://huggingface.co/Carve/LaMa-ONNX) on Hugging Face
- Inference: [ONNX Runtime](https://onnxruntime.ai/) (Microsoft)
- Classic fallback algorithm: [OpenCV](https://opencv.org/) (Navier-Stokes inpainting)

## License

Developed for GIMP 3.0 — Python 3 (PyGObject). See the LICENSE file in this repository for the plugin's own license terms. The LaMa ONNX model is distributed separately under the Apache 2.0 license by its authors — refer to the [Carve/LaMa-ONNX](https://huggingface.co/Carve/LaMa-ONNX) page for details.
