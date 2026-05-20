# MGN Post-Launch SSM Commands for `/etc/hosts` Cleanup

Use these commands as post-launch actions in AWS MGN so migrated hosts do not keep stale on-prem IP-to-hostname mappings.

## Linux (AWS-RunShellScript)

This command:
- Backs up `/etc/hosts`
- Removes prior mappings for the local host's short/FQDN names
- Rebuilds a managed block with current launch-time identity values

```bash
set -euo pipefail

hosts_file="/etc/hosts"
backup_file="/var/backups/hosts.pre-mgn.$(date +%Y%m%d%H%M%S)"
marker_begin="# BEGIN MGN LOCAL HOSTS MANAGED"
marker_end="# END MGN LOCAL HOSTS MANAGED"

[ -f "$hosts_file" ] || exit 0
cp -a "$hosts_file" "$backup_file"

short="$(hostname -s 2>/dev/null || hostname)"
fqdn="$(hostname -f 2>/dev/null || true)"
primary_ip="$(ip -o -4 route get 1.1.1.1 2>/dev/null | awk '{for(i=1;i<=NF;i++) if ($i=="src") {print $(i+1); exit}}')"

managed_names=""
if [ -n "${short:-}" ]; then
  managed_names="$short"
fi
if [ -n "${fqdn:-}" ] && [ "$fqdn" != "$short" ]; then
  managed_names="${managed_names} ${fqdn}"
fi

tmp="$(mktemp)"
trap 'rm -f "$tmp"' EXIT

awk \
  -v marker_begin="$marker_begin" \
  -v marker_end="$marker_end" \
  -v managed_names="$managed_names" '
  BEGIN {
    in_block = 0
    n = split(tolower(managed_names), a, /[[:space:]]+/)
    for (i = 1; i <= n; i++) {
      if (a[i] != "") {
        managed[a[i]] = 1
      }
    }
  }
  {
    raw = $0
    if (raw == marker_begin) { in_block = 1; next }
    if (raw == marker_end)   { in_block = 0; next }
    if (in_block == 1) { next }

    line = raw
    sub(/[[:space:]]*#.*/, "", line)
    gsub(/^[[:space:]]+|[[:space:]]+$/, "", line)
    if (line == "") { print raw; next }

    c = split(line, p, /[[:space:]]+/)
    if (c < 2) { print raw; next }

    for (i = 2; i <= c; i++) {
      h = tolower(p[i])
      if (h in managed) { next }
    }

    print raw
  }
' "$hosts_file" > "$tmp"

{
  echo "$marker_begin"
  echo "127.0.0.1 localhost"
  echo "::1 localhost ip6-localhost ip6-loopback"
  echo "ff02::1 ip6-allnodes"
  echo "ff02::2 ip6-allrouters"
  if [ -n "$managed_names" ]; then
    echo "127.0.1.1 $managed_names"
    if [ -n "${primary_ip:-}" ]; then
      echo "$primary_ip $managed_names"
    fi
  fi
  echo "$marker_end"
} >> "$tmp"

cp "$tmp" "$hosts_file"
chmod 0644 "$hosts_file"
```

## Windows (AWS-RunPowerShellScript)

For Windows MGN cutovers, this is a simple baseline cleanup for `hosts`:

```powershell
$hostsFile = 'C:\Windows\System32\drivers\etc\hosts'
if (-not (Test-Path -LiteralPath $hostsFile)) { exit 0 }

$backup = "C:\Windows\System32\drivers\etc\hosts.pre-mgn.$((Get-Date).ToString('yyyyMMddHHmmss')).bak"
Copy-Item -LiteralPath $hostsFile -Destination $backup -Force

$short = $env:COMPUTERNAME.ToLowerInvariant()
$fqdn = ([System.Net.Dns]::GetHostEntry($env:COMPUTERNAME).HostName).ToLowerInvariant()
$managed = @($short)
if ($fqdn -and $fqdn -ne $short) { $managed += $fqdn }

$markerBegin = '# BEGIN MGN LOCAL HOSTS MANAGED'
$markerEnd = '# END MGN LOCAL HOSTS MANAGED'

$output = New-Object System.Collections.Generic.List[string]
$inBlock = $false

Get-Content -LiteralPath $hostsFile | ForEach-Object {
  $raw = $_
  if ($raw -eq $markerBegin) { $inBlock = $true; return }
  if ($raw -eq $markerEnd) { $inBlock = $false; return }
  if ($inBlock) { return }

  $line = ($raw -replace '\s*#.*$', '').Trim()
  if ([string]::IsNullOrWhiteSpace($line)) { $output.Add($raw); return }

  $parts = $line -split '\s+'
  if ($parts.Count -lt 2) { $output.Add($raw); return }

  $drop = $false
  for ($i = 1; $i -lt $parts.Count; $i++) {
    if ($managed -contains $parts[$i].ToLowerInvariant()) {
      $drop = $true
      break
    }
  }

  if (-not $drop) { $output.Add($raw) }
}

$output.Add($markerBegin)
$output.Add('127.0.0.1 localhost')
if ($managed.Count -gt 0) {
  $output.Add(('127.0.0.1 {0}' -f ($managed -join ' ')))
}
$output.Add($markerEnd)

Set-Content -LiteralPath $hostsFile -Value $output -Encoding ASCII
```

## How to pair this with Ansible

Run this playbook in `audit` mode after cutover:

```bash
ansible-playbook -i inventory hosts_identity_reconcile.yml -e hosts_reconcile_state=audit
```

If it reports drift, run remediation:

```bash
ansible-playbook -i inventory hosts_identity_reconcile.yml -e hosts_reconcile_state=remediate
```
