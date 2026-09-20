#!/bin/sh
set -eu

artifact_root="${MTP_ARTIFACT_ROOT:-/app/artifacts}"

if [ "$(id -u)" -eq 0 ]; then
    artifact_root="$(realpath -m -- "$artifact_root")"
    case "$artifact_root" in
        /app/artifacts | /app/artifacts/*) ;;
        *)
            echo "拒绝以 root 调整非 /app/artifacts 路径: $artifact_root" >&2
            exit 64
            ;;
    esac

    mkdir -p "$artifact_root"
    chown -R --no-dereference mtp:mtp "$artifact_root"
    exec gosu mtp:mtp mtp "$@"
fi

exec mtp "$@"
