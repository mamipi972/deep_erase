# 🪄 Deep Erase pour GIMP 3.0

**Deep Erase** est un greffon (plugin) de retouche avancée pour **GIMP 3.0** qui utilise l'Intelligence Artificielle pour supprimer des éléments indésirables de vos photographies (*inpainting*) et reconstituer le fond de manière hyper-réaliste.

Propulsé par le modèle IA **LaMa** (*Large Mask Inpainting*) via **ONNX Runtime**, ce greffon bénéficie d'une architecture *« Crash-Proof »* : l'IA s'exécute dans un environnement totalement isolé, garantissant que GIMP ne plantera jamais — même en cas de problème avec le modèle.

> 🇬🇧 A version anglaise de ce document est disponible dans [`README.md`](README.md).

---

## ✨ Fonctionnalités

- **IA générative de pointe** — utilise le modèle LaMa pour une reconstitution intelligente des textures complexes (eau, sable, briques, horizon...).
- **Architecture isolée « sous-marin »** — le calcul lourd s'exécute hors du processus de GIMP, dans un environnement virtuel Python dédié. Un plantage ou une erreur dans le moteur IA ne peut jamais entraîner GIMP dans sa chute.
- **Mode de secours automatique** — si le modèle IA est absent, corrompu, ou échoue pour une raison quelconque, le greffon bascule silencieusement sur l'algorithme Navier-Stokes d'OpenCV pour ne jamais perdre votre travail.
- **Zéro configuration manuelle** — le greffon crée lui-même son environnement virtuel et télécharge ses dépendances Python, contournant nativement la restriction PEP 668 (« *externally-managed-environment* ») de Linux.
- **Durci par conception** — voir la section [Sécurité](#-notes-de-sécurité) ci-dessous pour savoir ce qui est fait afin qu'un fichier de modèle IA corrompu ou malveillant ne puisse pas nuire à votre système.

---

## 🛠️ Prérequis

- **GIMP 3.0** (Release Candidate ou version finale).
- **Une installation Python 3 standard en 64 bits**, distincte de celle intégrée dans GIMP. **CPython 3.10 à 3.12 depuis [python.org](https://www.python.org/) est recommandé.**
  - Les versions 32 bits ou ARM de Python **ne sont pas prises en charge** : les bibliothèques scientifiques utilisées par ce greffon (NumPy, OpenCV, ONNX Runtime) ne fournissent pas de paquets précompilés pour ces architectures, et le greffon refuse volontairement de les compiler depuis les sources (voir [Sécurité](#-notes-de-sécurité)).
  - Une version de Python très récente ou en pré-version (sortie depuis quelques mois seulement) peut ne pas encore disposer de paquets compatibles publiés par NumPy/OpenCV/ONNX Runtime. Si c'est votre cas, installez plutôt une version stable un peu plus ancienne (3.10 à 3.12).
- **Une connexion internet, uniquement lors du tout premier lancement** — le greffon télécharge environ 150 à 200 Mo de paquets Python la première fois qu'il s'exécute. Les lancements suivants fonctionnent entièrement hors ligne.

---

## 🚀 Installation

GIMP 3.0 est très strict sur l'arborescence des dossiers de greffons. Suivez ces étapes attentivement.

### Étape 1 : Préparer le dossier du greffon

1. Ouvrez GIMP et allez dans **Édition ▸ Préférences ▸ Dossiers ▸ Greffons**.
2. Ouvrez le dossier de greffons de votre utilisateur (le chemin se terminant généralement par `plug-ins`).
3. Créez un nouveau dossier nommé **EXACTEMENT** `deep_erase` (en minuscules, avec le tiret du bas).
4. Placez le script `deep_erase.py` à l'intérieur de ce dossier.

Votre arborescence doit ressembler à ceci :

```
.../plug-ins/deep_erase/deep_erase.py
```

> ⚠️ **GIMP 3.0 exige que le nom du dossier corresponde exactement au nom du script (sans extension)**, et ceci est **sensible à la casse sous Linux**. Si votre navigateur a renommé le fichier téléchargé en quelque chose comme `deep_erase (1).py`, GIMP l'ignorera silencieusement.

### Étape 2 : Installer le « Cerveau » IA (le modèle LaMa)

1. Rendez-vous sur le dépôt officiel du modèle sur Hugging Face : [**Carve/LaMa-ONNX**](https://huggingface.co/Carve/LaMa-ONNX).
2. Téléchargez le fichier **`lama_fp32.onnx`** (environ 200 Mo).

   > 🚨 **Important — à lire attentivement :** ce dépôt contient **deux fichiers différents** : `lama_fp32.onnx` (**recommandé**, stable) et `lama.onnx` (**non recommandé** — il utilise une chaîne d'export différente comportant un bug connu dans son opérateur de transformée de Fourier, et échouera avec une erreur mentionnant `DFT` / `one-sided DFT`). **Assurez-vous de bien télécharger `lama_fp32.onnx`, et non le fichier déjà nommé `lama.onnx`.**

3. Placez ce fichier dans votre dossier `deep_erase`.
4. **Renommez-le en `lama.onnx`** (tout en minuscules).

Votre arborescence finale doit être :

```
plug-ins/
└── deep_erase/
    ├── deep_erase.py
    └── lama.onnx        (il s'agit de lama_fp32.onnx, renommé)
```

### Étape 3 : Autorisations spécifiques (Linux & macOS uniquement)

Sur les systèmes UNIX, GIMP ignore silencieusement les scripts qui n'ont pas les droits d'exécution, **sans aucun message d'erreur**.

```bash
cd chemin/vers/plug-ins/deep_erase
chmod +x deep_erase.py
```

> ⚠️ Si vous avez téléchargé le fichier `.py` depuis Windows puis l'avez déplacé vers Linux/macOS, assurez-vous que ses fins de ligne soient au format **LF**, et non **CRLF** — sinon GIMP échouera à l'exécuter, là encore sans aucune erreur visible.

---

## 🎨 Utilisation

Lors de la toute première exécution, le greffon prendra quelques minutes pour créer son environnement virtuel et télécharger ses dépendances (NumPy, OpenCV, ONNX Runtime, ONNX). Une barre de progression vous tiendra informé. Les exécutions suivantes ne prendront que 5 à 15 secondes.

1. Ouvrez votre image dans GIMP.
2. Utilisez un outil de sélection (Lasso, Rectangle, etc.) pour entourer l'élément à supprimer — laissez la sélection déborder légèrement sur le fond environnant pour aider l'IA.
3. Assurez-vous d'avoir sélectionné le bon calque actif dans le panneau des calques.
4. Allez dans le menu : **Filtres ▸ Amélioration ▸ Deep Erase...**
5. Patientez. Le greffon créera un nouveau calque :
   - **`Deep Erase Result (IA LaMa)`** si le modèle IA a fonctionné avec succès.
   - **`Resultat (Mode Secours OpenCV)`** si le greffon a basculé sur l'algorithme classique (voir [Dépannage](#-dépannage-faq)).

---

## 🔒 Notes de sécurité

Comme ce greffon exécute sur votre machine un fichier de modèle IA tiers, plusieurs couches de protection sont en place contre un fichier `lama.onnx` corrompu ou malveillant :

| Protection | Ce qu'elle fait |
|---|---|
| **Installation par paquets précompilés uniquement** | Les dépendances Python sont installées avec `--only-binary`, ce qui signifie qu'**aucun compilateur n'est jamais invoqué** sur votre machine. Cela ferme toute une classe d'attaques de la chaîne d'approvisionnement par compilation depuis les sources, et évite d'avoir besoin de Visual Studio / Xcode / build-essential. |
| **Validation structurelle du graphe** | Avant que le modèle ne soit chargé dans le moteur d'inférence, sa structure est vérifiée (via le paquet officiel `onnx`) : dimensions des tenseurs, nombre de nœuds, et domaines d'opérateurs déclarés sont tous contrôlés par rapport à des limites raisonnables. |
| **Aucun opérateur personnalisé, jamais** | Le greffon n'appelle jamais l'API de chargement d'opérateurs personnalisés d'ONNX Runtime : un fichier de modèle ne peut donc pas provoquer le chargement d'une `.dll`/`.so` externe — c'est garanti structurellement, pas seulement par omission. |
| **Délai maximal strict sur l'inférence** | Le calcul IA est plafonné à 15 minutes. En cas de dépassement (par exemple un modèle malformé provoquant un calcul sans fin), le processus est tué et le greffon retente automatiquement en mode OpenCV sécurisé, sans plus jamais toucher au fichier du modèle. |
| **Plafond mémoire** | Le sous-processus de calcul s'exécute sous une limite mémoire best-effort (4 Go), aussi bien sous Windows que sous les systèmes POSIX, pour contenir une consommation mémoire excessive. |
| **Isolation des processus** | Le calcul IA s'exécute toujours dans un sous-processus séparé, dans un environnement virtuel dédié, avec un interpréteur qui n'est jamais le Python intégré de GIMP. Un plantage à cet endroit ne peut pas faire planter GIMP. |
| **Vérification de confiance à la première utilisation** | Le greffon mémorise l'empreinte SHA-256 du fichier de modèle lors de sa première utilisation réussie, et vous avertit si ce fichier change de manière inattendue lors d'une exécution ultérieure. |

**Rien de tout cela ne remplace le fait de ne télécharger le modèle que depuis le dépôt officiel [Carve/LaMa-ONNX](https://huggingface.co/Carve/LaMa-ONNX).** Merci de ne pas utiliser de fichiers `.onnx` provenant de sources non officielles ou non fiables.

---

## 🚨 Dépannage (FAQ)

### Le greffon n'apparaît pas dans le menu « Filtres »

- **Windows :** vérifiez que le dossier s'appelle bien `deep_erase` et le fichier `deep_erase.py`. GIMP 3.0 exige une correspondance exacte.
- **Linux/macOS :** avez-vous fait le `chmod +x` ? Pour voir la véritable erreur, lancez GIMP depuis un terminal (`gimp` ou `gimp-2.99`) — GIMP affiche la vraie erreur Python de démarrage dans la console, qui n'apparaît jamais dans l'interface graphique.
- Vérifiez que le fichier n'a pas été renommé par votre navigateur (ex. `deep_erase (1).py`) ou enregistré avec des fins de ligne CRLF après un transfert Windows → Linux.

### L'installation de NumPy / OpenCV / ONNX Runtime échoue

Si le message d'erreur mentionne un compilateur manquant (`cl.exe`, `gcc`, `Meson`, « Unknown compiler(s) »...), cela signifie qu'aucun paquet précompilé (*wheel*) n'a été trouvé pour votre interpréteur Python exact — généralement parce que :
- Votre Python est en **32 bits** ou en **ARM**, ou
- Votre version de Python est **très récente** et les bibliothèques n'ont pas encore publié de paquets compatibles.

Le message d'erreur inclut un bloc de diagnostic avec votre version de Python et son architecture — vérifiez-le, et si besoin, installez un **CPython 64 bits standard, version 3.10 à 3.12**, depuis [python.org](https://www.python.org/).

### L'installation semble bloquée, ou expire après plusieurs minutes

Cela signifie généralement que votre connexion réseau, un pare-feu, ou un proxy d'entreprise bloque l'accès à PyPI (l'index de paquets de `pip`). Vérifiez votre connexion et vos éventuels réglages de proxy.

### Le calque de résultat s'appelle « Resultat (Mode Secours OpenCV) » au lieu du nom IA

Cela signifie que le modèle IA a échoué et que le greffon a basculé en toute sécurité sur l'algorithme classique OpenCV. Lisez le message d'avertissement détaillé affiché par GIMP — il explique la raison précise (voir ci-dessous pour la cause la plus fréquente).

### Erreur mentionnant `DFT` / « one-sided DFT requires real input »

Vous avez très probablement téléchargé le mauvais fichier depuis le dépôt Hugging Face. Il y a deux fichiers dans ce dépôt — `lama_fp32.onnx` (le bon) et `lama.onnx` (déjà nommé ainsi, mais **pas** celui qu'il faut). Retéléchargez précisément `lama_fp32.onnx` et renommez-le vous-même en `lama.onnx`. Voir l'[Étape 2](#étape-2--installer-le-cerveau-ia-le-modèle-lama) ci-dessus.

### Un message indique que « le fichier de modèle a changé depuis la dernière utilisation réussie »

C'est normal et sans danger si vous avez volontairement remplacé `lama.onnx` (par exemple pour corriger le problème ci-dessus). Si vous n'avez **pas** remplacé le fichier, cela peut indiquer une corruption — retéléchargez-le depuis la source officielle.

### Le traitement prend très longtemps ou atteint le délai de 15 minutes

Les images ou sélections très grandes peuvent être lentes sur une inférence limitée au CPU. Essayez une sélection plus petite, ou réduisez la résolution de l'image avant de lancer le filtre.

### Repartir d'un état totalement propre

Si vous êtes bloqué, vous pouvez forcer une réinitialisation complète :
1. Supprimez le dossier `deep_erase_venv` à l'intérieur de `deep_erase/`.
2. Supprimez `deep_erase_python_cache.txt` et `deep_erase_model_trust.json` dans le dossier de configuration utilisateur de GIMP (**Édition ▸ Préférences ▸ Dossiers**, dossier racine indiqué là-bas).
3. Relancez GIMP et réessayez le filtre.

---

## Crédits

- Modèle et publication originale LaMa : [advimman/lama](https://github.com/advimman/lama)
- Export ONNX utilisé par ce greffon : [Carve-Photos/lama](https://github.com/Carve-Photos/lama) et [Carve/LaMa-ONNX](https://huggingface.co/Carve/LaMa-ONNX) sur Hugging Face
- Inférence : [ONNX Runtime](https://onnxruntime.ai/) (Microsoft)
- Algorithme de secours classique : [OpenCV](https://opencv.org/) (inpainting Navier-Stokes)

## Licence

Développé pour GIMP 3.0 — Python 3 (PyGObject). Consultez le fichier LICENSE de ce dépôt pour les conditions de licence du greffon lui-même. Le modèle ONNX LaMa est distribué séparément sous licence Apache 2.0 par ses auteurs — consultez la page [Carve/LaMa-ONNX](https://huggingface.co/Carve/LaMa-ONNX) pour plus de détails.
