#!/usr/bin/env bash
#
# kernel-revert.sh — Unattended revert of kernel-downgrade.sh
#
# Reads /var/lib/kernel-migration/state.env, restores GRUB config,
# removes the downgrade kernel (if we installed it), and restores holds.
#
# Intended to run AFTER the post-migration reboot onto the newer kernel.

set -euo pipefail

STATE_DIR="/var/lib/kernel-migration"
STATE_FILE="${STATE_DIR}/state.env"
GRUB_DEFAULT_FILE="/etc/default/grub"
LOG_FILE="/var/log/kernel-revert.log"

exec > >(tee -a "$LOG_FILE") 2>&1

log() { echo "[$(date '+%F %T')] [*] $*"; }
err() { echo "[$(date '+%F %T')] [✗] $*" >&2; }

if [[ $EUID -ne 0 ]]; then
    err "Must run as root."
    exit 1
fi

if [[ ! -f "$STATE_FILE" ]]; then
    err "State file ${STATE_FILE} not found. Nothing to revert."
    exit 1
fi

# shellcheck disable=SC1090
source "$STATE_FILE"

log "Reverting kernel downgrade"
log "  PREVIOUS_KERNEL=${PREVIOUS_KERNEL:-<unknown>}"
log "  TARGET_KERNEL=${TARGET_KERNEL:-<unknown>}"
log "  KERNEL_WAS_INSTALLED_BY_US=${KERNEL_WAS_INSTALLED_BY_US:-false}"
log "  NO_OP=${NO_OP:-false}"
log "  Currently running: $(uname -r)"

if [[ "${NO_OP:-false}" == "true" ]]; then
    log "Downgrade was a no-op. Removing state file only."
    rm -f "$STATE_FILE"
    exit 0
fi

# 1. Safety check: refuse to remove the kernel we're currently running on
RUNNING_KERNEL="$(uname -r)"
REMOVE_KERNEL="false"
if [[ "${KERNEL_WAS_INSTALLED_BY_US}" == "true" ]]; then
    if [[ "$RUNNING_KERNEL" == "$TARGET_KERNEL" ]]; then
        err "System is still running ${TARGET_KERNEL}. Reboot onto ${PREVIOUS_KERNEL} before reverting."
        exit 2
    fi
    REMOVE_KERNEL="true"
fi

# 2. Release holds placed by the downgrade script
log "Releasing apt holds on kernel packages..."
apt-mark unhold \
    "linux-image-${TARGET_KERNEL}" \
    "linux-headers-${TARGET_KERNEL}" \
    linux-image-generic \
    linux-headers-generic \
    linux-generic 2>&1 || true

# Restore any holds that existed before the downgrade (best-effort)
if [[ -n "${PREVIOUS_HOLDS:-}" ]]; then
    log "Restoring pre-migration holds: ${PREVIOUS_HOLDS}"
    # shellcheck disable=SC2086
    apt-mark hold ${PREVIOUS_HOLDS} 2>&1 || true
fi

# 3. Restore /etc/default/grub from backup
if [[ -n "${GRUB_BACKUP:-}" && -f "$GRUB_BACKUP" ]]; then
    log "Restoring ${GRUB_DEFAULT_FILE} from ${GRUB_BACKUP}"
    cp -a "$GRUB_BACKUP" "$GRUB_DEFAULT_FILE"
else
    err "GRUB backup not found (${GRUB_BACKUP:-<unset>}). Skipping GRUB file restore."
fi

# 4. Remove the downgrade kernel (only if we installed it and we're not running it)
if [[ "$REMOVE_KERNEL" == "true" ]]; then
    log "Removing ${TARGET_KERNEL} packages..."
    DEBIAN_FRONTEND=noninteractive apt-get purge -y \
        "linux-image-${TARGET_KERNEL}" \
        "linux-headers-${TARGET_KERNEL}" \
        "linux-modules-${TARGET_KERNEL}" \
        "linux-modules-extra-${TARGET_KERNEL}" 2>&1 || \
        err "Purge reported errors; continuing."
else
    log "Leaving ${TARGET_KERNEL} in place (not installed by us, or still running)."
fi

# 5. Regenerate grub.cfg to reflect restored config + removed kernel
log "Running update-grub..."
update-grub

# 6. Clean up state
log "Removing state file ${STATE_FILE}"
rm -f "$STATE_FILE"

log "Revert complete. Currently running: $(uname -r)"
log "A reboot is recommended to confirm GRUB default behavior."
exit 0
