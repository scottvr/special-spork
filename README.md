This is a group of scripts to assist with a migration where you may need to roll back the kernel to support replication drivers.

Step 1. Become root and run the downgrade.sh script.
Step 2. Reboot.
Step 3. Migrate.
Step 4. On the migrated server in it's new nest, run the kernel-grub-reset.sh script.  This will set the default kernel back to whatever it was before the downgrade script ran.
Step 5. Reboot
Step 6. You can now run the revert.sh script, which will unmark and remove the old packages.
Step 7. Reboot once more.