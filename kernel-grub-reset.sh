#!/usr/bin/env bash
#
# kernel-grub-reset.sh — Reset GRUB default to boot the newest kernel
#
# Run this AFTER the migration completes but BEFORE the reboot that
# precedes kernel-revert.sh. It restores /etc/default/grub from the
# backup taken by kernel-downgrade.sh and forces GRUB_DEFAULT=0 so
# the next boot lands on the newest installed kernel (typically the
# pre-downgrade one).

set -euo pipefail

STATE_DIR="/var/lib/kernel-migration"
STATE_FILE="${STATE_DIR}/state.env"
GRUB_DEFAULT_FILE="/etc/default/grub"
LOG_FILE="/var/log/kernel-grub-reset.log"

exec > >(tee -a "$LOG_FILE") 2>&1

log() { echo "[$(date '+%F %T')] [*] $*"; }
err() { echo "[$(date '+%F %T')] [✗] $*" >&2; }

if [[ $EUID -ne 0 ]]; then
    err "Must run as root."
    exit 1
fi

if [[ ! -f "$STATE_FILE" ]]; then
    err "State file ${STATE_FILE} not found. Did kernel-downgrade.sh run?"
    exit 1
fi

# shellcheck disable=SC1090
source "$STATE_FILE"

if [[ "${NO_OP:-false}" == "true" ]]; then
    log "Downgrade was a no-op. Nothing to reset."
    exit 0
fi

log "Resetting GRUB default to boot newest kernel"
log "  Currently running: $(uname -r)"
log "  PREVIOUS_KERNEL=${PREVIOUS_KERNEL:-<unknown>}"
log "  TARGET_KERNEL=${TARGET_KERNEL:-<unknown>}"

# 1. Restore /etc/default/grub from the pre-downgrade backup
if [[ -n "${GRUB_BACKUP:-}" && -f "$GRUB_BACKUP" ]]; then
    log "Restoring ${GRUB_DEFAULT_FILE} from ${GRUB_BACKUP}"
    cp -a "$GRUB_BACKUP" "$GRUB_DEFAULT_FILE"
else
    err "GRUB backup not found (${GRUB_BACKUP:-<unset>}). Patching in place instead."
fi

# 2. Force GRUB_DEFAULT=0 regardless of what the backup contained.
#    Entry 0 is the top-level "Ubuntu" entry, which boots the newest
#    installed kernel — this is what we want post-migration.
set_grub_var() {
    local key="$1" value="$2"
    if grep -qE "^\s*${key}=" "$GRUB_DEFAULT_FILE"; then
        sed -i "s|^\s*${key}=.*|${key}=${value}|" "$GRUB_DEFAULT_FILE"
    else
        echo "${key}=${value}" >> "$GRUB_DEFAULT_FILE"
    fi
}

log "Forcing GRUB_DEFAULT=0 (newest kernel)"
set_grub_var "GRUB_DEFAULT"     "0"
set_grub_var "GRUB_SAVEDEFAULT" "false"

# 3. Regenerate grub.cfg
log "Running update-grub..."
update-grub

# 4. Sanity check: confirm the newest kernel is not the downgrade target
NEWEST_INSTALLED="$(ls -1 /boot/vmlinuz-* 2>/dev/null | sed 's|.*/vmlinuz-||' | sort -V | tail -n1 || true)"
log "Newest installed kernel on disk: ${NEWEST_INSTALLED:-<none>}"

if [[ "${NEWEST_INSTALLED}" == "${TARGET_KERNEL}" ]]; then
    err "Newest installed kernel is still ${TARGET_KERNEL}."
    err "After reboot, the system will boot back into the downgrade target."
    err "Verify that ${PREVIOUS_KERNEL} is still installed before rebooting."
    exit 2
fi

log "GRUB reset complete. Next reboot will load ${NEWEST_INSTALLED}."
exit 0
