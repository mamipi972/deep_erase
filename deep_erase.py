#!/usr/bin/env python3
# -*- coding: utf-8 -*-

import gi
gi.require_version('Gimp', '3.0')
gi.require_version('GimpUi', '3.0')
from gi.repository import Gimp, GimpUi, GLib, Gio, Gegl

import os
import sys
import subprocess
import tempfile
import time
import glob
import hashlib
import json

PROCEDURE_NAME = "deep-erase"
PLUGIN_ID = "deep_erase"
CACHE_FILE = "deep_erase_python_cache.txt"
MODEL_TRUST_FILE = "deep_erase_model_trust.json"

# Paquets requis avec une plage de versions figee : evite qu'une future
# release majeure (ex: numpy 3.x, onnxruntime 2.x) casse silencieusement
# le greffon des annees apres sa publication.
REQUIRED_PACKAGES = {
    "numpy>=1.24,<3": "numpy",
    "opencv-python-headless>=4.8,<6": "cv2",
    "onnxruntime>=1.16,<2": "onnxruntime",
    # Bibliotheque legere (parsing Protobuf + verificateur de graphe officiel)
    # utilisee UNIQUEMENT pour valider la structure du modele avant de le
    # confier au moteur d'inference lourd. Ne supprime pas le risque
    # protobuf (meme famille de parseur), mais ajoute une passe de sanite
    # (dimensions, domaines d'operateurs, nombre de noeuds) qui bloque les
    # cas grossierement aberrants avant toute allocation memoire consequente.
    "onnx>=1.14,<2": "onnx",
}

PROBE_TIMEOUT = 5          # secondes, pour une sonde "ce binaire repond-il ?"
INSTALL_TIMEOUT = 600      # secondes, pour l'installation reseau des paquets
INFERENCE_TIMEOUT = 900    # secondes, plafond dur sur le calcul IA (anti boucle infinie / DoS)
SELECTION_COVERAGE_WARN = 0.97  # avertir si la selection couvre >97% de l'image
MASK_DILATION_PX = 6  # marge de securite (pixels) pour ne jamais laisser un reste de l'objet juste hors du masque
MAX_CROP_SIZE = 900  # taille max du recadrage carre : limite le facteur de reduction/agrandissement vers les 512x512 du modele
WHOLE_IMAGE_THRESHOLD = 1200  # en dessous de cette taille (plus grande dimension), on traite le cadre complet, jamais un recadrage local

MAX_MODEL_FILE_BYTES = 800 * 1024 * 1024   # 800 Mo : tres au-dessus des ~200 Mo documentes
MAX_GRAPH_NODES = 50_000                    # tres au-dessus d'un LaMa reel (quelques centaines/milliers)
MAX_TENSOR_ELEMENTS = 2_000_000_000         # ~2 milliards d'elements : bloque les dimensions aberrantes
ALLOWED_OPSET_DOMAINS = {"", "ai.onnx", "ai.onnx.ml"}  # aucun operateur personnalise autorise

# Plafond memoire applique au sous-processus de calcul (best-effort, ne
# bloque jamais le fonctionnement normal si la plateforme ne le supporte pas).
WORKER_MEMORY_LIMIT_BYTES = 4 * 1024 * 1024 * 1024  # 4 Go


# ---------------------------------------------------------------------------
# Environnement / detection Python
# ---------------------------------------------------------------------------

def env_clean():
    """Retourne un environnement debarrasse de tout ce qui pourrait faire
    pointer un interpreteur externe vers les libs internes de GIMP."""
    clean_env = os.environ.copy()
    clean_env.pop('PYTHONPATH', None)
    clean_env.pop('PYTHONHOME', None)
    if 'PATH' in clean_env:
        paths = clean_env['PATH'].split(os.pathsep)
        clean_env['PATH'] = os.pathsep.join([p for p in paths if 'gimp' not in p.lower()])
    return clean_env


def _popen_kwargs():
    return {'creationflags': 0x08000000} if os.name == 'nt' else {}


def test_python_executable(path, timeout=PROBE_TIMEOUT):
    """Verifie par execution reelle (jamais par simple presence du fichier)
    qu'un interpreteur est utilisable et distinct de celui de GIMP."""
    if not path or not os.path.isfile(path):
        return False
    gimp_dir = os.path.dirname(sys.executable).lower()
    if gimp_dir in path.lower():
        return False
    try:
        res = subprocess.run(
            [path, '-c', 'import sys; sys.exit(0)'],
            env=env_clean(), capture_output=True, timeout=timeout, **_popen_kwargs()
        )
        return res.returncode == 0
    except Exception:
        return False


def _registry_python_candidates():
    """Lecture directe du registre Windows (Software\\Python\\PythonCore).
    Complementaire de 'py -0p' : certaines installations (ex: via un
    installeur MSI tiers, ou une politique d'entreprise) enregistrent la
    cle registre sans forcement enregistrer le lanceur 'py'."""
    if os.name != 'nt':
        return []
    candidates = set()
    try:
        import winreg
    except ImportError:
        return []

    hives = [
        (winreg.HKEY_CURRENT_USER, r"Software\Python\PythonCore"),
        (winreg.HKEY_LOCAL_MACHINE, r"Software\Python\PythonCore"),
        (winreg.HKEY_LOCAL_MACHINE, r"Software\WOW6432Node\Python\PythonCore"),
    ]
    for hive, base_key in hives:
        try:
            with winreg.OpenKey(hive, base_key) as key:
                i = 0
                while True:
                    try:
                        version_name = winreg.EnumKey(key, i)
                    except OSError:
                        break
                    i += 1
                    try:
                        with winreg.OpenKey(key, f"{version_name}\\InstallPath") as install_key:
                            install_dir, _ = winreg.QueryValueEx(install_key, "")
                            candidate = os.path.join(install_dir, "python.exe")
                            if os.path.isfile(candidate):
                                candidates.add(candidate)
                    except OSError:
                        continue
        except OSError:
            continue
    return list(candidates)


def find_system_python():
    candidates = set()
    env = env_clean()
    kwargs = _popen_kwargs()

    cmds = ['python3', 'python'] if os.name != 'nt' else ['py', 'python', 'python3']
    for cmd in cmds:
        try:
            res = subprocess.run(
                [cmd, '-c', 'import sys; print(sys.executable)'],
                env=env, capture_output=True, text=True, timeout=PROBE_TIMEOUT, **kwargs
            )
            if res.returncode == 0:
                candidates.add(res.stdout.strip())
        except Exception:
            pass

    if os.name == 'nt':
        try:
            res = subprocess.run(['py', '-0p'], env=env, capture_output=True, text=True,
                                  timeout=PROBE_TIMEOUT, **kwargs)
            import re
            for match in re.findall(r"\s+\-\s+\S+\s+(.+)", res.stdout):
                candidates.add(match.strip())
        except Exception:
            pass

        candidates.update(_registry_python_candidates())

        local_appdata = os.environ.get('LOCALAPPDATA', '')
        if local_appdata:
            for p in glob.glob(os.path.join(local_appdata, 'Programs', 'Python', 'Python*', 'python.exe')):
                candidates.add(p)
            winapps_python = os.path.join(local_appdata, 'Microsoft', 'WindowsApps', 'python.exe')
            if os.path.isfile(winapps_python):
                candidates.add(winapps_python)

        for pf in [os.environ.get('PROGRAMFILES', ''), os.environ.get('PROGRAMFILES(X86)', '')]:
            if pf:
                for p in glob.glob(os.path.join(pf, 'Python*', 'python.exe')):
                    candidates.add(p)

    if os.name != 'nt':
        home = os.path.expanduser("~")
        for venv_base in ['.venvs', '.virtualenvs', '.venv', PLUGIN_ID]:
            base_dir = os.path.join(home, venv_base)
            if os.path.isdir(base_dir):
                try:
                    for d in os.listdir(base_dir):
                        p = os.path.join(base_dir, d, 'bin', 'python3')
                        if os.path.isfile(p):
                            candidates.add(p)
                except Exception:
                    pass

    for cand in candidates:
        if test_python_executable(cand):
            return cand
    return None


def setup_venv(system_python):
    plugin_dir = os.path.dirname(os.path.realpath(__file__))
    venv_dir = os.path.join(plugin_dir, f"{PLUGIN_ID}_venv")
    venv_python = os.path.join(venv_dir, 'Scripts', 'python.exe') if os.name == 'nt' else os.path.join(venv_dir, 'bin', 'python3')
    env = env_clean()
    kwargs = _popen_kwargs()

    if not os.path.isfile(venv_python):
        Gimp.progress_set_text("Création de l'environnement virtuel dédié (anti-PEP 668)...")
        try:
            process = subprocess.Popen([system_python, '-m', 'venv', venv_dir], env=env, **kwargs)
            while process.poll() is None:
                Gimp.progress_pulse()
                time.sleep(0.2)
            if process.returncode != 0 or not os.path.isfile(venv_python):
                raise RuntimeError(
                    "Échec de création de l'environnement virtuel.\n"
                    "Sur Linux/Debian/Ubuntu, installez le paquet manquant avec :\n"
                    "sudo apt install python3-venv"
                )
        except FileNotFoundError:
            raise RuntimeError(f"Interpréteur Python introuvable : {system_python}")
        except Exception as e:
            raise RuntimeError(f"Erreur de création venv : {str(e)}")

    def _run_capture(cmd):
        """Execute une commande en capturant stdout+stderr sans risque de
        deadlock (ecriture dans un fichier temporaire plutot que PIPE, qui
        peut se bloquer si la sortie depasse le tampon du pipe pendant
        qu'on poll() sans lire)."""
        with tempfile.NamedTemporaryFile(mode='w+', delete=False, suffix='.log') as log_f:
            log_path = log_f.name
        try:
            with open(log_path, 'w') as log_f:
                res = subprocess.run(cmd, env=env, stdout=log_f, stderr=subprocess.STDOUT,
                                      text=True, **kwargs)
            with open(log_path, 'r', errors='replace') as log_f:
                output = log_f.read()
            return res.returncode, output
        finally:
            try:
                os.remove(log_path)
            except OSError:
                pass

    def _run_popen_capture(cmd, timeout_s, progress_text):
        """Comme _run_capture mais non-bloquant, avec pulsation de la barre
        de progression pendant l'attente et timeout strict."""
        with tempfile.NamedTemporaryFile(mode='w', delete=False, suffix='.log') as log_f:
            log_path = log_f.name
        try:
            with open(log_path, 'w') as log_f:
                Gimp.progress_set_text(progress_text)
                process = subprocess.Popen(cmd, env=env, stdout=log_f, stderr=subprocess.STDOUT, **kwargs)
                start_time = time.time()
                while process.poll() is None:
                    Gimp.progress_pulse()
                    time.sleep(0.2)
                    if time.time() - start_time > timeout_s:
                        process.kill()
                        process.wait()
                        with open(log_path, 'r', errors='replace') as f2:
                            partial = f2.read()
                        raise RuntimeError(
                            f"Timeout ({timeout_s // 60} min) dépassé.\n"
                            f"Dernière sortie avant coupure :\n{partial[-1500:]}"
                        )
            with open(log_path, 'r', errors='replace') as log_f:
                output = log_f.read()
            return process.returncode, output
        finally:
            try:
                os.remove(log_path)
            except OSError:
                pass

    # Met a jour pip lui-meme avant toute installation : un pip trop ancien
    # (herite d'un vieux Python systeme) echoue souvent silencieusement a
    # resoudre des contraintes de version modernes ou des wheels recentes.
    # Non bloquant : si cette etape echoue (ex: pas de reseau), on continue
    # quand meme, l'installation des paquets ci-dessous donnera le vrai motif.
    try:
        _run_popen_capture([venv_python, '-m', 'pip', 'install', '--upgrade', 'pip'],
                            timeout_s=120, progress_text="Mise à jour de pip...")
    except Exception:
        pass

    def _platform_diagnostic():
        rc, out = _run_capture([
            venv_python, '-c',
            "import sys, platform; "
            "print('Python :', sys.version); "
            "print('Executable :', sys.executable); "
            "print('Architecture :', platform.machine()); "
            "print('64 bits :', sys.maxsize > 2**32)"
        ])
        return out.strip() if rc == 0 else "(diagnostic indisponible)"

    for package_spec, module_name in REQUIRED_PACKAGES.items():
        check_rc, check_out = _run_capture([venv_python, '-c', f'import {module_name}'])
        if check_rc != 0:
            Gimp.progress_set_text(f"Installation réseau de {module_name} (patientez)...")
            # --only-binary=:all: interdit toute compilation depuis les
            # sources : si aucune wheel precompilee n'existe pour cet
            # interpreteur (version trop recente/beta, architecture 32 bits
            # ou ARM non supportee...), pip echoue vite et proprement au
            # lieu de tenter d'invoquer un compilateur C absent du systeme.
            install_rc, install_out = _run_popen_capture(
                [venv_python, '-m', 'pip', 'install', '--no-input',
                 '--only-binary=:all:', '--prefer-binary', package_spec],
                timeout_s=INSTALL_TIMEOUT,
                progress_text=f"Installation réseau de {module_name} (patientez)..."
            )

            if install_rc != 0:
                raise RuntimeError(
                    f"Échec de l'installation de {module_name} : aucun paquet précompilé "
                    "compatible n'a été trouvé pour votre interpréteur Python (ou le réseau "
                    "est inaccessible). Ce greffon n'installe jamais de compilateur — il lui "
                    "faut un Python 64 bits standard (CPython 3.10 à 3.12 recommandé, "
                    "téléchargé depuis python.org).\n\n"
                    f"Diagnostic de l'interpréteur utilisé :\n{_platform_diagnostic()}\n\n"
                    f"Sortie de pip :\n{install_out[-2000:]}"
                )

            recheck_rc, recheck_out = _run_capture([venv_python, '-c', f'import {module_name}'])
            if recheck_rc != 0:
                raise RuntimeError(
                    f"Le paquet {module_name} a été installé par pip mais reste inimportable "
                    f"(bibliothèque système manquante ?).\n\n"
                    f"Diagnostic de l'interpréteur utilisé :\n{_platform_diagnostic()}\n\n"
                    f"Erreur Python :\n{recheck_out[-2000:]}"
                )

    return venv_python


# ---------------------------------------------------------------------------
# Confiance a la premiere utilisation (TOFU) pour le modele ONNX
# ---------------------------------------------------------------------------

def _sha256_of_file(path, chunk_size=1024 * 1024):
    h = hashlib.sha256()
    with open(path, 'rb') as f:
        for chunk in iter(lambda: f.read(chunk_size), b''):
            h.update(chunk)
    return h.hexdigest()


def check_model_trust(model_path):
    """Nous ne connaissons pas a l'avance le hash officiel du fichier
    (il depend de la source exacte telechargee par l'utilisateur), donc on
    applique une politique de confiance-a-la-premiere-utilisation :
    - premiere execution reussie -> on memorise le hash du fichier utilise
    - executions suivantes -> si le fichier a change sans que l'utilisateur
      ne l'ait explicitement remplace intentionnellement, on previent.
    Retourne (is_known_change, message) ; ne bloque jamais l'execution,
    n'affiche qu'un avertissement informatif."""
    try:
        trust_path = os.path.join(Gimp.directory(), MODEL_TRUST_FILE)
        current_hash = _sha256_of_file(model_path)

        stored = {}
        if os.path.isfile(trust_path):
            try:
                with open(trust_path, 'r') as f:
                    stored = json.load(f)
            except Exception:
                stored = {}

        previous_hash = stored.get(model_path)
        if previous_hash is None:
            stored[model_path] = current_hash
            with open(trust_path, 'w') as f:
                json.dump(stored, f)
            return False, ""
        if previous_hash != current_hash:
            stored[model_path] = current_hash
            with open(trust_path, 'w') as f:
                json.dump(stored, f)
            return True, (
                "Le fichier de modèle IA (lama.onnx) a changé depuis la dernière utilisation "
                "réussie. Si vous n'avez pas volontairement remplacé ce fichier, il pourrait "
                "être corrompu ou avoir été modifié par un tiers."
            )
        return False, ""
    except Exception:
        # Le controle de confiance ne doit jamais empecher le traitement.
        return False, ""


# ---------------------------------------------------------------------------
# Worker isole (execute dans le venv, hors du processus GIMP)
# ---------------------------------------------------------------------------

def is_model_file_size_sane(model_path):
    """Rejette d'emblee un fichier modele a la taille absurde (trop gros
    pour etre le modele documente, ou suspicieusement vide/tronque), avant
    meme de le confier au moteur d'inference."""
    try:
        size = os.path.getsize(model_path)
    except OSError:
        return False, "Fichier illisible."
    if size <= 0:
        return False, "Fichier vide."
    if size > MAX_MODEL_FILE_BYTES:
        return False, (
            f"Fichier de {size / (1024*1024):.0f} Mo, largement au-dessus des ~200 Mo "
            "attendus pour ce modele. Rejete par precaution."
        )
    return True, ""


def _posix_memory_limit_preexec(limit_bytes):
    """A utiliser comme preexec_fn de subprocess.Popen sur POSIX uniquement :
    applique un plafond dur sur la memoire adressable du sous-processus, de
    sorte qu'un modele malveillant tentant d'allouer des quantites de RAM
    aberrantes provoque un MemoryError controle dans le worker plutot que
    de saturer la machine hote."""
    def _limiter():
        try:
            import resource
            resource.setrlimit(resource.RLIMIT_AS, (limit_bytes, limit_bytes))
        except Exception:
            pass  # Best-effort : ne doit jamais empecher l'execution normale.
    return _limiter


def _assign_windows_job_memory_limit(process, limit_bytes):
    """Best-effort, Windows uniquement : place le processus enfant dans un
    Job Object avec un plafond memoire, via ctypes (aucune dependance
    externe type pywin32 requise). Echoue silencieusement si indisponible."""
    if os.name != 'nt':
        return
    try:
        import ctypes
        from ctypes import wintypes

        kernel32 = ctypes.windll.kernel32

        JobObjectExtendedLimitInformation = 9
        JOB_OBJECT_LIMIT_PROCESS_MEMORY = 0x00000100

        class IO_COUNTERS(ctypes.Structure):
            _fields_ = [("ReadOperationCount", ctypes.c_uint64),
                        ("WriteOperationCount", ctypes.c_uint64),
                        ("OtherOperationCount", ctypes.c_uint64),
                        ("ReadTransferCount", ctypes.c_uint64),
                        ("WriteTransferCount", ctypes.c_uint64),
                        ("OtherTransferCount", ctypes.c_uint64)]

        class JOBOBJECT_BASIC_LIMIT_INFORMATION(ctypes.Structure):
            _fields_ = [("PerProcessUserTimeLimit", ctypes.c_int64),
                        ("PerJobUserTimeLimit", ctypes.c_int64),
                        ("LimitFlags", wintypes.DWORD),
                        ("MinimumWorkingSetSize", ctypes.c_size_t),
                        ("MaximumWorkingSetSize", ctypes.c_size_t),
                        ("ActiveProcessLimit", wintypes.DWORD),
                        ("Affinity", ctypes.c_size_t),
                        ("PriorityClass", wintypes.DWORD),
                        ("SchedulingClass", wintypes.DWORD)]

        class JOBOBJECT_EXTENDED_LIMIT_INFORMATION(ctypes.Structure):
            _fields_ = [("BasicLimitInformation", JOBOBJECT_BASIC_LIMIT_INFORMATION),
                        ("IoInfo", IO_COUNTERS),
                        ("ProcessMemoryLimit", ctypes.c_size_t),
                        ("JobMemoryLimit", ctypes.c_size_t),
                        ("PeakProcessMemoryUsed", ctypes.c_size_t),
                        ("PeakJobMemoryUsed", ctypes.c_size_t)]

        job = kernel32.CreateJobObjectW(None, None)
        if not job:
            return

        info = JOBOBJECT_EXTENDED_LIMIT_INFORMATION()
        info.BasicLimitInformation.LimitFlags = JOB_OBJECT_LIMIT_PROCESS_MEMORY
        info.ProcessMemoryLimit = limit_bytes

        kernel32.SetInformationJobObject(
            job, JobObjectExtendedLimitInformation,
            ctypes.byref(info), ctypes.sizeof(info)
        )

        PROCESS_ALL_ACCESS = 0x1F0FFF
        h_process = kernel32.OpenProcess(PROCESS_ALL_ACCESS, False, process.pid)
        if h_process:
            kernel32.AssignProcessToJobObject(job, h_process)
    except Exception:
        pass  # Best-effort.


def execute_worker(python_executable, image_path, mask_path, out_path, plugin_dir, force_opencv_fallback=False):
    worker_code = f"""# -*- coding: utf-8 -*-
import sys
import os

try:
    import numpy as np
    import cv2
    import onnxruntime as ort
except ImportError as e:
    print(f"[CACHE_INVALIDATION_REQUIRED] Erreur d'import : {{e}}")
    sys.exit(1)

def process_image(img_in, mask_in, img_out, plugin_directory, force_opencv_fallback=False):
    # IMREAD_UNCHANGED preserve le canal alpha si l'image source en possede un.
    raw = cv2.imread(img_in, cv2.IMREAD_UNCHANGED)
    mask = cv2.imread(mask_in, cv2.IMREAD_GRAYSCALE)

    if raw is None or mask is None:
        print("Erreur : Impossible de lire les fichiers temporaires.", file=sys.stderr)
        sys.exit(1)

    has_alpha = (raw.ndim == 3 and raw.shape[2] == 4)
    if has_alpha:
        alpha_channel = raw[:, :, 3]
        img = raw[:, :, :3]
    else:
        alpha_channel = None
        img = raw if raw.ndim == 3 else cv2.cvtColor(raw, cv2.COLOR_GRAY2BGR)

    if img.shape[:2] != mask.shape[:2]:
        mask = cv2.resize(mask, (img.shape[1], img.shape[0]), interpolation=cv2.INTER_NEAREST)

    _, binary_mask = cv2.threshold(mask, 127, 255, cv2.THRESH_BINARY)

    if cv2.countNonZero(binary_mask) == 0:
        print("Erreur : Le masque genere est vide.", file=sys.stderr)
        sys.exit(1)

    # Dilatation de securite : une selection collant tres pres du bord reel
    # de l'objet (ou l'effleurant) laisserait sinon des pixels de l'objet
    # juste hors du masque. Le fondu de bord ci-dessous melangerait alors
    # ces pixels dans la zone de transition, faisant reapparaitre un
    # "fantome" translucide de l'objet qu'on cherche justement a effacer.
    # En elargissant legerement le masque, la zone de transition demarre
    # toujours en territoire sur, deja hors de l'objet.
    dilation_kernel = np.ones(({MASK_DILATION_PX} * 2 + 1, {MASK_DILATION_PX} * 2 + 1), np.uint8)
    binary_mask = cv2.dilate(binary_mask, dilation_kernel, iterations=1)

    # ---------------------------------------------------------
    # ADAPTATEUR TENSORIEL (LaMa ONNX)
    # ---------------------------------------------------------
    # Les modeles LaMa exportes en ONNX (FFC / DFT de Fourier) n'acceptent
    # generalement AUCUNE taille dynamique, meme si le graphe declare parfois
    # des dimensions symboliques dans ses metadonnees : la taille reellement
    # utilisable est figee au moment de l'export (512x512 pour lama_fp32.onnx
    # de Carve/LaMa-ONNX, recommande dans la documentation de ce greffon).
    # On tente donc plusieurs tailles candidates dans l'ordre, et on ne
    # bascule sur OpenCV que si aucune ne fonctionne.
    onnx_model_path = os.path.join(plugin_directory, "lama.onnx")
    result = None
    last_onnx_error = None

    if force_opencv_fallback:
        print("[ONNX_ERROR] Repli OpenCV force (timeout d'inference precedent).", file=sys.stdout)
    elif os.path.isfile(onnx_model_path):
        try:
            # ---------------------------------------------------------
            # VALIDATION STRUCTURELLE DU GRAPHE (avant tout chargement lourd)
            # ---------------------------------------------------------
            # On utilise le verificateur officiel du paquet "onnx" (parsing
            # Protobuf + regles de coherence ONNX) pour rejeter en amont les
            # graphes structurellement aberrants : dimensions de tenseurs
            # demesurees, nombre de noeuds absurde, ou operateurs personnalises
            # provenant d'un domaine non standard (ce qui bloquerait de toute
            # facon le chargement d'une bibliotheque externe malveillante,
            # puisque ce script n'appelle jamais register_custom_ops_library).
            # Cela ne supprime pas tout risque (le parseur Protobuf reste le
            # meme composant que celui utilise par le moteur d'inference),
            # mais ecarte les cas grossierement malveillants avant toute
            # allocation memoire consequente.
            try:
                import onnx as onnx_check
                onnx_model = onnx_check.load(onnx_model_path)
                onnx_check.checker.check_model(onnx_model, full_check=True)

                for opset in onnx_model.opset_import:
                    if opset.domain not in {ALLOWED_OPSET_DOMAINS!r}:
                        raise ValueError(f"Domaine d'operateur non standard refuse : '{{opset.domain}}'")

                if len(onnx_model.graph.node) > {MAX_GRAPH_NODES}:
                    raise ValueError(f"Graphe avec {{len(onnx_model.graph.node)}} noeuds, au-dela du plafond de securite.")

                for initializer in onnx_model.graph.initializer:
                    element_count = 1
                    for d in initializer.dims:
                        if d < 0:
                            raise ValueError("Dimension de tenseur negative refusee.")
                        element_count *= max(d, 1)
                    if element_count > {MAX_TENSOR_ELEMENTS}:
                        raise ValueError(
                            f"Tenseur '{{initializer.name}}' avec {{element_count}} elements, "
                            "au-dela du plafond de securite."
                        )
            except ImportError:
                # Le paquet onnx n'est pas installe (installation partielle) :
                # on ne bloque pas l'usage, mais on saute la validation.
                pass

            available = ort.get_available_providers()
            providers = [p for p in available if p != "CPUExecutionProvider"] + ["CPUExecutionProvider"]
            session = ort.InferenceSession(onnx_model_path, providers=providers)
            inputs = session.get_inputs()

            if len(inputs) < 2:
                raise ValueError("Le modele ONNX fourni ne possede pas d'entrees distinctes pour l'image et le masque.")

            img_name = inputs[0].name
            mask_name = inputs[1].name
            expected_shape = inputs[0].shape

            is_nhwc = False
            declared_h, declared_w = None, None

            if len(expected_shape) == 4:
                if expected_shape[3] == 3:
                    is_nhwc = True
                    if isinstance(expected_shape[1], int) and isinstance(expected_shape[2], int):
                        declared_h, declared_w = expected_shape[1], expected_shape[2]
                elif expected_shape[1] == 3:
                    is_nhwc = False
                    if isinstance(expected_shape[2], int) and isinstance(expected_shape[3], int):
                        declared_h, declared_w = expected_shape[2], expected_shape[3]

            h, w = img.shape[:2]

            # ---------------------------------------------------------
            # RECADRAGE INTELLIGENT AUTOUR DE LA SELECTION
            # ---------------------------------------------------------
            # Le modele n'accepte qu'une image 512x512. Pour une GRANDE
            # photo, redimensionner l'image ENTIERE degraderait toute la
            # photo et ecraserait les details fins dans le masque (cas de
            # l'eolienne). On recadre alors une zone locale autour de la
            # selection, avec du contexte.
            # Mais pour une image DEJA proche de 512x512, ce recadrage local
            # peut au contraire couper une structure importante proche de
            # l'objet (ex: l'arche d'une fenetre ronde) hors du champ vu par
            # l'IA, qui perd alors la comprehension geometrique de la scene.
            # Dans ce cas, mieux vaut traiter le cadre COMPLET, exactement
            # comme le fait la demo officielle du modele.
            h_full, w_full = h, w
            ys, xs = np.where(binary_mask > 0)
            bbox_x0, bbox_x1 = int(xs.min()), int(xs.max()) + 1
            bbox_y0, bbox_y1 = int(ys.min()), int(ys.max()) + 1
            bbox_w, bbox_h = bbox_x1 - bbox_x0, bbox_y1 - bbox_y0

            if max(w_full, h_full) <= {WHOLE_IMAGE_THRESHOLD}:
                # Image assez petite : le facteur de reduction vers 512x512
                # reste raisonnable meme en traitant tout le cadre, donc on
                # ne perd aucune structure de la scene.
                crop_x0, crop_y0, crop_x1, crop_y1 = 0, 0, w_full, h_full
            else:
                context_pad = max(80, int(max(bbox_w, bbox_h) * 0.75))

                # Plafond de taille : sans cela, un grand objet (ex: un mat
                # d'eolienne de 600px de haut) produirait un recadrage bien
                # plus grand que 512x512, impliquant une reduction/agrandissement
                # important (x3 ou plus) et donc une texture reconstruite floue
                # ou "en blocs" une fois remise a l'echelle reelle. On reduit la
                # marge de contexte si besoin pour rester proche de la
                # resolution native du modele, sans jamais rogner l'objet
                # lui-meme (le plancher de contexte descend a 20px seulement si
                # l'objet est deja plus grand que MAX_CROP_SIZE).
                max_object_dim = max(bbox_w, bbox_h)
                if max_object_dim + 2 * context_pad > {MAX_CROP_SIZE}:
                    context_pad = max(20, ({MAX_CROP_SIZE} - max_object_dim) // 2)

                crop_x0 = bbox_x0 - context_pad
                crop_y0 = bbox_y0 - context_pad
                crop_x1 = bbox_x1 + context_pad
                crop_y1 = bbox_y1 + context_pad

            # Rendre le recadrage carre (evite toute distorsion d'aspect au
            # redimensionnement vers 512x512) en etendant le cote le plus court.
            crop_w = crop_x1 - crop_x0
            crop_h = crop_y1 - crop_y0
            if crop_w > crop_h:
                extra = (crop_w - crop_h) // 2
                crop_y0 -= extra
                crop_y1 += (crop_w - crop_h) - extra
            elif crop_h > crop_w:
                extra = (crop_h - crop_w) // 2
                crop_x0 -= extra
                crop_x1 += (crop_h - crop_w) - extra

            # La zone peut deborder des limites reelles de l'image : on
            # complete par reflet (contexte visuel) plutot que de rogner,
            # pour conserver un recadrage carre sans distorsion. Le masque,
            # lui, est complete par du noir (aucune zone a effacer dans le
            # contexte ajoute).
            pad_left = max(0, -crop_x0)
            pad_top = max(0, -crop_y0)
            pad_right = max(0, crop_x1 - w)
            pad_bottom = max(0, crop_y1 - h)

            padded_img = cv2.copyMakeBorder(img, pad_top, pad_bottom, pad_left, pad_right, cv2.BORDER_REFLECT)
            padded_mask = cv2.copyMakeBorder(binary_mask, pad_top, pad_bottom, pad_left, pad_right,
                                              cv2.BORDER_CONSTANT, value=0)

            local_x0 = crop_x0 + pad_left
            local_y0 = crop_y0 + pad_top
            crop_img = padded_img[local_y0:local_y0 + crop_h, local_x0:local_x0 + crop_w]
            crop_mask = padded_mask[local_y0:local_y0 + crop_h, local_x0:local_x0 + crop_w]

            # Ordre des tentatives : la taille declaree dans le graphe (si
            # elle est vraiment statique), puis 512x512 (taille documentee
            # de reference pour ce modele), puis un pad dynamique au
            # multiple de 32 en tout dernier recours pour d'autres variantes
            # de LaMa qui accepteraient reellement une taille arbitraire.
            # Ces tentatives portent desormais sur le RECADRAGE, plus sur
            # l'image entiere.
            candidates = []
            if declared_h and declared_w:
                candidates.append(("fixed", declared_h, declared_w))
            if (declared_h, declared_w) != (512, 512):
                candidates.append(("fixed", 512, 512))
            candidates.append(("pad32", None, None))

            crop_result = None
            for mode, cand_h, cand_w in candidates:
                try:
                    if mode == "fixed":
                        work_img = cv2.resize(crop_img, (cand_w, cand_h), interpolation=cv2.INTER_LANCZOS4)
                        work_mask = cv2.resize(crop_mask, (cand_w, cand_h), interpolation=cv2.INTER_NEAREST)
                        inner_pad_h, inner_pad_w = 0, 0
                    else:
                        pad_size = 32
                        inner_pad_h = (pad_size - crop_h % pad_size) % pad_size
                        inner_pad_w = (pad_size - crop_w % pad_size) % pad_size
                        work_img = cv2.copyMakeBorder(crop_img, 0, inner_pad_h, 0, inner_pad_w, cv2.BORDER_REFLECT)
                        work_mask = cv2.copyMakeBorder(crop_mask, 0, inner_pad_h, 0, inner_pad_w, cv2.BORDER_REFLECT)

                    work_img_rgb = cv2.cvtColor(work_img, cv2.COLOR_BGR2RGB)
                    img_tensor = work_img_rgb.astype(np.float32) / 255.0
                    mask_tensor = work_mask.astype(np.float32) / 255.0

                    if is_nhwc:
                        img_tensor = np.expand_dims(img_tensor, axis=0)
                        mask_tensor = np.expand_dims(mask_tensor, axis=-1)
                        mask_tensor = np.expand_dims(mask_tensor, axis=0)
                    else:
                        img_tensor = np.transpose(img_tensor, (2, 0, 1))
                        img_tensor = np.expand_dims(img_tensor, axis=0)
                        mask_tensor = np.expand_dims(mask_tensor, axis=0)
                        mask_tensor = np.expand_dims(mask_tensor, axis=0)

                    onnx_inputs = {{
                        img_name: img_tensor,
                        mask_name: mask_tensor
                    }}

                    outputs = session.run(None, onnx_inputs)
                    output_tensor = outputs[0]

                    if len(output_tensor.shape) == 4:
                        output_tensor = output_tensor[0]

                    if output_tensor.shape[0] == 3:
                        output_img = np.transpose(output_tensor, (1, 2, 0))
                    else:
                        output_img = output_tensor

                    if np.max(output_img) <= 1.0:
                        output_img = output_img * 255.0

                    output_img = np.clip(output_img, 0, 255).astype(np.uint8)
                    result_ai = cv2.cvtColor(output_img, cv2.COLOR_RGB2BGR)

                    if mode == "fixed":
                        crop_result = cv2.resize(result_ai, (crop_w, crop_h), interpolation=cv2.INTER_LANCZOS4)
                    else:
                        crop_result = result_ai[:crop_h, :crop_w] if (inner_pad_h > 0 or inner_pad_w > 0) else result_ai

                    print("[ONNX_SUCCESS]")
                    break
                except Exception as e:
                    last_onnx_error = str(e)
                    crop_result = None
                    continue

            if crop_result is None:
                raise RuntimeError(last_onnx_error or "Toutes les tailles candidates ont echoue.")

            # ---------------------------------------------------------
            # REINJECTION : uniquement la zone recadree, avec fondu au bord
            # de la selection reelle. Tout le reste de l'image reste
            # strictement identique aux pixels d'origine.
            # ---------------------------------------------------------
            result = img.copy()

            # Fondu doux sur les bords de la selection (evite une couture
            # visible), sans jamais toucher aux pixels hors selection.
            mask_crop_f32 = crop_mask.astype(np.float32) / 255.0
            feather_px = max(3, min(15, context_pad // 4))
            k = feather_px * 2 + 1
            mask_feathered = cv2.GaussianBlur(mask_crop_f32, (k, k), 0)
            mask_feathered = np.clip(mask_feathered, 0.0, 1.0)[:, :, None]

            blended_crop = (crop_result.astype(np.float32) * mask_feathered +
                            crop_img.astype(np.float32) * (1.0 - mask_feathered)).astype(np.uint8)

            # On ne reecrit que la portion du recadrage qui correspond a de
            # vrais pixels de l'image (pas le contexte ajoute par reflet).
            write_x0, write_y0 = max(0, crop_x0), max(0, crop_y0)
            write_x1, write_y1 = min(w, crop_x1), min(h, crop_y1)
            src_x0, src_y0 = write_x0 - crop_x0, write_y0 - crop_y0
            src_x1, src_y1 = src_x0 + (write_x1 - write_x0), src_y0 + (write_y1 - write_y0)

            result[write_y0:write_y1, write_x0:write_x1] = blended_crop[src_y0:src_y1, src_x0:src_x1]
        except Exception as e:
            result = None
            print(f"[ONNX_ERROR] {{str(e)}}", file=sys.stdout)

    if result is None:
        # ---------------------------------------------------------
        # FALLBACK OPENCV (Mode Secours Automatique)
        # ---------------------------------------------------------
        result = cv2.inpaint(img, binary_mask, 15, cv2.INPAINT_NS)

    if has_alpha:
        result = cv2.cvtColor(result, cv2.COLOR_BGR2BGRA)
        result[:, :, 3] = alpha_channel

    cv2.imwrite(img_out, result)

if __name__ == "__main__":
    if len(sys.argv) not in (5, 6):
        sys.exit(1)
    force_fallback = (len(sys.argv) == 6 and sys.argv[5] == "1")
    process_image(sys.argv[1], sys.argv[2], sys.argv[3], sys.argv[4], force_fallback)
    sys.exit(0)
"""
    def _launch_and_wait(argv, timeout_s):
        with tempfile.NamedTemporaryFile(mode='w', delete=False, suffix='.log') as log_f:
            log_path = log_f.name
        try:
            popen_kwargs = dict(_popen_kwargs())
            if os.name != 'nt':
                # Plafond memoire best-effort (POSIX) applique juste avant exec().
                popen_kwargs['preexec_fn'] = _posix_memory_limit_preexec(WORKER_MEMORY_LIMIT_BYTES)

            with open(log_path, 'w') as log_f:
                process = subprocess.Popen(
                    argv, env=env_clean(), stdout=log_f, stderr=subprocess.STDOUT,
                    text=True, encoding="utf-8", errors="replace", **popen_kwargs
                )

                # Plafond memoire best-effort (Windows), applique juste apres
                # la creation du processus.
                _assign_windows_job_memory_limit(process, WORKER_MEMORY_LIMIT_BYTES)

                start_time = time.time()
                timed_out = False
                while process.poll() is None:
                    Gimp.progress_pulse()
                    time.sleep(0.2)
                    if time.time() - start_time > timeout_s:
                        timed_out = True
                        process.kill()
                        process.wait()
                        break

            with open(log_path, 'r', errors='replace') as log_f:
                output = log_f.read()
            return process.returncode, output, timed_out
        finally:
            try:
                os.remove(log_path)
            except OSError:
                pass

    # Nom de fichier unique (pas de nom fixe) : evite toute collision si
    # deux instances de GIMP lancent le greffon en meme temps, et permet
    # un nettoyage sur (et par) tempfile lui-meme.
    fd, worker_path = tempfile.mkstemp(prefix=f"{PLUGIN_ID}_worker_", suffix=".py")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            f.write(worker_code)

        Gimp.progress_set_text("Traitement IA (Inpainting)...")
        initial_argv = [python_executable, worker_path, image_path, mask_path, out_path, plugin_dir]
        if force_opencv_fallback:
            initial_argv.append("1")
        rc, output, timed_out = _launch_and_wait(initial_argv, INFERENCE_TIMEOUT)

        if timed_out:
            # Le calcul a depasse le plafond de temps autorise (protection
            # anti boucle infinie / DoS). On ne fait plus confiance au
            # modele pour cette execution : on relance le worker en mode
            # "repli OpenCV force", qui ne touche jamais au fichier .onnx.
            Gimp.progress_set_text("Délai IA dépassé, repli sécurisé sur OpenCV...")
            rc, output, timed_out2 = _launch_and_wait(
                [python_executable, worker_path, image_path, mask_path, out_path, plugin_dir, "1"],
                timeout_s=180
            )
            if timed_out2:
                return False, (
                    "Le traitement a été interrompu deux fois pour dépassement de délai "
                    "(y compris en mode de secours OpenCV). Réessayez avec une sélection "
                    "plus petite ou une image de résolution inférieure."
                ), False, ""
            stdout = output
        else:
            stdout = output
    finally:
        if os.path.isfile(worker_path):
            try:
                os.remove(worker_path)
            except OSError:
                pass

    if "[CACHE_INVALIDATION_REQUIRED]" in stdout:
        return False, "Le worker a perdu ses modules. Le cache a été réinitialisé.", False, ""
    if rc != 0:
        return False, f"Échec critique du processus externe :\n{stdout}", False, ""

    is_ai_success = "[ONNX_SUCCESS]" in stdout
    onnx_error = ""
    for line in stdout.splitlines():
        if line.startswith("[ONNX_ERROR]"):
            onnx_error = line.replace("[ONNX_ERROR]", "").strip()
            break

    return True, "", is_ai_success, onnx_error


def safe_export(image, drawables, gio_file):
    try:
        Gimp.file_save(Gimp.RunMode.NONINTERACTIVE, image, gio_file)
        return
    except TypeError:
        pass
    try:
        Gimp.file_save(Gimp.RunMode.NONINTERACTIVE, image, drawables, gio_file)
        return
    except TypeError:
        pass
    try:
        Gimp.file_save(Gimp.RunMode.NONINTERACTIVE, image, gio_file, drawables)
        return
    except TypeError:
        pass
    try:
        Gimp.file_save(Gimp.RunMode.NONINTERACTIVE, image, drawables[0], gio_file)
        return
    except TypeError as e:
        raise RuntimeError(f"Impossible d'exporter l'image. Signature API GIMP inconnue : {str(e)}")


class DeepErasePlugin(Gimp.PlugIn):
    def do_query_procedures(self):
        return [PROCEDURE_NAME]

    def do_create_procedure(self, name):
        procedure = Gimp.ImageProcedure.new(self, name, Gimp.PDBProcType.PLUGIN, self.run, None)
        procedure.set_image_types("RGB*, GRAY*")
        procedure.set_menu_label("Deep Erase...")
        procedure.add_menu_path("<Image>/Filters/Enhance")
        return procedure

    def run(self, procedure, run_mode, image, drawables, config, run_data):
        temp_files = []
        try:
            if len(drawables) != 1:
                Gimp.message("Veuillez sélectionner un seul calque actif.")
                return procedure.new_return_values(Gimp.PDBStatusType.CALLING_ERROR, GLib.Error())

            try:
                is_empty = Gimp.Selection.is_empty(image)
            except AttributeError:
                is_empty = image.get_selection().is_empty()

            if is_empty:
                Gimp.message("Aucune zone sélectionnée. Veuillez utiliser l'outil de sélection autour de l'élément.")
                return procedure.new_return_values(Gimp.PDBStatusType.CALLING_ERROR, GLib.Error())

            GimpUi.init(PLUGIN_ID)

            if run_mode == Gimp.RunMode.INTERACTIVE:
                dialog = GimpUi.ProcedureDialog.new(procedure, config)
                dialog.fill(None)
                if not dialog.run():
                    dialog.destroy()
                    return procedure.new_return_values(Gimp.PDBStatusType.CANCEL, GLib.Error())
                dialog.destroy()

            Gimp.progress_init("Deep Erase Initialisation...")
            image.undo_group_start()
            Gimp.context_push()

            saved_sel = None
            temp_mask_layer = None
            cache_path = os.path.join(Gimp.directory(), CACHE_FILE)

            try:
                # Garde-fou : une selection couvrant (presque) toute l'image
                # produira une image entierement "inventee" par l'IA plutot
                # qu'une reparation locale. On previent sans bloquer, car un
                # tel usage peut etre volontaire (ex: regeneration de fond).
                try:
                    bounds_result = Gimp.Selection.bounds(image)
                except AttributeError:
                    bounds_result = image.get_selection().bounds(image)
                # Selon la version/le binding GI, le booleen de retour C
                # ("succes") est parfois conserve en tete du tuple en plus
                # du "non_empty" applicatif : on gere les deux cas (5 ou 6
                # valeurs) plutot que de presumer un format fixe.
                bounds_result = tuple(bounds_result)
                if len(bounds_result) == 6:
                    _, non_empty, x1, y1, x2, y2 = bounds_result
                elif len(bounds_result) == 5:
                    non_empty, x1, y1, x2, y2 = bounds_result
                else:
                    raise RuntimeError(
                        f"Format de retour inattendu pour Selection.bounds : {len(bounds_result)} valeurs."
                    )
                # x1,y1,x2,y2 sont les coins (haut-gauche / bas-droite), pas
                # une largeur/hauteur directe : w = x2-x1, h = y2-y1.
                w = max(0, x2 - x1)
                h = max(0, y2 - y1)
                img_area = image.get_width() * image.get_height()
                sel_area = w * h
                if img_area > 0 and (sel_area / img_area) >= SELECTION_COVERAGE_WARN:
                    Gimp.message(
                        "Attention : la sélection couvre la quasi-totalité de l'image. "
                        "Le résultat sera en grande partie généré par l'IA plutôt que "
                        "reconstruit à partir du contexte local."
                    )

                valid_python = None
                if os.path.isfile(cache_path):
                    with open(cache_path, 'r') as f:
                        cached_env = f.read().strip()
                        if test_python_executable(cached_env):
                            valid_python = cached_env

                if not valid_python:
                    sys_python = find_system_python()
                    if not sys_python:
                        raise RuntimeError("Aucun Python système valide trouvé hors de GIMP. Veuillez installer Python 3.")
                    valid_python = setup_venv(sys_python)
                    with open(cache_path, 'w') as f:
                        f.write(valid_python)

                tmp_dir = tempfile.gettempdir()
                path_in = os.path.join(tmp_dir, f"{PLUGIN_ID}_in_{os.getpid()}.png")
                path_mask = os.path.join(tmp_dir, f"{PLUGIN_ID}_mask_{os.getpid()}.png")
                path_out = os.path.join(tmp_dir, f"{PLUGIN_ID}_out_{os.getpid()}.png")
                temp_files.extend([path_in, path_mask, path_out])

                file_in = Gio.File.new_for_path(path_in)
                file_mask = Gio.File.new_for_path(path_mask)
                file_out = Gio.File.new_for_path(path_out)

                Gimp.progress_set_text("Exportation de l'image source...")
                try:
                    saved_sel = Gimp.Selection.save(image)
                except AttributeError:
                    saved_sel = image.selection_save()

                try:
                    Gimp.Selection.none(image)
                except AttributeError:
                    image.select_none()

                safe_export(image, drawables, file_in)

                Gimp.progress_set_text("Génération et exportation du masque...")
                base_type = image.get_base_type()
                layer_type = Gimp.ImageType.RGBA_IMAGE if base_type == Gimp.ImageBaseType.RGB else Gimp.ImageType.GRAYA_IMAGE

                temp_mask_layer = Gimp.Layer.new(image, "temp_mask", image.get_width(), image.get_height(), layer_type, 100.0, Gimp.LayerMode.NORMAL)

                try:
                    image.insert_layer(temp_mask_layer, None, 0)
                except AttributeError:
                    image.add_layer(temp_mask_layer, 0)

                Gimp.context_set_background(Gegl.Color.new("black"))
                temp_mask_layer.edit_fill(Gimp.FillType.BACKGROUND)

                try:
                    Gimp.Selection.load(saved_sel)
                except AttributeError:
                    image.select_item(Gimp.ChannelOps.REPLACE, saved_sel)

                Gimp.context_set_background(Gegl.Color.new("white"))
                temp_mask_layer.edit_fill(Gimp.FillType.BACKGROUND)

                try:
                    Gimp.Selection.none(image)
                except AttributeError:
                    image.select_none()

                safe_export(image, [temp_mask_layer], file_mask)

            finally:
                if temp_mask_layer and image.is_valid():
                    image.remove_layer(temp_mask_layer)
                if saved_sel and image.is_valid():
                    try:
                        image.remove_channel(saved_sel)
                    except Exception:
                        pass

            plugin_dir = os.path.dirname(os.path.realpath(__file__))
            model_path = os.path.join(plugin_dir, "lama.onnx")
            force_fallback_pretreatment = False
            if os.path.isfile(model_path):
                size_ok, size_msg = is_model_file_size_sane(model_path)
                if not size_ok:
                    Gimp.message(
                        f"Fichier modèle IA suspect, ignoré par précaution : {size_msg}\n"
                        "Basculement sur le moteur de secours OpenCV."
                    )
                    force_fallback_pretreatment = True
                else:
                    changed, trust_msg = check_model_trust(model_path)
                    if changed:
                        Gimp.message(trust_msg)

            Gimp.progress_set_text("Traitement IA (Inpainting)...")
            success, error_msg, is_ai_used, onnx_error = execute_worker(
                valid_python, path_in, path_mask, path_out, plugin_dir,
                force_opencv_fallback=force_fallback_pretreatment
            )

            if not success:
                if "Cache corrompu" in error_msg or "perdu ses modules" in error_msg:
                    if os.path.isfile(cache_path):
                        os.remove(cache_path)
                raise RuntimeError(error_msg)

            if not is_ai_used and os.path.isfile(model_path):
                if "DFT" in onnx_error and ("one-sided" in onnx_error or "ShapeInferenceError" in onnx_error):
                    Gimp.message(
                        "Le modèle IA a échoué sur un problème connu de l'opérateur DFT (Fourier). "
                        "Cela correspond très probablement à une confusion de fichier sur Hugging Face : "
                        "le dépôt Carve/LaMa-ONNX contient deux fichiers, « lama_fp32.onnx » (recommandé) "
                        "et « lama.onnx » (non recommandé, connu pour ce bug précis). Si vous avez "
                        "téléchargé directement le fichier déjà nommé « lama.onnx », remplacez-le par "
                        "« lama_fp32.onnx » renommé.\n\n"
                        f"Détail technique : {onnx_error}"
                    )
                else:
                    Gimp.message(
                        "Information technique : Le modèle IA a rencontré une dimension inattendue "
                        f"et a basculé sur le moteur de secours (OpenCV) pour éviter un crash.\n\nErreur du modèle : {onnx_error}"
                    )

            Gimp.progress_set_text("Finalisation...")
            if os.path.isfile(path_out):
                try:
                    if hasattr(Gimp, 'file_load_layers'):
                        new_layers = Gimp.file_load_layers(Gimp.RunMode.NONINTERACTIVE, image, file_out)
                        for layer in new_layers:
                            if hasattr(image, 'insert_layer'):
                                image.insert_layer(layer, None, 0)
                            else:
                                image.add_layer(layer, 0)

                            if is_ai_used:
                                layer.set_name("Deep Erase Result (IA LaMa)")
                            else:
                                layer.set_name("Resultat (Mode Secours OpenCV)")
                except Exception as e:
                    raise RuntimeError(f"Échec du rechargement de l'image traitée : {str(e)}")

            return procedure.new_return_values(Gimp.PDBStatusType.SUCCESS, GLib.Error())

        except Exception as e:
            Gimp.message(f"Erreur Globale Deep Erase : {str(e)}")
            return procedure.new_return_values(Gimp.PDBStatusType.EXECUTION_ERROR, GLib.Error())

        finally:
            for p in temp_files:
                if p and os.path.isfile(p):
                    try:
                        os.remove(p)
                    except OSError:
                        pass
            Gimp.context_pop()
            image.undo_group_end()
            Gimp.progress_end()


if __name__ == "__main__":
    Gimp.main(DeepErasePlugin.__gtype__, sys.argv)
