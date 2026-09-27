"""Database migration runner for scheduler."""

import sqlite3
import logging
from pathlib import Path

logger = logging.getLogger(__name__)


class MigrationRunner:
    """Handles database migrations for the scheduler."""

    def __init__(self, db_path: str):
        """Initialize migration runner.

        Args:
            db_path: Path to SQLite database
        """
        self.db_path = db_path
        self._migrations = [
            "001_add_tagging",
            # Add more migrations here as needed
        ]

    def _get_connection(self) -> sqlite3.Connection:
        """Get database connection."""
        conn = sqlite3.connect(str(self.db_path), timeout=30.0)
        conn.row_factory = sqlite3.Row
        return conn

    def _get_column_names(self, table_name: str) -> set:
        """Get list of column names for a table."""
        try:
            conn = self._get_connection()
            cursor = conn.cursor()
            cursor.execute(f"PRAGMA table_info({table_name})")
            columns = {row[1] for row in cursor.fetchall()}
            conn.close()
            return columns
        except sqlite3.Error as e:
            logger.error(f"Failed to get columns for {table_name}: {e}")
            return set()

    def _add_column_if_missing(self, table_name: str, column_name: str, 
                                column_def: str) -> bool:
        """Add a column to a table if it doesn't exist.
        
        SQLite doesn't support ADD COLUMN IF NOT EXISTS, so we check first.
        
        Args:
            table_name: Name of table to modify
            column_name: Name of column to add
            column_def: Column definition (e.g., "TEXT")
            
        Returns:
            True if column was added or already exists, False on error
        """
        columns = self._get_column_names(table_name)
        if column_name in columns:
            logger.debug(f"Column {column_name} already exists in {table_name}")
            return True
        
        try:
            conn = self._get_connection()
            cursor = conn.cursor()
            cursor.execute(f"ALTER TABLE {table_name} ADD COLUMN {column_name} {column_def}")
            conn.commit()
            conn.close()
            logger.info(f"Added column {column_name} to {table_name}")
            return True
        except sqlite3.Error as e:
            logger.error(f"Failed to add column {column_name} to {table_name}: {e}")
            return False

    def _get_applied_migrations(self) -> set:
        """Get list of applied migrations."""
        try:
            conn = self._get_connection()
            cursor = conn.cursor()
            cursor.execute("""
                SELECT name FROM sqlite_master 
                WHERE type='table' AND name='schema_migrations'
            """)
            if cursor.fetchone():
                cursor.execute("SELECT migration_name FROM schema_migrations")
                return {row[0] for row in cursor.fetchall()}
            conn.close()
        except sqlite3.OperationalError:
            pass
        return set()

    def _ensure_migrations_table(self):
        """Create schema_migrations table if it doesn't exist."""
        conn = self._get_connection()
        cursor = conn.cursor()
        cursor.execute("""
            CREATE TABLE IF NOT EXISTS schema_migrations (
                migration_name TEXT PRIMARY KEY,
                applied_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )
        """)
        conn.commit()
        conn.close()

    def run_migration(self, migration_name: str) -> bool:
        """Run a single migration.

        Args:
            migration_name: Name of migration file (without .sql extension)
            
        Returns:
            True if migration applied successfully, False on error
        """
        migrations_dir = Path(__file__).parent / "migrations"
        migration_path = migrations_dir / f"{migration_name}.sql"

        if not migration_path.exists():
            # Check with .sql extension
            migration_path_with_ext = migrations_dir / f"{migration_name}.sql"
            if not migration_path_with_ext.exists():
                logger.error(f"Migration file not found: {migration_path}")
                return False
            migration_path = migration_path_with_ext

        try:
            with open(migration_path, 'r') as f:
                sql = f.read()

            conn = self._get_connection()
            cursor = conn.cursor()
            
            # Execute each statement separately (SQLite limitation)
            for statement in sql.split(';'):
                statement = statement.strip()
                if statement:
                    cursor.execute(statement)
            
            cursor.execute(
                "INSERT INTO schema_migrations (migration_name) VALUES (?)",
                (migration_name,)
            )
            conn.commit()
            conn.close()

            logger.info(f"Applied migration: {migration_name}")
            return True

        except Exception as e:
            logger.error(f"Failed to apply migration {migration_name}: {e}")
            conn.rollback()
            return False

    def run_all(self) -> bool:
        """Run all pending migrations.

        Returns:
            True if all migrations applied successfully
        """
        # Ensure migrations table exists
        self._ensure_migrations_table()

        # Get applied migrations
        applied = self._get_applied_migrations()

        # Apply pending migrations
        success = True
        for migration_name in self._migrations:
            if migration_name not in applied:
                if not self.run_migration(migration_name):
                    success = False
                    break

        return success
