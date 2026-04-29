"""User-facing commands for hardening the on-disk install.

The `security` subcommand group exposes:

* ``status``  — print the active secrets backend, filesystem perms,
  FDE state, and any cloud-sync warnings the doctor would surface.
* ``migrate`` — move credentials between the DB and the OS keychain.

Phase 3 will add ``upgrade``, ``rotate-passphrase``, ``unlock``,
``lock`` for passphrase-encrypted secrets. They're scoped here so the
namespace stays one ``unread security`` deep.
"""
