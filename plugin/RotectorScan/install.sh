#!/usr/bin/env bash
#
# RotectorScan — install or update the plugin and its themes.
#
# Copies this folder into every Equicord/Vencord data directory it finds, as
# `dynamicPlugins/RotectorScan`, and installs `theme/*.theme.css` into the
# sibling `themes/` folder. Safe to run repeatedly: it is a sync, not an append.
#
# The layout matters more than it looks. The DynamicPluginLoader treats every
# top-level entry in `dynamicPlugins/` as its own plugin — a directory becomes a
# multi-file plugin and a loose source file becomes a single-file one. Copying
# this folder's *contents* into `dynamicPlugins/` therefore does not install one
# plugin, it installs nine broken ones, and the real plugin never appears in the
# list at all. That failure is silent and is the reason this script exists.
#
#   ./install.sh                 install/update everywhere it finds a client
#   ./install.sh --pull          git pull --ff-only first, then install
#   ./install.sh --dir PATH      install into one specific Equicord data dir
#   ./install.sh --plugin-only   skip the themes
#   ./install.sh --themes-only   skip the plugin
#   ./install.sh --dry-run       print what would happen, change nothing
#   ./install.sh --uninstall     remove the plugin and themes again
#
set -euo pipefail

PLUGIN_NAME="RotectorScan"
SRC="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"

PULL=0
DRY=0
UNINSTALL=0
DO_PLUGIN=1
DO_THEMES=1
EXPLICIT_DIR=""

die() { printf '%s\n' "error: $*" >&2; exit 1; }
say() { printf '%s\n' "$*"; }
run() { if [ "$DRY" -eq 1 ]; then printf '  would: %s\n' "$*"; else "$@"; fi; }

while [ $# -gt 0 ]; do
    case "$1" in
        --pull) PULL=1 ;;
        --dry-run|-n) DRY=1 ;;
        --uninstall) UNINSTALL=1 ;;
        --plugin-only) DO_THEMES=0 ;;
        --themes-only) DO_PLUGIN=0 ;;
        --dir) shift; [ $# -gt 0 ] || die "--dir needs a path"; EXPLICIT_DIR="$1" ;;
        -h|--help) sed -n '2,25p' "$0" | sed 's/^# \{0,1\}//'; exit 0 ;;
        *) die "unknown option: $1 (try --help)" ;;
    esac
    shift
done

# --------------------------------------------------------------------------
# sanity: is this actually the plugin folder?
# --------------------------------------------------------------------------

[ -f "$SRC/index.tsx" ] || die "no index.tsx beside this script — run it from inside the $PLUGIN_NAME folder"
[ -f "$SRC/style.css" ] || die "no style.css beside this script — the folder looks incomplete"

# --------------------------------------------------------------------------
# optionally fast-forward the checkout first
# --------------------------------------------------------------------------

if [ "$PULL" -eq 1 ]; then
    if git -C "$SRC" rev-parse --git-dir >/dev/null 2>&1; then
        say "==> updating the checkout"
        # --ff-only on purpose: this script must never create a merge commit or
        # leave a half-rebased tree behind somebody's working changes.
        if [ "$DRY" -eq 1 ]; then
            printf '  would: git -C %s pull --ff-only\n' "$SRC"
        elif ! git -C "$SRC" pull --ff-only; then
            die "git pull --ff-only failed — resolve the checkout by hand, then re-run"
        fi
    else
        say "==> --pull ignored: this is not a git checkout"
    fi
fi

# --------------------------------------------------------------------------
# where the clients keep their data
#
# Equicord derives both directories from one DATA_DIR (see the upstream
# src/main/utils/constants.ts): `<DATA_DIR>/dynamicPlugins` and
# `<DATA_DIR>/themes`. EQUICORD_USER_DATA_DIR overrides it, so it is honoured
# here too. Everything else is the per-platform default for each client.
# --------------------------------------------------------------------------

candidates=()

if [ -n "$EXPLICIT_DIR" ]; then
    candidates+=("$EXPLICIT_DIR")
elif [ -n "${EQUICORD_USER_DATA_DIR:-}" ]; then
    candidates+=("$EQUICORD_USER_DATA_DIR")
else
    case "$(uname -s)" in
        Darwin)
            candidates+=("$HOME/Library/Application Support/Equicord")
            candidates+=("$HOME/Library/Application Support/Vencord")
            ;;
        Linux)
            candidates+=("${XDG_CONFIG_HOME:-$HOME/.config}/Equicord")
            candidates+=("${XDG_CONFIG_HOME:-$HOME/.config}/Vencord")
            # Flatpak sandboxes keep their own config tree, writable from the host
            candidates+=("$HOME/.var/app/com.discordapp.Discord/config/Equicord")
            candidates+=("$HOME/.var/app/com.discordapp.DiscordCanary/config/Equicord")
            candidates+=("$HOME/.var/app/dev.vencord.Vesktop/config/Equicord")
            candidates+=("$HOME/.var/app/io.github.equicord.equibop/config/Equicord")
            candidates+=("$HOME/.var/app/com.discordapp.Discord/config/Vencord")
            candidates+=("$HOME/.var/app/dev.vencord.Vesktop/config/Vencord")
            ;;
        MINGW*|MSYS*|CYGWIN*)
            [ -n "${APPDATA:-}" ] && candidates+=("$APPDATA/Equicord" "$APPDATA/Vencord")
            ;;
        *)
            candidates+=("${XDG_CONFIG_HOME:-$HOME/.config}/Equicord")
            ;;
    esac
fi

# A directory counts as a real install if it already holds either of the two
# folders the client itself creates. An --dir the user named is taken on trust:
# they may be setting one up before its first launch.
targets=()
for dir in "${candidates[@]}"; do
    if [ -n "$EXPLICIT_DIR" ] || [ -d "$dir/dynamicPlugins" ] || [ -d "$dir/themes" ] || [ -d "$dir/settings" ]; then
        targets+=("$dir")
    fi
done

if [ "${#targets[@]}" -eq 0 ]; then
    say "No Equicord or Vencord data directory found. Looked in:"
    for dir in "${candidates[@]}"; do say "  $dir"; done
    say ""
    say "Start the client once so it creates its folders, or name one:"
    say "  ./install.sh --dir '$HOME/.config/Equicord'"
    exit 1
fi

# --------------------------------------------------------------------------
# install / update / remove
# --------------------------------------------------------------------------

[ "$DRY" -eq 1 ] && say "(dry run — nothing will be written)"

for dir in "${targets[@]}"; do
    say "==> $dir"
    dest="$dir/dynamicPlugins/$PLUGIN_NAME"
    themes="$dir/themes"

    if [ "$UNINSTALL" -eq 1 ]; then
        if [ "$DO_PLUGIN" -eq 1 ] && [ -d "$dest" ]; then
            run rm -rf -- "$dest"
            say "  removed the plugin"
        fi
        if [ "$DO_THEMES" -eq 1 ]; then
            for f in "$SRC"/theme/*.theme.css; do
                [ -e "$f" ] || continue
                target="$themes/$(basename "$f")"
                # Only remove a theme file this plugin actually ships, so a
                # theme somebody wrote themselves is never collateral.
                [ -f "$target" ] && { run rm -f -- "$target"; say "  removed $(basename "$f")"; }
            done
        fi
        continue
    fi

    if [ "$DO_PLUGIN" -eq 1 ]; then
        run mkdir -p -- "$dir/dynamicPlugins"

        # A stale file left from an older version would still be compiled and
        # still be mirrored into settings.json, so the plugin folder is replaced
        # rather than merged. Only this plugin's own folder is touched.
        if [ -d "$dest" ]; then
            run rm -rf -- "$dest"
        fi
        run mkdir -p -- "$dest"
        if [ "$DRY" -eq 1 ]; then
            printf '  would: copy %s -> %s\n' "$SRC" "$dest"
        else
            # -a would carry ownership and timestamps that mean nothing here;
            # the plugin is plain text and the loader reads it fresh each time.
            (cd "$SRC" && tar -cf - \
                --exclude=".git" --exclude="node_modules" --exclude="__pycache__" \
                .) | (cd "$dest" && tar -xf -)
        fi
        say "  plugin -> dynamicPlugins/$PLUGIN_NAME"
    fi

    if [ "$DO_THEMES" -eq 1 ]; then
        shopt -s nullglob
        theme_files=("$SRC"/theme/*.theme.css)
        shopt -u nullglob
        if [ "${#theme_files[@]}" -eq 0 ]; then
            say "  no themes to install"
        else
            run mkdir -p -- "$themes"
            for f in "${theme_files[@]}"; do
                run cp -f -- "$f" "$themes/"
                say "  theme  -> themes/$(basename "$f")"
            done
        fi
    fi
done

# --------------------------------------------------------------------------
# what to do next
# --------------------------------------------------------------------------

say ""
if [ "$UNINSTALL" -eq 1 ]; then
    say "Removed. Hit Reload all in DynamicPluginLoader's settings, and untick the"
    say "themes under Settings -> Themes if they were enabled."
    exit 0
fi

say "Done. In Discord:"
say "  1. Settings -> Plugins -> DynamicPluginLoader -> Reload all"
say "  2. Enable $PLUGIN_NAME if it is not already on."
say "     A first enable also switches on MemberListDecoratorsAPI,"
say "     MessageDecorationsAPI and ProfileSectionsAPI, which only take effect at"
say "     startup — the loader will say 'Needs reload'. Restart Discord once."
if [ "$DO_THEMES" -eq 1 ]; then
    say "  3. Settings -> Themes -> tick ten-thousand.theme.css (or the -light one)."
    say "     The themes are optional; the plugin is designed to look right without them."
fi
