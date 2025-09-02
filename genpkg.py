#!/usr/bin/env python3
# genpkg.py – mini gerenciador de pacotes fonte para LFS-like
# Recursos: recipes (YAML) em repo git, deps recursivas, patches, DESTDIR+fakeroot,
# empacotamento .tar.gz, logs, cores, strip opcional, upgrade, spinner, bin dir, clean.

import os
import sys
import json
import yaml
import tarfile
import shutil
import bz2
import lzma
import time
import itertools
import threading
import argparse
import subprocess
from pathlib import Path
from typing import List, Dict, Optional
from colorama import Fore, Style, init as colorama_init

# =============================
# CONFIGURAÇÃO (variáveis globais)
# =============================
GENPKG_NAME    = "genpkg"
GENPKG_VERSION = "0.9"

REPO_DIR       = "repo"                                   # git repo raiz
RECIPES_DIR    = os.path.join(REPO_DIR, "recipes")        # recipes ficam aqui (com subpastas)
RECIPE_INDEX   = os.path.join(REPO_DIR, "index.json")     # cache/índice de recipes

DB_PATH        = "installed.json"                         # banco de pacotes instalados
SOURCES_DIR    = "sources"                                # downloads de tarballs
PATCHES_DIR    = "patches"                                # downloads de patches
DESTDIR_BASE   = "destdir"                                # instalação fake por pacote
PACKAGES_DIR   = "packages"                               # pacotes .tar.gz gerados
LOGS_DIR       = "logs"                                   # logs por pacote
BIN_DIR        = os.path.expanduser("~/.genpkg/bin")      # onde copiar binários (usr/bin)

CHROOT_ENV     = {}                                       # reservado p/ futuro (ex: chroot)
CHECK_ICON     = "[✔]"
UNCHECK_ICON   = "[ ]"

colorama_init(autoreset=True)

# =============================
# Helpers
# =============================

def which(cmd: str) -> Optional[str]:
    return shutil.which(cmd)

def ensure_dirs():
    for d in [REPO_DIR, RECIPES_DIR, SOURCES_DIR, PATCHES_DIR,
              DESTDIR_BASE, PACKAGES_DIR, LOGS_DIR, BIN_DIR]:
        os.makedirs(d, exist_ok=True)

class Spinner:
    def __init__(self, message="Processando..."):
        self.message = message
        self._stop = False
        self._thread = None
        self._frames = itertools.cycle(['⠋','⠙','⠹','⠸','⠼','⠴','⠦','⠧','⠇','⠏'])

    def start(self):
        def run():
            while not self._stop:
                sys.stdout.write(Fore.YELLOW + f"\r{self.message} " + next(self._frames))
                sys.stdout.flush()
                time.sleep(0.1)
        self._thread = threading.Thread(target=run, daemon=True)
        self._thread.start()

    def stop(self):
        self._stop = True
        if self._thread:
            self._thread.join()
        sys.stdout.write("\r" + " " * 80 + "\r")
        sys.stdout.flush()

class pushd:
    """Context manager para trocar diretório e voltar ao final."""
    def __init__(self, path):
        self.path = path
        self.prev = None
    def __enter__(self):
        self.prev = os.getcwd()
        os.chdir(self.path)
    def __exit__(self, exc_type, exc, tb):
        os.chdir(self.prev)

def colored(msg, color=Fore.CYAN):
    return color + msg + Style.RESET_ALL

def run_cmd(cmd, *, shell=False, check=True, env=None, stdout=None, stderr=None):
    return subprocess.run(cmd, shell=shell, check=check, env=env, stdout=stdout, stderr=stderr)

def run_shell_as_user_or_fakeroot(cmd: str, env=None, stdout=None, stderr=None):
    """Usa fakeroot se existir, senão roda normalmente."""
    if which("fakeroot"):
        return run_cmd(f"fakeroot sh -c '{cmd}'", shell=True, env=env, stdout=stdout, stderr=stderr)
    else:
        return run_cmd(cmd, shell=True, env=env, stdout=stdout, stderr=stderr)

def strip_file(path: str):
    if which("strip"):
        try:
            run_cmd(["strip", "--strip-unneeded", path], check=False,
                    stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        except Exception:
            pass

def safe_relpath(path: str, start: str) -> str:
    try:
        return os.path.relpath(path, start)
    except ValueError:
        return path

# =============================
# Modelos
# =============================

class Package:
    def __init__(self, name, version, url, dependencies, commands, patches=None):
        self.name = name
        self.version = version
        self.url = url
        self.dependencies = dependencies or []
        self.commands = commands or []
        self.patches = patches or []
    def __repr__(self):
        return f"{self.name}-{self.version}"

# =============================
# Recipe Manager + Index
# =============================

class RecipeIndex:
    @staticmethod
    def build_index() -> Dict[str, str]:
        idx = {}
        if not os.path.isdir(RECIPES_DIR):
            return idx
        for root, _, files in os.walk(RECIPES_DIR):
            for f in files:
                if f.endswith(".yml") or f.endswith(".yaml"):
                    name = f.rsplit(".", 1)[0]
                    # Se houver duplicatas, a primeira encontrada prevalece
                    idx.setdefault(name, os.path.join(root, f))
        with open(RECIPE_INDEX, "w") as fp:
            json.dump(idx, fp, indent=2)
        return idx

    @staticmethod
    def load_index() -> Dict[str, str]:
        if not os.path.exists(RECIPE_INDEX):
            return RecipeIndex.build_index()
        try:
            with open(RECIPE_INDEX, "r") as fp:
                return json.load(fp)
        except Exception:
            return RecipeIndex.build_index()

class RecipeManager:
    def __init__(self):
        self.index = RecipeIndex.load_index()

    def reindex(self):
        self.index = RecipeIndex.build_index()

    def _find_recipe_path(self, package_name: str) -> Optional[str]:
        path = self.index.get(package_name)
        if path and os.path.exists(path):
            return path
        # fallback: search recursively (em caso de cache desatualizado)
        for root, _, files in os.walk(RECIPES_DIR):
            for f in files:
                if f == f"{package_name}.yml":
                    return os.path.join(root, f)
        return None

    def load(self, package_name: str) -> Package:
        recipe_path = self._find_recipe_path(package_name)
        if not recipe_path:
            raise FileNotFoundError(colored(f"⚠️ Receita {package_name} não encontrada em {RECIPES_DIR}.", Fore.RED))
        with open(recipe_path, "r") as f:
            data = yaml.safe_load(f) or {}
        return Package(
            data.get("nome", package_name),
            data.get("versão", "0"),
            data.get("url", ""),
            data.get("dependências", []) or data.get("deps", []),
            data.get("comandos", []) or data.get("commands", []),
            data.get("patches", []),
        )

    def search(self, term: str) -> List[str]:
        term = term.lower()
        names = set(self.index.keys())
        # fallback: varrer se vazio
        if not names and os.path.isdir(RECIPES_DIR):
            for root, _, files in os.walk(RECIPES_DIR):
                for f in files:
                    if f.endswith(".yml") or f.endswith(".yaml"):
                        names.add(f.rsplit(".", 1)[0])
        return sorted([n for n in names if term in n.lower()])

# =============================
# Banco (installed.json)
# =============================

class DB:
    def __init__(self, path=DB_PATH):
        self.path = path
        self.data = self._load()
    def _load(self):
        if not os.path.exists(self.path):
            return {}
        with open(self.path, "r") as fp:
            try:
                return json.load(fp)
            except Exception:
                return {}
    def save(self):
        with open(self.path, "w") as fp:
            json.dump(self.data, fp, indent=2)

# =============================
# Downloader / Extractor
# =============================

def http_download(url: str, dest_dir: str) -> str:
    import requests
    os.makedirs(dest_dir, exist_ok=True)
    filename = url.split("/")[-1]
    filepath = os.path.join(dest_dir, filename)
    if os.path.exists(filepath):
        return filepath
    spinner = Spinner(f"📥 Baixando {filename}")
    spinner.start()
    try:
        with requests.get(url, stream=True) as r:
            r.raise_for_status()
            with open(filepath, "wb") as f:
                for chunk in r.iter_content(chunk_size=1024 * 64):
                    if chunk:
                        f.write(chunk)
    finally:
        spinner.stop()
    return filepath

def extract_tar_any(filepath: str, outdir: str) -> str:
    print(colored(f"📦 Extraindo {os.path.basename(filepath)}...", Fore.CYAN))
    os.makedirs(outdir, exist_ok=True)
    # Detect by suffix
    if filepath.endswith((".tar.gz", ".tgz")):
        mode = "r:gz"
        with tarfile.open(filepath, mode) as tar:
            tar.extractall(path=outdir)
    elif filepath.endswith(".tar.xz"):
        with tarfile.open(fileobj=lzma.open(filepath), mode="r:") as tar:
            tar.extractall(path=outdir)
    elif filepath.endswith(".tar.bz2"):
        with tarfile.open(fileobj=bz2.BZ2File(filepath), mode="r:") as tar:
            tar.extractall(path=outdir)
    else:
        # tentar abrir normalmente (tar sem compressão)
        with tarfile.open(filepath, "r:") as tar:
            tar.extractall(path=outdir)
    # tentativa: folder = nome do tar sem sufixos
    base = os.path.basename(filepath)
    dirname = (base
               .replace(".tar.gz", "")
               .replace(".tgz", "")
               .replace(".tar.xz", "")
               .replace(".tar.bz2", "")
               .replace(".tar", ""))
    candidate = os.path.join(outdir, dirname)
    return candidate if os.path.isdir(candidate) else outdir

# =============================
# Installer
# =============================

class Installer:
    def __init__(self, db: DB, recipes: RecipeManager):
        self.db = db
        self.recipes = recipes
        ensure_dirs()

    def _apply_patches(self, pkg: Package, build_dir: str):
        if not pkg.patches:
            return
        with pushd(build_dir):
            for url in pkg.patches:
                patch_file = http_download(url, PATCHES_DIR)
                print(colored(f"🩹 Aplicando patch {os.path.basename(patch_file)}...", Fore.YELLOW))
                run_cmd(f"patch -p1 < '{patch_file}'", shell=True)

    def _run_build_commands(self, pkg: Package, build_dir: str, strip_binaries: bool) -> str:
        destdir = os.path.abspath(os.path.join(DESTDIR_BASE, pkg.name))
        os.makedirs(destdir, exist_ok=True)
        env = os.environ.copy()
        env["DESTDIR"] = destdir
        env.update(CHROOT_ENV)

        log_path = os.path.join(LOGS_DIR, f"{pkg.name}.log")
        print(colored(f"📝 Log: {log_path}", Fore.BLUE))

        with open(log_path, "w") as log, pushd(build_dir):
            for cmd in pkg.commands:
                print(colored(f"⚙️  {cmd}", Fore.GREEN))
                run_shell_as_user_or_fakeroot(cmd, env=env, stdout=log, stderr=log)

            if strip_binaries:
                print(colored("🔪 Strip dos binários...", Fore.MAGENTA))
                usr_bin = os.path.join(destdir, "usr", "bin")
                usr_sbin = os.path.join(destdir, "usr", "sbin")
                for root_dir in [usr_bin, usr_sbin]:
                    if os.path.isdir(root_dir):
                        for name in os.listdir(root_dir):
                            strip_file(os.path.join(root_dir, name))
        return destdir

    def _package_destdir_to_tar_gz(self, pkg: Package, destdir: str) -> str:
        pkgfile = os.path.join(PACKAGES_DIR, f"{pkg.name}-{pkg.version}.tar.gz")
        print(colored(f"📦 Empacotando {pkgfile}...", Fore.MAGENTA))
        with tarfile.open(pkgfile, "w:gz") as tar:
            tar.add(destdir, arcname=".")
        return pkgfile

    def _copy_binaries_to_bindir(self, destdir: str) -> List[str]:
        copied = []
        for sub in ["usr/bin", "bin", "sbin", "usr/sbin"]:
            src = os.path.join(destdir, sub)
            if os.path.isdir(src):
                os.makedirs(BIN_DIR, exist_ok=True)
                for f in os.listdir(src):
                    s = os.path.join(src, f)
                    if os.path.isfile(s) and os.access(s, os.X_OK):
                        d = os.path.join(BIN_DIR, f)
                        shutil.copy2(s, d)
                        copied.append(d)
                        print(colored(f"👉 Binário disponível: {d}", Fore.GREEN))
        return copied

    def _collect_file_list(self, destdir: str) -> List[str]:
        files = []
        for root, _, names in os.walk(destdir):
            for n in names:
                abspath = os.path.join(root, n)
                rel = safe_relpath(abspath, destdir)
                files.append(rel)
        return sorted(files)

    def _install_core(self, pkg: Package, strip_binaries: bool) -> None:
        # download + extract
        if not pkg.url:
            raise RuntimeError(f"URL ausente na recipe de {pkg.name}.")
        tarball = http_download(pkg.url, SOURCES_DIR)
        build_dir = extract_tar_any(tarball, SOURCES_DIR)

        # patches + build
        self._apply_patches(pkg, build_dir)
        spinner = Spinner(f"🔨 Compilando/instalando {pkg}")
        spinner.start()
        try:
            destdir = self._run_build_commands(pkg, build_dir, strip_binaries)
        finally:
            spinner.stop()

        pkgfile = self._package_destdir_to_tar_gz(pkg, destdir)
        bin_copied = self._copy_binaries_to_bindir(destdir)
        files = self._collect_file_list(destdir)

        # registrar
        self.db.data[pkg.name] = {
            "version": pkg.version,
            "files": files,
            "package_file": pkgfile,
            "destdir": os.path.relpath(destdir, "."),
            "bin_files": bin_copied
        }
        self.db.save()
        print(colored(f"🎉 {pkg} instalado em {destdir}", Fore.GREEN))

    def _install_with_deps(self, pkg: Package, visited: Optional[set], strip_binaries: bool):
        if visited is None:
            visited = set()
        if pkg.name in visited:
            return
        visited.add(pkg.name)

        # deps primeiro
        for dep_name in pkg.dependencies:
            if dep_name not in self.db.data:
                dep_pkg = self.recipes.load(dep_name)
                self._install_with_deps(dep_pkg, visited, strip_binaries)

        # instalar se não instalado
        if pkg.name in self.db.data:
            print(colored(f"✅ {pkg} já instalado.", Fore.BLUE))
            return
        self._install_core(pkg, strip_binaries)

    # API pública

    def install(self, name: str, strip_binaries: bool):
        pkg = self.recipes.load(name)
        self._install_with_deps(pkg, visited=set(), strip_binaries=strip_binaries)

    def build_only(self, name: str, strip_binaries: bool):
        """Compila e empacota no DESTDIR, mas não registra como instalado."""
        pkg = self.recipes.load(name)
        # somente core, sem deps e sem registrar na DB
        tarball = http_download(pkg.url, SOURCES_DIR)
        build_dir = extract_tar_any(tarball, SOURCES_DIR)
        self._apply_patches(pkg, build_dir)
        spinner = Spinner(f"🔨 Compilando {pkg} (build-only)")
        spinner.start()
        try:
            destdir = self._run_build_commands(pkg, build_dir, strip_binaries)
        finally:
            spinner.stop()
        pkgfile = self._package_destdir_to_tar_gz(pkg, destdir)
        print(colored(f"📦 Build completo para {pkg}. Pacote: {pkgfile}", Fore.CYAN))

    def remove(self, name: str):
        meta = self.db.data.get(name)
        if not meta:
            print(colored(f"⚠️ {name} não está instalado.", Fore.RED))
            return
        # remover destdir
        destdir = meta.get("destdir") or os.path.join(DESTDIR_BASE, name)
        if os.path.exists(destdir):
            print(colored(f"🗑️ Removendo {destdir}...", Fore.RED))
            shutil.rmtree(destdir, ignore_errors=True)
        # remover binários copiados
        for b in meta.get("bin_files", []):
            try:
                if os.path.exists(b):
                    os.remove(b)
            except Exception:
                pass
        # manter pacote .tar.gz e logs por padrão
        del self.db.data[name]
        self.db.save()
        print(colored(f"✔️ {name} removido com sucesso.", Fore.GREEN))

    def list_installed(self):
        if not self.db.data:
            print(colored("📂 Nenhum pacote instalado.", Fore.YELLOW))
            return
        print(colored("📦 Pacotes instalados:", Fore.CYAN))
        for n, meta in sorted(self.db.data.items()):
            print(f" - {n}-{meta.get('version','?')}")

    def info(self, name: str):
        try:
            pkg = self.recipes.load(name)
        except Exception as e:
            print(colored(f"Erro: {e}", Fore.RED))
            return
        status = CHECK_ICON if name in self.db.data else UNCHECK_ICON
        print(colored(f"📦 {pkg.name} {pkg.version}", Fore.CYAN))
        print(colored(f"   Status: {status}", Fore.YELLOW))
        print(colored(f"   URL: {pkg.url}", Fore.YELLOW))
        if pkg.dependencies:
            print(colored(f"   Dependências: {', '.join(pkg.dependencies)}", Fore.YELLOW))
        if pkg.patches:
            print(colored(f"   Patches: {', '.join(pkg.patches)}", Fore.YELLOW))
        if name in self.db.data:
            meta = self.db.data[name]
            print(colored(f"   DESTDIR: {meta.get('destdir','')}", Fore.YELLOW))
            print(colored(f"   Pacote:  {meta.get('package_file','')}", Fore.YELLOW))

    def search(self, term: str):
        results = self.recipes.search(term)
        if not results:
            print(colored("❌ Nenhum pacote encontrado.", Fore.YELLOW))
            return
        for r in results:
            status = CHECK_ICON if r in self.db.data else UNCHECK_ICON
            print(f"{status} {r}")

    def upgrade(self, name: Optional[str], strip_binaries: bool, all_pkgs: bool):
        if all_pkgs:
            if not self.db.data:
                print(colored("Nada instalado para recompilar.", Fore.YELLOW))
                return
            # compilar novamente todos os instalados (na ordem simples)
            pkgs = list(self.db.data.keys())
            for p in pkgs:
                print(colored(f"🔄 Recompilando {p}...", Fore.CYAN))
                # remover antes? Vamos recompilar por cima do destdir (limpo)
                self.remove(p)
                self.install(p, strip_binaries)
        else:
            if not name:
                print(colored("Especifique um pacote ou use --all.", Fore.RED))
                return
            if name in self.db.data:
                print(colored(f"🔄 Recompilando {name}...", Fore.CYAN))
                self.remove(name)
            self.install(name, strip_binaries)

# =============================
# Git sync & clean & reindex
# =============================

def sync_repo(url: str):
    ensure_dirs()
    if not os.path.exists(os.path.join(REPO_DIR, ".git")):
        print(colored(f"📥 Clonando {url} em {REPO_DIR}...", Fore.CYAN))
        run_cmd(["git", "clone", url, REPO_DIR])
    else:
        print(colored(f"🔄 Atualizando repositório em {REPO_DIR}...", Fore.CYAN))
        run_cmd(["git", "-C", REPO_DIR, "pull"])
    # rebuild index
    RecipeIndex.build_index()
    print(colored("✅ Index de recipes atualizado.", Fore.GREEN))

def reindex_repo():
    ensure_dirs()
    RecipeIndex.build_index()
    print(colored("✅ Index reconstruído.", Fore.GREEN))

def clean_workspace(sources=False, patches=False, destdir=False, packages=False, logs=False, all_=False):
    targets = []
    if all_:
        targets = [SOURCES_DIR, PATCHES_DIR, DESTDIR_BASE, PACKAGES_DIR, LOGS_DIR]
    else:
        if sources: targets.append(SOURCES_DIR)
        if patches: targets.append(PATCHES_DIR)
        if destdir: targets.append(DESTDIR_BASE)
        if packages: targets.append(PACKAGES_DIR)
        if logs: targets.append(LOGS_DIR)
    for d in targets:
        if os.path.exists(d):
            print(colored(f"🗑️ Limpando {d}...", Fore.RED))
            shutil.rmtree(d, ignore_errors=True)
        os.makedirs(d, exist_ok=True)
    print(colored("🧹 Limpeza concluída.", Fore.GREEN))

# =============================
# CLI
# =============================

def build_parser():
    p = argparse.ArgumentParser(prog=GENPKG_NAME, description=f"{GENPKG_NAME} v{GENPKG_VERSION}")
    sp = p.add_subparsers(dest="cmd")

    # install
    c_install = sp.add_parser("install", aliases=["i"], help="Instala pacote com deps")
    c_install.add_argument("package")
    c_install.add_argument("--strip", action="store_true", help="Strip dos binários após build")

    # build-only
    c_build = sp.add_parser("build", aliases=["b"], help="Compila e empacota sem registrar")
    c_build.add_argument("package")
    c_build.add_argument("--strip", action="store_true")

    # remove
    c_remove = sp.add_parser("remove", aliases=["r"], help="Remove pacote instalado")
    c_remove.add_argument("package")

    # list
    sp.add_parser("list", aliases=["l"], help="Lista pacotes instalados")

    # search
    c_search = sp.add_parser("search", aliases=["s"], help="Procura recipes")
    c_search.add_argument("term")

    # info
    c_info = sp.add_parser("info", help="Info do pacote")
    c_info.add_argument("package")

    # sync git
    c_sync = sp.add_parser("sync", help="Sincroniza recipes de repo git")
    c_sync.add_argument("url")

    # reindex
    sp.add_parser("reindex", help="Reconstrói índice de recipes")

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
    c_up.add_argument("package", nargs="?", default=None, help="Nome do pacote (ou omita com --all)")
    c_up.add_argument("--all", action="store_true", help="Recompila todos os instalados")
    c_up.add_argument("--strip", action="store_true")

    return p

def main():
    ensure_dirs()
    parser = build_parser()
    args = parser.parse_args()

    if not args.cmd:
        parser.print_help()
        sys.exit(0)

    # comandos que não precisam de DB/recipes
    if args.cmd == "sync":
        sync_repo(args.url)
        return
    if args.cmd == "reindex":
        reindex_repo()
        return
    if args.cmd == "clean":
        clean_workspace(args.sources, args.patches, args.destdir, args.packages, args.logs, args.all)
        return

    recipes = RecipeManager()
    db = DB()
    inst = Installer(db, recipes)

    if args.cmd in ("install", "i"):
        inst.install(args.package, strip_binaries=args.strip)
    elif args.cmd in ("build", "b"):
        inst.build_only(args.package, strip_binaries=args.strip)
    elif args.cmd in ("remove", "r"):
        inst.remove(args.package)
    elif args.cmd in ("list", "l"):
        inst.list_installed()
    elif args.cmd in ("search", "s"):
        inst.search(args.term)
    elif args.cmd == "info":
        inst.info(args.package)
    elif args.cmd == "upgrade":
        inst.upgrade(args.package, strip_binaries=args.strip, all_pkgs=args.all)
    else:
        parser.print_help()

if __name__ == "__main__":
    main()
