"""Backup/restore implementation deferred to Phase 6; never assume a copied WAL DB is valid."""

class BackupNotImplementedError(NotImplementedError):
    pass


def backup_database() -> None:
    raise BackupNotImplementedError("Safe online SQLite backups are deferred to Phase 6")
