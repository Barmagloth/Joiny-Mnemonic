#!/usr/bin/env bash
set -euo pipefail

project_root="$PWD"
install_root="${HOME}/.joiny-mnemonic/runtime"
source_root=""
repository="https://github.com/Barmagloth/Joiny-Mnemonic.git"
revision=""
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
    --revision) revision="$2"; shift 2 ;;
    --python) python="$2"; shift 2 ;;
    --scope) scope="$2"; shift 2 ;;
    --agent) agents+=("$2"); shift 2 ;;
    --plugin) plugins+=("$2"); shift 2 ;;
    --all-plugins|--with-mcp|--without-hooks|--skip-plugin-install|--enable-extraction|--yes|--dry-run)
      extra+=("$1"); shift ;;
    -h|--help)
      echo "Usage: install.sh [--project-root PATH] [--scope project|global]"
      echo "                  [--agent PRODUCT] [--plugin COMPONENT] [--with-mcp] [--yes]"
      echo "                  [--revision TAG_OR_COMMIT] [--enable-extraction]"
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
    if [[ -z "$revision" ]]; then
      git -C "$source_root" pull --ff-only
    fi
  elif [[ -e "$source_root" ]]; then
    echo "Source path exists but is not a Git checkout: $source_root" >&2
    exit 1
  else
    git clone --depth 1 "$repository" "$source_root"

  fi
fi
if [[ -n "$revision" ]]; then
  if [[ ! -d "${source_root}/.git" ]]; then
    echo "--revision requires a Git source checkout: $source_root" >&2
    exit 1
  fi
  git -C "$source_root" fetch --depth 1 origin "$revision"
  git -C "$source_root" checkout --detach FETCH_HEAD
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
for value in ${agents[@]+"${agents[@]}"}; do setup_args+=(--agent "$value"); done
for value in ${plugins[@]+"${plugins[@]}"}; do setup_args+=(--plugin "$value"); done
for value in ${extra[@]+"${extra[@]}"}; do setup_args+=("$value"); done

"$venv_python" "${setup_args[@]}"
echo "Joiny-Mnemonic runtime: $venv_python"
