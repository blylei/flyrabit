# (c) 2020 Frabit Project maintained and limited by Blylei < blylei918@gmail.com >
# GNU General Public License v3.0+ (see COPYING or https://www.gnu.org/licenses/gpl-3.0.txt)
#
# This file is part of Frabit
#
"""
Backup engine module

A Backup engine is a class responsible for the execution
of a backup. Specific implementations of backups are defined by
classes that derive from BackupExecutor (e.g.: backup with rsync
through Ssh).

A BackupExecutor is invoked by the BackupManager for backup operations.
"""
import datetime
import logging
import os
import re
import shutil
from abc import ABCMeta, abstractmethod

import frabit
from frabit import output


_logger = logging.getLogger(__name__)


class BackupEngine(ABCMeta):
    """抽象类，在下列备份执行引擎里实现：
    1）物理备份：全量 VS 增量
    2）逻辑备份：--all-databases --database --none
    3）Binlog备份：
    """

    def __init__(self, backup_manager, mode=None):
        """
        Base constructor

        :param frabit.backup.BackupManager backup_manager: the BackupManager assigned to the executor
        """
        super(BackupEngine, self).__init__()
        self.backup_manager = backup_manager
        self.server = backup_manager.server
        self.config = backup_manager.config
        self.strategy = None
        self._mode = mode
        self.copy_start_time = None
        self.copy_end_time = None

        # Holds the action being executed. Used for error messages.
        self.current_action = None

    def init(self):
        """初始化执行引擎"""


class PhysicalBackup(BackupEngine):
    """调用xreabackup来执行物理备份任务"""
    def __init__(self):
        """NONE"""
        pass

    def full_backup(self):
        """执行全量备份任务"""
        pass

    def incr_backup(self):
        """执行增量备份任务"""
        pass


class LogicalBackup(BackupEngine):
    """调用mysqldump来执行逻辑备份任务"""
    def __init__(self):
        """NONE"""
        pass

    def all_dbs_backup(self):
        """mysqldump --all-databases:对指定实例上的所有库进行备份"""
        pass

    def some_dbs_backup(self):
        """mysqldump --databases:对指定实例上的部分库进行备份"""
        pass

    def others_backup(self):
        """mysqldump :对指定实例上的库或表进行备份"""
        pass


class BinlogBackup(BackupEngine):
    """调用mysqlbinlog来执行binlog备份任务"""
    def __init__(self):
        """NONE"""
        pass

    def binlog_backup(self):
        """对binlog进行备份"""
        pass


class BackupStrategy(ABCMeta):
    """备份策略解析"""
    def __init__(self):
        """NONE"""
        pass

    def start_backup(self, backup_info):
        """
        开启一个备份 - 由 BackupEngine.backup()调用

        :param frabit.infofile.BackupInfo backup_info: 备份任务基本信息
        """
        # Retrieve PostgreSQL server metadata
        self._pg_get_metadata(backup_info)

        # Record that we are about to start the backup
        self.current_action = "issuing start backup command"
        _logger.debug(self.current_action)

    @abstractmethod
    def stop_backup(self, backup_info):
        """
        停止备份任务 - 由 BackupEngine.backup()调用

        :param frabit.infofile.LocalBackupInfo backup_info: 备份任务基本信息
        """

    @abstractmethod
    def check(self, check_strategy):
        """
        执行常规性的检查- 由BackupEngine.check()调用

        :param CheckStrategy check_strategy: 备份策略管理
        """

    def _mysql_get_metadata(self, backup_info):
        """
        将MySQL备份相关的元数据解析到 backup_info 对象内

        :param frabit.infofile.BackupInfo backup_info: 备份任务基本信息
        """
        # 获取MySQL的数据文件夹
        self.current_action = 'detecting data_dir'
        output.debug(self.current_action)
        data_dir = self.mysql.get_setting('data_dir')
        backup_info.set_attribute('data_dir', data_dir)

        # Set server version
        backup_info.set_attribute('version', self.mysql.server_version)

        # Set binlog file size
        backup_info.set_attribute('max_binlog_size',
                                  self.mysql.binlog_file_size)

        # Set configuration files location
        cf = self.mysql.get_configuration_files()
        for key in cf:
            backup_info.set_attribute(key, cf[key])


class MysqlBackupStrategy(BackupStrategy):
    """
    Concrete class for MySQL backup strategy.

    This strategy is for PostgresBackupExecutor only and is responsible for
    executing pre e post backup operations during a physical backup executed
    using pg_basebackup.
    """

    def check(self, check_strategy):
        """
        Perform additional checks for the Postgres backup strategy
        """

    def start_backup(self, backup_info):
        """
        Manage the start of an pg_basebackup backup

        The method performs all the preliminary operations required for a
        backup executed using pg_basebackup to start, gathering information
        from postgres and filling the backup_info.

        :param frabit.infofile.LocalBackupInfo backup_info: backup information
        """
        self.current_action = "initialising mysql backup_method"
        super(MysqlBackupStrategy, self).start_backup(backup_info)
        current_xlog_info = self.postgres.current_xlog_info
        self._backup_info_from_start_location(backup_info, current_xlog_info)

    def stop_backup(self, backup_info):
        """
        Manage the stop of an pg_basebackup backup

        The method retrieves the information necessary for the
        backup.info file reading the backup_label file.

        Due of the nature of the pg_basebackup, information that are gathered
        during the start of a backup performed using rsync, are retrieved
        here

        :param barman.infofile.LocalBackupInfo backup_info: backup information
        """
        self._read_backup_label(backup_info)
        self._backup_info_from_backup_label(backup_info)

        # Set data in backup_info from current_xlog_info
        self.current_action = "stopping postgres backup_method"
        output.info("Finalising the backup.")

        # Get the current xlog position
        current_xlog_info = self.postgres.current_xlog_info
        if current_xlog_info:
            self._backup_info_from_stop_location(
                backup_info, current_xlog_info)

        # Ask PostgreSQL to switch to another WAL file. This is needed
        # to archive the transaction log file containing the backup
        # end position, which is required to recover from the backup.
        try:
            self.postgres.switch_wal()
        except PostgresIsInRecovery:
            # Skip switching XLOG if a standby server
            pass
