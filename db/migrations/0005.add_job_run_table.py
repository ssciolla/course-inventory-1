#
# file: migrations/0004.add_sis_id.py
#
from yoyo import step

__depends__ = {'0004.add_sis_id'}

step('''
    CREATE TABLE IF NOT EXISTS job_run
    (
        id INTEGER NOT NULL UNIQUE AUTO_INCREMENT,
        timestamp DATETIME NOT NULL,
        PRIMARY KEY (id)
    )
    ENGINE=InnoDB
    CHARACTER SET utf8mb4;
''')
