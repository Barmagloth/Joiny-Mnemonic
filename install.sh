#!/usr/bin/env bash
set -euo pipefail

project_root="$PWD"
install_root="${HOME}/.joiny-mnemonic/runtime"
source_root=""
repository="https://github.com/Barmagloth/Joiny-Mnemonic.git"
python="python3"
scope="project"
agents=()
plugins=()
extra=()

while (($#)); do
  case "$1" in
    --project-root) project_root="$2"; shift 2 ;;
    --install-root) install_root="$2"; shift 2 ;;
    --source-root) source_root="$2"; shift 2 ;;
    --repository) repository="$2"; shift 2 ;;
    --python) python="$2"; shift 2 ;;
    --scope) scope="$2"; shift 2 ;;
    --agent) agents+=("$2"); shift 2 ;;
    --plugin) plugins+=("$2"); shift 2 ;;
    --all-plugins|--with-mcp|--without-hooks|--yes|--dry-run)
      extra+=("$1"); shift ;;
    -h|--help)
      echo "Usage: install.sh [--project-root PATH] [--scope project|global]"
      echo "                  [--agent PRODUCT] [--plugin COMPONENT] [--with-mcp] [--yes]"
      exit 0 ;;
    *) echo "Unknown argument: $1" >&2; exit 2 ;;
  esac
done

script_root="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
if [[ -z "$source_root" && -f "$script_root/pyproject.toml" ]]; then
  source_root="$script_root"
fi

mkdir -p "$install_root"
if [[ -z "$source_root" ]]; then
  source_root="${install_root}/source"
  if [[ -d "${source_root}/.git" ]]; then
    git -C "$source_root" pull --ff-only
  elif [[ -e "$source_root" ]]; then
    echo "Source path exists but is not a Git checkout: $source_root" >&2
    exit 1
  else
    git clone --depth 1 "$repository" "$source_root"
  fi
fi
if [[ ! -f "${source_root}/pyproject.toml" ]]; then
  echo "Joiny-Mnemonic source is missing pyproject.toml: $source_root" >&2
  exit 1
fi

venv="${install_root}/venv"
venv_python="${venv}/bin/python"
if [[ ! -x "$venv_python" ]]; then
  "$python" -m venv "$venv"
fi
"$venv_python" -m pip install "$source_root"

setup_args=(
  -m joiny_mnemonic
  --project-root "$project_root"
  setup
  --scope "$scope"
  --source-root "$source_root"
)
for value in "${agents[@]}"; do setup_args+=(--agent "$value"); done
for value in "${plugins[@]}"; do setup_args+=(--plugin "$value"); done
setup_args+=("${extra[@]}")

"$venv_python" "${setup_args[@]}"
echo "Joiny-Mnemonic runtime: $venv_python"
