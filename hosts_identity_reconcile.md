
```bash
ansible-playbook -i inventory hosts_identity_reconcile.yml -e hosts_reconcile_state=audit
```

```bash
ansible-playbook -i inventory hosts_identity_reconcile.yml -e hosts_reconcile_state=remediate
```
