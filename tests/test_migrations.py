"""Tests for database migration system."""

import sqlite3
import pytest
import tempfile
from pathlib import Path

from gtx_broker.scheduler import Scheduler, SchedulerConfig
from gtx_broker.scheduler.migrations import MigrationRunner


@pytest.fixture
def temp_db():
    """Create a temporary database file."""
    with tempfile.NamedTemporaryFile(suffix='.db', delete=False) as f:
        temp_path = f.name
    yield temp_path
    Path(temp_path).unlink(missing_ok=True)


@pytest.fixture
def scheduler():
    """Create a scheduler with temporary database in a valid location."""
    with tempfile.TemporaryDirectory() as tmpdir:
        db_path = Path(tmpdir) / "tasks.db"
        config = SchedulerConfig(db_path=str(db_path))
        yield Scheduler(config)


class TestMigrationRunner:
    """Tests for MigrationRunner class."""

    def test_migration_not_found(self, scheduler):
        """Test that missing migration files are handled gracefully."""
        runner = MigrationRunner(scheduler.config.db_path)
        result = runner.run_migration("nonexistent_migration")
        assert result is False

    def test_migration_idempotency(self, scheduler):
        """Test that running migrations twice is safe."""
        runner = MigrationRunner(scheduler.config.db_path)
        
        # First run
        result1 = runner.run_all()
        assert result1 is True
        
        # Second run (should be no-op)
        result2 = runner.run_all()
        assert result2 is True

    def test_batch_epochs_table_created(self, scheduler):
        """Test that batch_epochs table is created."""
        runner = MigrationRunner(scheduler.config.db_path)
        runner.run_all()
        
        conn = sqlite3.connect(str(scheduler.config.db_path))
        cursor = conn.cursor()
        
        # Check table exists
        cursor.execute("SELECT name FROM sqlite_master WHERE type='table' AND name='batch_epochs'")
        assert cursor.fetchone() is not None
        
        # Check columns
        cursor.execute("PRAGMA table_info(batch_epochs)")
        columns = {row[1] for row in cursor.fetchall()}
        assert 'id' in columns
        assert 'name' in columns
        assert 'start_time' in columns
        assert 'updated_at' in columns
        
        conn.close()

    def test_indexes_created(self, scheduler):
        """Test that indexes are created for efficient querying."""
        runner = MigrationRunner(scheduler.config.db_path)
        
        # Run migration (scheduler already created tasks table with new columns)
        runner.run_all()
        
        # Check indexes exist
        conn = sqlite3.connect(str(scheduler.config.db_path))
        cursor = conn.cursor()
        cursor.execute("SELECT name FROM sqlite_master WHERE type='index' AND name LIKE 'idx_tasks_%'")
        indexes = {row[0] for row in cursor.fetchall()}
        
        assert 'idx_tasks_review_tag' in indexes
        assert 'idx_tasks_schedule_type' in indexes
        assert 'idx_tasks_batch_epoch' in indexes
        
        conn.close()


class TestSchemaUpgrade:
    """Tests for schema upgrade from old databases."""

    def test_fresh_database_has_all_columns(self):
        """Test that a fresh database has all new columns."""
        with tempfile.TemporaryDirectory() as tmpdir:
            db_path = Path(tmpdir) / "tasks.db"
            config = SchedulerConfig(db_path=str(db_path))
            scheduler = Scheduler(config)
            
            conn = sqlite3.connect(str(db_path))
            cursor = conn.cursor()
            cursor.execute("PRAGMA table_info(tasks)")
            columns = {row[1] for row in cursor.fetchall()}
            
            assert 'review_tag' in columns
            assert 'schedule_type' in columns
            assert 'batch_epoch_id' in columns
            
            conn.close()

    def test_upgrade_old_schema(self):
        """Test upgrading a database without new columns."""
        with tempfile.TemporaryDirectory() as tmpdir:
            db_path = Path(tmpdir) / "tasks.db"
            
            # Create old schema manually
            conn = sqlite3.connect(str(db_path))
            cursor = conn.cursor()
            
            cursor.execute("""
                CREATE TABLE tasks (
                    id TEXT PRIMARY KEY,
                    kind TEXT NOT NULL,
                    state TEXT NOT NULL DEFAULT 'queued',
                    priority INTEGER NOT NULL DEFAULT 0,
                    mode TEXT NOT NULL DEFAULT 'batch',
                    payload TEXT,
                    input_path TEXT,
                    output_path TEXT,
                    idempotency_key TEXT UNIQUE,
                    deadline_timestamp TEXT,
                    retry_policy TEXT,
                    retry_at TIMESTAMP,
                    error TEXT,
                    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                    updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
                )
            """)
            
            # Insert a test task
            cursor.execute("""
                INSERT INTO tasks (id, kind, state, payload)
                VALUES ('TEST-001', 'vision', 'queued', '{}')
            """)
            conn.commit()
            conn.close()
            
            # Now upgrade by creating scheduler
            config = SchedulerConfig(db_path=str(db_path))
            scheduler = Scheduler(config)
            
            # Verify columns were added
            conn = sqlite3.connect(str(db_path))
            cursor = conn.cursor()
            cursor.execute("PRAGMA table_info(tasks)")
            columns = {row[1] for row in cursor.fetchall()}
            
            assert 'review_tag' in columns
            assert 'schedule_type' in columns
            assert 'batch_epoch_id' in columns
            
            # Verify existing data survived
            cursor.execute("SELECT * FROM tasks WHERE id = 'TEST-001'")
            task = cursor.fetchone()
            assert task is not None
            assert task[0] == 'TEST-001'
            assert task[1] == 'vision'
            
            conn.close()

    def test_upgrade_preserves_data(self):
        """Test that upgrade doesn't lose existing data."""
        with tempfile.TemporaryDirectory() as tmpdir:
            db_path = Path(tmpdir) / "tasks.db"
            
            # Create database with old schema and data
            conn = sqlite3.connect(str(db_path))
            cursor = conn.cursor()
            
            cursor.execute("""
                CREATE TABLE tasks (
                    id TEXT PRIMARY KEY,
                    kind TEXT NOT NULL,
                    state TEXT NOT NULL DEFAULT 'queued',
                    priority INTEGER NOT NULL DEFAULT 0,
                    mode TEXT NOT NULL DEFAULT 'batch',
                    payload TEXT,
                    input_path TEXT,
                    output_path TEXT,
                    idempotency_key TEXT UNIQUE,
                    deadline_timestamp TEXT,
                    retry_policy TEXT,
                    retry_at TIMESTAMP,
                    error TEXT,
                    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                    updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
                )
            """)
            
            # Insert multiple test tasks
            for i in range(5):
                cursor.execute("""
                    INSERT INTO tasks (id, kind, state, priority, payload, input_path)
                    VALUES (?, ?, ?, ?, ?, ?)
                """, (f'TEST-{i:03d}', 'vision' if i % 2 == 0 else 'coding', 
                      'queued' if i % 3 == 0 else 'claimed', i * 10,
                      '{"test": true}', f'/path/to/image_{i}.jpg'))
            
            conn.commit()
            conn.close()
            
            # Upgrade
            config = SchedulerConfig(db_path=str(db_path))
            scheduler = Scheduler(config)
            
            # Verify all tasks survived
            conn = sqlite3.connect(str(db_path))
            cursor = conn.cursor()
            cursor.execute("SELECT COUNT(*) FROM tasks")
            count = cursor.fetchone()[0]
            assert count == 5
            
            # Verify specific data
            cursor.execute("SELECT kind, priority, input_path FROM tasks ORDER BY id")
            rows = cursor.fetchall()
            assert rows[0][0] == 'vision'
            assert rows[0][1] == 0
            assert 'image_0' in rows[0][2]
            
            conn.close()

    def test_upgrade_creates_indexes(self):
        """Test that indexes are created during upgrade."""
        with tempfile.TemporaryDirectory() as tmpdir:
            db_path = Path(tmpdir) / "tasks.db"
            
            # Create old schema
            conn = sqlite3.connect(str(db_path))
            cursor = conn.cursor()
            
            cursor.execute("""
                CREATE TABLE tasks (
                    id TEXT PRIMARY KEY,
                    kind TEXT NOT NULL,
                    state TEXT NOT NULL DEFAULT 'queued',
                    priority INTEGER NOT NULL DEFAULT 0,
                    mode TEXT NOT NULL DEFAULT 'batch',
                    payload TEXT,
                    input_path TEXT,
                    output_path TEXT,
                    idempotency_key TEXT UNIQUE,
                    deadline_timestamp TEXT,
                    retry_policy TEXT,
                    retry_at TIMESTAMP,
                    error TEXT,
                    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                    updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
                )
            """)
            
            cursor.execute("""
                CREATE TABLE task_attempts (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    task_id TEXT NOT NULL,
                    worker_profile TEXT,
                    model_profile TEXT,
                    start_at TIMESTAMP,
                    end_at TIMESTAMP,
                    result TEXT,
                    failure_class TEXT,
                    resource_evidence TEXT,
                    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                    FOREIGN KEY (task_id) REFERENCES tasks(id)
                )
            """)
            
            cursor.execute("""
                CREATE TABLE task_events (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    task_id TEXT NOT NULL,
                    event_type TEXT NOT NULL,
                    from_state TEXT,
                    to_state TEXT,
                    details TEXT,
                    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
                )
            """)
            
            conn.commit()
            conn.close()
            
            # Upgrade
            config = SchedulerConfig(db_path=str(db_path))
            scheduler = Scheduler(config)
            
            # Verify indexes created
            conn = sqlite3.connect(str(db_path))
            cursor = conn.cursor()
            cursor.execute("SELECT name FROM sqlite_master WHERE type='index' AND name LIKE 'idx_tasks_%'")
            indexes = {row[0] for row in cursor.fetchall()}
            
            assert 'idx_tasks_review_tag' in indexes
            assert 'idx_tasks_schedule_type' in indexes
            assert 'idx_tasks_batch_epoch' in indexes
            
            conn.close()


class TestConnectionHandling:
    """Tests for connection leak fixes."""

    def test_connection_closed_in_migration_runner(self, scheduler):
        """Test that connections are properly closed in MigrationRunner."""
        runner = MigrationRunner(scheduler.config.db_path)
        
        # Run migration
        result = runner.run_all()
        assert result is True
        
        # Run multiple times to ensure no connection leaks
        for _ in range(10):
            result = runner.run_all()
            assert result is True
