#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
 genpkg.py — Gerenciador de pacotes a partir de receitas (YAML) para Linux From Scratch-like
 Autoridade da recipe: baixar → extrair → (opcional) aplicar patches → executar comandos →
 instalar em DESTDIR → empacotar (.tar.gz) → instalar no root alvo → registrar arquivos.

 Funcionalidades principais
 -------------------------
 • Variáveis/diretórios base: REPO, RECIPES, SOURCES, PATCHES, DESTDIR, PACKAGES, LOGS, BIN_DIR
 • Download via HTTPS (requests se disponível; fallback wget/curl) e suporte a git (git+https)
 • Aplicação de patches (lista de URLs HTTPS) com `patch` (sistema)
 • Build com comandos definidos pela recipe (qualquer buildsystem)
 • DESTDIR + (opcional) fakeroot
 • Empacotamento .tar.gz do DESTDIR ANTES da instalação no root
 • Instalação em ROOT alvo (padrão "/"; pode alterar via CLI --root)
 • Registro completo dos arquivos instalados para remoção segura (installed.json)
 • Hooks: pre_install, post_install, pre_remove, post_remove
 • Strip opcional (--strip)
 • Sync do repositório Git de recipes + index e busca em subpastas
 • Limpeza de diretórios de trabalho (clean)
 • Upgrade de 1 pacote ou de todos (--all)
 • Spinner para operações longas
 • Cópia de binários do DESTDIR para BIN_DIR (~/.genpkg/bin) para uso rápido
 • Logs por pacote em LOGS_DIR
 • CLI com abreviações: install(i), build(b), remove(r), list(l), search(s)
 • Info do pacote com ícones [✔]/[ ]

 Requisitos de sistema:
  - Python 3.8+
  - git, patch, tar, (opcional) fakeroot, (opcional) strip
  - PyYAML (yaml) | instale: pip install pyyaml
  - (opcional) requests | pip install requests

 Observação importante sobre instalação em "/":
  - Extrair/instalar em "/" requer permissões de root. Execute com sudo OU use --root ./rootfs para testar sem root.
"""
from __future__ import annotations
import os
import sys
import json
import tarfile
import shutil
import time
import threading
import itertools
import argparse
import subprocess
from pathlib import Path
from typing import Dict, List, Optional

# ====== Cores ANSI simples (sem dependências externas) ======
class Color:
    RESET="\033[0m"; BOLD="\033[1m"
    RED="\033[91m"; GREEN="\033[92m"; YELLOW="\033[93m"; BLUE="\033[94m"; CYAN="\033[96m"; MAGENTA="\033[95m"; WHITE="\033[97m"

def c(text, color):
    return f"{color}{text}{Color.RESET}"

# ====== Variáveis globais / diretórios ======
APP_NAME = "genpkg"
APP_VERSION = "1.0.0"

# Base pode ser definida via env GENPKG_BASE; default: diretório atual do script
BASE_DIR = Path(os.environ.get("GENPKG_BASE", Path.cwd()))
REPO_DIR = Path(os.environ.get("REPO", BASE_DIR / "repo"))
RECIPES_DIR = REPO_DIR / "recipes"
RECIPE_INDEX = REPO_DIR / "index.json"
SOURCES_DIR = Path(os.environ.get("SOURCES", BASE_DIR / "sources"))
PATCHES_DIR = Path(os.environ.get("PATCHES", BASE_DIR / "patches"))
DESTDIR_BASE = Path(os.environ.get("DESTDIR", BASE_DIR / "destdir"))
PACKAGES_DIR = Path(os.environ.get("PACKAGES", BASE_DIR / "packages"))
LOGS_DIR = Path(os.environ.get("LOGS", BASE_DIR / "logs"))
BIN_DIR = Path(os.environ.get("BIN_DIR", Path.home() / ".genpkg" / "bin"))
DB_PATH = Path(os.environ.get("DB", BASE_DIR / "installed.json"))

CHECK_ICON = "[✔]"
EMPTY_ICON = "[ ]"

ALLOWED_REMOVE_PREFIXES = ["/usr", "/etc", "/var", "/opt", "/bin", "/sbin", "/lib", "/lib64", "/usr/local"]

# ====== util ======
def ensure_dirs():
    for d in [REPO_DIR, RECIPES_DIR, SOURCES_DIR, PATCHES_DIR, DESTDIR_BASE, PACKAGES_DIR, LOGS_DIR, BIN_DIR]:
        d.mkdir(parents=True, exist_ok=True)


def which(cmd: str) -> Optional[str]:
    return shutil.which(cmd)


def run_cmd(cmd: List[str] | str, *, cwd: Optional[Path]=None, env=None, check=True):
    return subprocess.run(cmd, cwd=str(cwd) if cwd else None, env=env, shell=isinstance(cmd, str), check=check)


class Spinner:
    def __init__(self, message: str="Processando..."):
        self.message = message
        self._stop = threading.Event()
        self._thread: Optional[threading.Thread] = None
        self.frames = itertools.cycle(['⠋','⠙','⠹','⠸','⠼','⠴','⠦','⠧','⠇','⠏'])

    def start(self):
        def spin():
            while not self._stop.is_set():
                sys.stdout.write("\r" + c(self.message + " " + next(self.frames), Color.YELLOW))
                sys.stdout.flush()
                time.sleep(0.1)
        self._thread = threading.Thread(target=spin, daemon=True)
        self._thread.start()

    def stop(self):
        if self._thread:
            self._stop.set()
            self._thread.join()
        sys.stdout.write("\r" + " " * 80 + "\r")
        sys.stdout.flush()


# ====== Recipes ======
class Recipe:
    def __init__(self, data: dict, path: Path):
        # Aceita chaves em PT/EN
        self.name = data.get("nome") or data.get("name")
        self.version = str(data.get("versão") or data.get("version") or "0")
        self.url = data.get("url") or ""
        self.deps: List[str] = data.get("dependências") or data.get("deps") or []
        self.commands: List[str] = data.get("comandos") or data.get("commands") or []
        self.patches: List[str] = data.get("patches") or []
        # hooks
        self.pre_install: List[str] = data.get("pre_install") or []
        self.post_install: List[str] = data.get("post_install") or []
        self.pre_remove: List[str] = data.get("pre_remove") or []
        self.post_remove: List[str] = data.get("post_remove") or []
        self.path = path
        if not self.name:
            raise ValueError(f"Recipe {path} sem 'nome'.")


class RecipeIndex:
    @staticmethod
    def build() -> Dict[str, str]:
        index: Dict[str, str] = {}
        if not RECIPES_DIR.exists():
            return index
        for root, _, files in os.walk(RECIPES_DIR):
            for f in files:
                if f.endswith((".yml", ".yaml")):
                    name = f.rsplit(".", 1)[0]
                    index.setdefault(name, str(Path(root) / f))
        RECIPE_INDEX.parent.mkdir(parents=True, exist_ok=True)
        with open(RECIPE_INDEX, "w", encoding="utf-8") as fp:
            json.dump(index, fp, indent=2, ensure_ascii=False)
        return index

    @staticmethod
    def load() -> Dict[str, str]:
        if not RECIPE_INDEX.exists():
            return RecipeIndex.build()
        try:
            with open(RECIPE_INDEX, "r", encoding="utf-8") as fp:
                return json.load(fp)
        except Exception:
            return RecipeIndex.build()


class Recipes:
    def __init__(self):
        self.index = RecipeIndex.load()

    def reindex(self):
        self.index = RecipeIndex.build()

    def find(self, name: str) -> Recipe:
        path = self.index.get(name)
        if not path or not Path(path).exists():
            # busca recursiva como fallback
            for root, _, files in os.walk(RECIPES_DIR):
                if f"{name}.yml" in files:
                    path = str(Path(root) / f"{name}.yml")
                    break
                if f"{name}.yaml" in files:
                    path = str(Path(root) / f"{name}.yaml")
                    break
        if not path:
            raise FileNotFoundError(f"Receita '{name}' não encontrada em {RECIPES_DIR}.")
        import yaml  # garantimos erro claro se faltar
        with open(path, "r", encoding="utf-8") as f:
            data = yaml.safe_load(f) or {}
        return Recipe(data, Path(path))

    def search(self, term: str) -> List[str]:
        term = term.lower()
        names = set(self.index.keys())
        for root, _, files in os.walk(RECIPES_DIR):
            for f in files:
                if f.endswith((".yml", ".yaml")):
                    names.add(f.rsplit(".",1)[0])
        return sorted([n for n in names if term in n.lower()])


# ====== Database ======
class DB:
    def __init__(self, path: Path = DB_PATH):
        self.path = path
        self.data: Dict[str, dict] = self._load()

    def _load(self) -> Dict[str, dict]:
        if not self.path.exists():
            return {}
        try:
            with open(self.path, "r", encoding="utf-8") as fp:
                return json.load(fp)
        except Exception:
            return {}

    def save(self):
        with open(self.path, "w", encoding="utf-8") as fp:
            json.dump(self.data, fp, indent=2, ensure_ascii=False)


# ====== Download/Extract/Patch ======
def http_download(url: str, dest_dir: Path) -> Path:
    dest_dir.mkdir(parents=True, exist_ok=True)
    filename = url.split("/")[-1]
    out = dest_dir / filename
    if out.exists():
        return out
    spinner = Spinner(f"Baixando {filename}")
    spinner.start()
    try:
        try:
            import requests
            with requests.get(url, stream=True) as r:
                r.raise_for_status()
                with open(out, "wb") as f:
                    for chunk in r.iter_content(chunk_size=1024*128):
                        if chunk:
                            f.write(chunk)
        except Exception:
            # fallback wget/curl
            if which("wget"):
                run_cmd(["wget", "-O", str(out), url])
            elif which("curl"):
                run_cmd(["curl", "-L", "-o", str(out), url])
            else:
                raise RuntimeError("Instale 'requests' ou tenha 'wget'/'curl' no sistema para baixar arquivos.")
    finally:
        spinner.stop()
    return out


def git_download(git_url: str, dest_dir: Path) -> Path:
    if not which("git"):
        raise RuntimeError("'git' não encontrado no sistema.")
    dest_dir.mkdir(parents=True, exist_ok=True)
    folder = dest_dir / (git_url.rsplit("/",1)[-1].replace(".git",""))
    if folder.exists():
        run_cmd(["git", "-C", str(folder), "fetch", "--all", "-p"])
        run_cmd(["git", "-C", str(folder), "reset", "--hard", "origin/HEAD"], check=False)
    else:
        run_cmd(["git", "clone", git_url, str(folder)])
    return folder


def extract_tar_any(filepath: Path, outdir: Path) -> Path:
    outdir.mkdir(parents=True, exist_ok=True)
    print(c(f"📦 Extraindo {filepath.name}...", Color.CYAN))
    with tarfile.open(str(filepath), "r:*") as tar:
        top_members = [m for m in tar.getmembers() if m.name and "/" not in m.name.strip("/")]
        tar.extractall(path=str(outdir))
    # Melhor tentativa de detectar diretório raiz
    try:
        roots = set(p.parts[0] for p in (Path(m.name) for m in tar.getmembers()) if m.name.strip())  # type: ignore
    except Exception:
        roots = set()
    # fallback: escolher o diretório mais recente criado
    candidates = sorted([p for p in outdir.iterdir() if p.is_dir()], key=lambda p: p.stat().st_mtime, reverse=True)
    return candidates[0] if candidates else outdir


def apply_patches(patch_urls: List[str], build_dir: Path):
    if not patch_urls:
        return
    if not which("patch"):
        raise RuntimeError("'patch' não encontrado no sistema.")
    for url in patch_urls:
        patch_file = http_download(url, PATCHES_DIR)
        print(c(f"🩹 Aplicando patch {patch_file.name}...", Color.MAGENTA))
        run_cmd(f"patch -p1 < '{patch_file}'", cwd=build_dir, check=True)


# ====== Execução de comandos de build com log e spinner ======
def run_logged(cmd: str, *, cwd: Path, log_file: Path, env=None):
    log_file.parent.mkdir(parents=True, exist_ok=True)
    print(c(f"⚙️  {cmd}", Color.GREEN))
    spinner = Spinner("Executando…")
    spinner.start()
    try:
        with open(log_file, "a", encoding="utf-8") as lf:
            lf.write(f"\n$ {cmd}\n")
            proc = subprocess.Popen(cmd, cwd=str(cwd), env=env, shell=True, stdout=lf, stderr=lf)
            ret = proc.wait()
            if ret != 0:
                raise subprocess.CalledProcessError(ret, cmd)
    finally:
        spinner.stop()


# ====== Build/Package/Install ======
def strip_binaries_in(destdir: Path):
    if not which("strip"):
        return
    for sub in ["usr/bin", "usr/sbin", "bin", "sbin"]:
        d = destdir / sub
        if d.is_dir():
            for f in d.iterdir():
                try:
                    run_cmd(["strip", "--strip-unneeded", str(f)], check=False)
                except Exception:
                    pass


def collect_file_list(destdir: Path) -> List[str]:
    files: List[str] = []
    for root, _, names in os.walk(destdir):
        for n in names:
            p = Path(root) / n
            rel = str(p.relative_to(destdir))
            files.append(rel)
    files.sort()
    return files


def package_destdir(pkgname: str, version: str, destdir: Path) -> Path:
    pkgfile = PACKAGES_DIR / f"{pkgname}-{version}.tar.gz"
    print(c(f"📦 Empacotando {pkgfile.name}…", Color.MAGENTA))
    PACKAGES_DIR.mkdir(parents=True, exist_ok=True)
    with tarfile.open(pkgfile, "w:gz") as tar:
        tar.add(str(destdir), arcname=".")
    return pkgfile


def copy_binaries_to_bindir(destdir: Path) -> List[str]:
    copied: List[str] = []
    BIN_DIR.mkdir(parents=True, exist_ok=True)
    for sub in ["usr/bin", "bin", "usr/sbin", "sbin"]:
        d = destdir / sub
        if d.is_dir():
            for f in d.iterdir():
                if f.is_file() and os.access(str(f), os.X_OK):
                    target = BIN_DIR / f.name
                    shutil.copy2(str(f), str(target))
                    copied.append(str(target))
                    print(c(f"👉 Binário disponível: {target}", Color.BLUE))
    return copied


def safe_join(root: Path, rel: str) -> Path:
    p = (root / rel).resolve()
    if not str(p).startswith(str(root.resolve())):
        raise RuntimeError(f"Caminho inseguro: {rel}")
    return p


def install_package_files(pkgfile: Path, target_root: Path) -> List[str]:
    """Extrai o pacote para target_root e retorna a lista de arquivos realmente criados."""
    created: List[str] = []
    with tarfile.open(pkgfile, "r:gz") as tar:
        members = tar.getmembers()
        for m in members:
            # Rejeita caminhos absolutos
            if m.name.startswith("/"):
                raise RuntimeError(f"Entrada inválida no tar: {m.name}")
        tar.extractall(path=str(target_root))
        for m in members:
            path = target_root / m.name
            created.append(str(Path(m.name)))
    return sorted(list({p for p in created}))


def run_hooks(hooks: List[str], stage: str, workdir: Path, env=None, log_file: Optional[Path]=None):
    if not hooks:
        return
    for cmd in hooks:
        print(c(f"[HOOK {stage}] {cmd}", Color.YELLOW))
        if log_file is not None:
            with open(log_file, "a", encoding="utf-8") as lf:
                lf.write(f"\n[HOOK {stage}] $ {cmd}\n")
        run_cmd(cmd, cwd=workdir, env=env, check=True,)


class Installer:
    def __init__(self, db: DB, recipes: Recipes, *, target_root: Path, use_strip: bool):
        self.db = db
        self.recipes = recipes
        self.target_root = target_root
        self.use_strip = use_strip

    def _install_with_deps(self, name: str, visited: Optional[set]=None):
        if visited is None:
            visited = set()
        if name in visited:
            return
        visited.add(name)
        r = self.recipes.find(name)
        # deps primeiro
        for d in r.deps:
            if d not in self.db.data:
                self._install_with_deps(d, visited)
        # se já instalado, pula
        if name in self.db.data:
            print(c(f"✅ {name}-{r.version} já instalado.", Color.BLUE))
            return
        self._build_and_install(r)

    def _build_and_install(self, recipe: Recipe):
        name, version = recipe.name, recipe.version
        log_file = LOGS_DIR / f"{name}.log"
        LOGS_DIR.mkdir(parents=True, exist_ok=True)

        print(c(f"\n==> {name}-{version}", Color.CYAN))

        # PRE INSTALL hooks (antes de baixar/compilar)
        run_hooks(recipe.pre_install, "pre_install", workdir=BASE_DIR, log_file=log_file)

        # Download fonte
        if recipe.url.startswith("git+"):
            src_dir = git_download(recipe.url[len("git+"):], SOURCES_DIR)
        else:
            tarball = http_download(recipe.url, SOURCES_DIR)
            src_dir = extract_tar_any(tarball, SOURCES_DIR)

        # Aplicar patches
        apply_patches(recipe.patches, src_dir)

        # Preparar DESTDIR
        destdir = DESTDIR_BASE / recipe.name
        if destdir.exists():
            shutil.rmtree(destdir, ignore_errors=True)
        destdir.mkdir(parents=True, exist_ok=True)

        # Ambiente de build
        env = os.environ.copy()
        env.update({
            "DESTDIR": str(destdir),
            "GENPKG_NAME": recipe.name,
            "GENPKG_VERSION": recipe.version,
            "SOURCES": str(SOURCES_DIR),
            "PATCHES": str(PATCHES_DIR),
            "BIN_DIR": str(BIN_DIR),
        })

        # Executar comandos da recipe
        for cmd in recipe.commands:
            run_logged(cmd, cwd=src_dir, log_file=log_file, env=env)

        # Strip (opcional)
        if self.use_strip:
            print(c("🔪 Strip dos binários…", Color.MAGENTA))
            strip_binaries_in(destdir)

        # Empacotar antes de instalar
        pkgfile = package_destdir(name, version, destdir)

        # Copiar binários para BIN_DIR
        bin_copied = copy_binaries_to_bindir(destdir)

        # Instalar no root alvo
        print(c(f"📥 Instalando em {self.target_root}…", Color.CYAN))
        created_files = install_package_files(pkgfile, self.target_root)

        # POST INSTALL hooks
        run_hooks(recipe.post_install, "post_install", workdir=src_dir, log_file=log_file)

        # Registrar no DB
        self.db.data[name] = {
            "version": version,
            "files": created_files,  # caminhos relativos em relação ao root
            "package_file": str(pkgfile),
            "destdir": str(destdir),
            "bin_files": bin_copied,
            "installed_root": str(self.target_root),
            "installed_at": int(time.time()),
            "recipe_path": str(recipe.path),
        }
        self.db.save()
        print(c(f"🎉 {name}-{version} instalado.", Color.GREEN))

    # API pública
    def install(self, name: str):
        self._install_with_deps(name, visited=set())

    def build_only(self, name: str):
        r = self.recipes.find(name)
        log_file = LOGS_DIR / f"{name}.log"
        print(c(f"\n==> build-only {r.name}-{r.version}", Color.CYAN))
        # Download
        if r.url.startswith("git+"):
            src_dir = git_download(r.url[len("git+"):], SOURCES_DIR)
        else:
            tarball = http_download(r.url, SOURCES_DIR)
            src_dir = extract_tar_any(tarball, SOURCES_DIR)
        # patches
        apply_patches(r.patches, src_dir)
        # destdir
        destdir = DESTDIR_BASE / r.name
        if destdir.exists():
            shutil.rmtree(destdir, ignore_errors=True)
        destdir.mkdir(parents=True, exist_ok=True)
        env = os.environ.copy(); env.update({"DESTDIR": str(destdir)})
        for cmd in r.commands:
            run_logged(cmd, cwd=src_dir, log_file=log_file, env=env)
        if self.use_strip:
            strip_binaries_in(destdir)
        pkgfile = package_destdir(r.name, r.version, destdir)
        print(c(f"📦 Build concluído. Pacote: {pkgfile}", Color.GREEN))

    def remove(self, name: str):
        meta = self.db.data.get(name)
        if not meta:
            print(c(f"⚠️ {name} não está instalado.", Color.RED)); return
        # Carregar recipe para hooks de remoção
        try:
            r = self.recipes.find(name)
        except Exception:
            r = None
        target_root = Path(meta.get("installed_root", "/"))
        files: List[str] = meta.get("files", [])

        # PRE REMOVE hooks
        if r:
            run_hooks(r.pre_remove, "pre_remove", workdir=BASE_DIR)

        # Remover arquivos
        removed = 0
        for rel in sorted(files, key=lambda p: len(p.split("/")), reverse=True):
            # segurança: só remover dentro de prefixes conhecidos (quando root real)
            abs_path = (target_root / rel).resolve()
            try:
                if target_root.resolve() == Path("/"):
                    if not any(str(abs_path).startswith(pref) for pref in ALLOWED_REMOVE_PREFIXES):
                        continue
                if abs_path.is_symlink() or abs_path.is_file():
                    abs_path.unlink(missing_ok=True)
                    removed += 1
                elif abs_path.is_dir():
                    try:
                        abs_path.rmdir()
                    except OSError:
                        pass
            except Exception:
                pass
        # Limpar BIN_DIR copiados
        for b in meta.get("bin_files", []):
            try:
                Path(b).unlink(missing_ok=True)
            except Exception:
                pass

        # POST REMOVE hooks
        if r:
            run_hooks(r.post_remove, "post_remove", workdir=BASE_DIR)

        # Remover destdir se existir
        try:
            shutil.rmtree(meta.get("destdir", ""), ignore_errors=True)
        except Exception:
            pass

        del self.db.data[name]
        self.db.save()
        print(c(f"🗑️ Remoção concluída de {name} ({removed} arquivos).", Color.GREEN))

    def upgrade(self, name: Optional[str], all_pkgs: bool):
        if all_pkgs:
            if not self.db.data:
                print(c("Nada para upgrade.", Color.YELLOW)); return
            for pkg in list(self.db.data.keys()):
                print(c(f"🔄 Upgrade {pkg}…", Color.CYAN))
                self.remove(pkg)
                self.install(pkg)
        else:
            if not name:
                print(c("Especifique um pacote ou use --all.", Color.RED)); return
            if name in self.db.data:
                self.remove(name)
            self.install(name)

    def info(self, name: str):
        try:
            r = self.recipes.find(name)
        except Exception as e:
            print(c(f"Erro: {e}", Color.RED)); return
        status = CHECK_ICON if name in self.db.data else EMPTY_ICON
        print(c(f"📦 {r.name} {r.version}", Color.CYAN))
        print(c(f"   Status: {status}", Color.YELLOW))
        print(c(f"   URL: {r.url}", Color.YELLOW))
        if r.deps:
            print(c(f"   Dependências: {', '.join(r.deps)}", Color.YELLOW))
        if r.patches:
            print(c(f"   Patches: {', '.join(r.patches)}", Color.YELLOW))
        if name in self.db.data:
            m = self.db.data[name]
            print(c(f"   Instalado em: {m.get('installed_root','/')}", Color.YELLOW))
            print(c(f"   Pacote: {m.get('package_file','')} ", Color.YELLOW))

    def list_installed(self):
        if not self.db.data:
            print(c("📂 Nenhum pacote instalado.", Color.YELLOW)); return
        print(c("📦 Pacotes instalados:", Color.CYAN))
        for n, meta in sorted(self.db.data.items()):
            print(f" - {n}-{meta.get('version','?')}")

    def search(self, term: str):
        results = self.recipes.search(term)
        for r in results:
            status = CHECK_ICON if r in self.db.data else EMPTY_ICON
            print(f"{status} {r}")


# ====== Repo sync, reindex e clean ======
def sync_repo(url: str):
    ensure_dirs()
    if not (REPO_DIR / ".git").exists():
        print(c(f"📥 Clonando {url} em {REPO_DIR}…", Color.CYAN))
        run_cmd(["git", "clone", url, str(REPO_DIR)])
    else:
        print(c(f"🔄 Atualizando {REPO_DIR}…", Color.CYAN))
        run_cmd(["git", "-C", str(REPO_DIR), "pull"])
    RecipeIndex.build()
    print(c("✅ Índice de recipes atualizado.", Color.GREEN))


def reindex_repo():
    RecipeIndex.build()
    print(c("✅ Índice reconstruído.", Color.GREEN))


def clean_workspace(*, sources=False, patches=False, destdir=False, packages=False, logs=False, all_=False):
    targets: List[Path] = []
    if all_:
        targets = [SOURCES_DIR, PATCHES_DIR, DESTDIR_BASE, PACKAGES_DIR, LOGS_DIR]
    else:
        if sources: targets.append(SOURCES_DIR)
        if patches: targets.append(PATCHES_DIR)
        if destdir: targets.append(DESTDIR_BASE)
        if packages: targets.append(PACKAGES_DIR)
        if logs: targets.append(LOGS_DIR)
    for d in targets:
        if d.exists():
            print(c(f"🗑️ Limpando {d}…", Color.RED))
            shutil.rmtree(d, ignore_errors=True)
        d.mkdir(parents=True, exist_ok=True)
    print(c("🧹 Limpeza concluída.", Color.GREEN))


# ====== CLI ======
def build_parser():
    p = argparse.ArgumentParser(prog=APP_NAME, description=f"{APP_NAME} v{APP_VERSION}")
    p.add_argument("--root", default="/", help="Root de instalação (default: /). Use uma pasta para testar sem root.")
    sp = p.add_subparsers(dest="cmd")

    # install
    c_i = sp.add_parser("install", aliases=["i"], help="Instala pacote com dependências")
    c_i.add_argument("package")
    c_i.add_argument("--strip", action="store_true", help="Strip de binários após build")

    # build-only
    c_b = sp.add_parser("build", aliases=["b"], help="Compila e empacota sem instalar")
    c_b.add_argument("package")
    c_b.add_argument("--strip", action="store_true")

    # remove
    c_r = sp.add_parser("remove", aliases=["r"], help="Remove pacote instalado")
    c_r.add_argument("package")

    # list
    sp.add_parser("list", aliases=["l"], help="Lista pacotes instalados")

    # search
    c_s = sp.add_parser("search", aliases=["s"], help="Procura recipes por termo")
    c_s.add_argument("term")

    # info
    c_info = sp.add_parser("info", help="Mostra informações da recipe/pacote")
    c_info.add_argument("package")

    # sync
    c_sync = sp.add_parser("sync", help="Sincroniza receitas via git")
    c_sync.add_argument("url")

    # reindex
    sp.add_parser("reindex", help="Reconstrói o índice de recipes")

    # clean
    c_clean = sp.add_parser("clean", help="Remove diretórios de trabalho")
    c_clean.add_argument("--sources", action="store_true")
    c_clean.add_argument("--patches", action="store_true")
    c_clean.add_argument("--destdir", action="store_true")
    c_clean.add_argument("--packages", action="store_true")
    c_clean.add_argument("--logs", action="store_true")
    c_clean.add_argument("--all", action="store_true")

    # upgrade
    c_up = sp.add_parser("upgrade", help="Recompila pacote ou todo o sistema")
    c_up.add_argument("package", nargs="?", default=None)
    c_up.add_argument("--all", action="store_true")
    c_up.add_argument("--strip", action="store_true")

    return p


def main():
    ensure_dirs()
    parser = build_parser()
    args = parser.parse_args()

    if not args.cmd:
        parser.print_help(); return

    # comandos simples
    if args.cmd == "sync":
        sync_repo(args.url); return
    if args.cmd == "reindex":
        reindex_repo(); return
    if args.cmd == "clean":
        clean_workspace(sources=args.sources, patches=args.patches, destdir=args.destdir, packages=args.packages, logs=args.logs, all_=args.all); return

    recipes = Recipes()
    db = DB()
    target_root = Path(args.root).resolve()

    # instalar/compilar
    if args.cmd in ("install", "i"):
        inst = Installer(db, recipes, target_root=target_root, use_strip=args.strip)
        inst.install(args.package)
        return
    if args.cmd in ("build", "b"):
        inst = Installer(db, recipes, target_root=target_root, use_strip=args.strip)
        inst.build_only(args.package); return
    if args.cmd in ("remove", "r"):
        inst = Installer(db, recipes, target_root=target_root, use_strip=False)
        inst.remove(args.package); return
    if args.cmd in ("list", "l"):
        Installer(db, recipes, target_root=target_root, use_strip=False).list_installed(); return
    if args.cmd in ("search", "s"):
        Installer(db, recipes, target_root=target_root, use_strip=False).search(args.term); return
    if args.cmd == "info":
        Installer(db, recipes, target_root=target_root, use_strip=False).info(args.package); return
    if args.cmd == "upgrade":
        inst = Installer(db, recipes, target_root=target_root, use_strip=args.strip)
        inst.upgrade(args.package, args.all); return

    parser.print_help()


if __name__ == "__main__":
    try:
        main()
    except FileNotFoundError as e:
        print(c(f"Erro: {e}", Color.RED))
        sys.exit(1)
    except subprocess.CalledProcessError as e:
        print(c(f"Comando falhou ({e.returncode}): {e.cmd}", Color.RED))
        print(c("Veja o log em logs/<pacote>.log", Color.YELLOW))
        sys.exit(e.returncode)
    except KeyboardInterrupt:
        print("\nInterrompido.")
        sys.exit(130)
