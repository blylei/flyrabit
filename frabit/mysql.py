# (c) 2020 Frabit Project maintained and limited by Blylei < blylei918@gmail.com >
# GNU General Public License v3.0+ (see COPYING or https://www.gnu.org/licenses/gpl-3.0.txt)
#
# This file is part of Frabit
#
"""
Based on mysql_connector封装对MySQL的操作接口,Frabit对MySQL的操作均通过此模块实现
"""
import os
import sys
import re
import datetime
import time
import logging

import mysql
from mysql import connector
from mysql.connector import errorcode

import frabit
from frabit import utils
from frabit import exceptions
from frabit import output

from frabit.exceptions import (ConninfoException,
                               MysqlException,
                               MysqlDataError,
                               MysqlIntegrityError,
                               MysqlInternalError,
                               MysqlOperationalError,
                               MysqlNotSupportedError,
                               MysqlProgrammingError,
                               MysqlConnectError,
                               MysqlInterfaceError,
                               BackupInefficientPrivilege
                               )
from frabit.utils import force_str, simplify_version, with_metaclass
from frabit.binlog import DEFAULT_XLOG_SEG_SIZE

_logger = logging.getLogger(__name__)

_live_connections = []
"""
List of connections to be closed at the interpreter shutdown
"""


@atexit.register
def _atexit():
    """
    Ensure that all the connections are correctly closed
    at interpreter shutdown
    """
    # Take a copy of the list because the conn.close() method modify it
    for conn in list(_live_connections):
        _logger.warning(
            "Forcing {} cleanup during process shut down.".format(conn.__class__.__name__))
        conn.close()


class MySQL:
    """
    This  class represents a generic interface to a MySQL server.
    """

    CHECK_QUERY = 'SELECT 1;'

    def __init__(self, conninfo):
        """
        Abstract base class constructor for MySQL interface.

        :param str conninfo: Connection information (aka DSN)
        """
        super(MySQL, self).__init__()
        self.conninfo = conninfo
        self._conn = None
        self.allow_reconnect = True
        # Build a dictionary with connection info parameters,This is mainly used to speed up search in conninfo
        try:
            self.conn_parameters = self.parse_dsn(conninfo)
        except (ValueError, TypeError) as e:
            _logger.debug(e)
            raise ConninfoException('Cannot connect to mysql: "{}" is a invalid connection string'.format(conninfo))

    @staticmethod
    def parse_dsn(dsn):
        """
        Parse connection parameters from 'conninfo'

        :param str dsn: Connection information (aka DSN)
        :rtype: dict[str,str]
        """
        return dict(x.split('=', 1) for x in dsn.split())

    @staticmethod
    def encode_dsn(parameters):
        """
        Build a connection string from a dictionary of connection
        parameters

        :param dict[str,str] parameters: Connection parameters
        :rtype: str
        """
        return ' '.join(["%s=%s" % (k, v) for k, v in sorted(parameters.items())])

    def connect(self):
        """
        Generic function for MySQL connection (using mysql-connector-python)
        """

        if not self._check_connection():
            try:
                self._conn = mysql.connector.connect(self.conn_parameters)
            # If mysql-connector-python fails to connect to the host, raise the appropriate exception
            except connector.PoolError as e:
                raise MysqlConnectError(force_str(e).strip())
            # Register the connection to the list of live connections
            _live_connections.append(self)
        return self._conn

    def _check_connection(self):
        """
        Return false if the connection is broken

        :rtype: bool
        """
        # If the connection is not present return False
        if not self._conn:
            return False

        # Check if the connection works by running 'SELECT 1'
        cursor = None
        initial_status = None
        try:
            initial_status = self._conn.status
            cursor = self._conn.cursor()
            cursor.execute(self.CHECK_QUERY)
            # Rollback if initial status was IDLE because the CHECK QUERY has started a new transaction.
            if initial_status == STATUS_READY:
                self._conn.rollback()
        except connector.DatabaseError:
            # Connection is broken, so we need to reconnect
            self.close()
            # Raise an error if reconnect is not allowed
            if not self.allow_reconnect:
                raise MysqlConnectError("Connection lost, reconnection not allowed")
            return False
        finally:
            if cursor:
                cursor.close()
        return True

    def close(self):
        """
        Close the connection to MySQL
        """
        if self._conn:
            # If the connection is still alive, rollback and close it
            if not self._conn.closed:
                if self._conn.status == STATUS_IN_TRANSACTION:
                    self._conn.rollback()
                self._conn.close()
            # Remove the connection from the live connections list
            self._conn = None
            _live_connections.remove(self)

    def _cursor(self, *args, **kwargs):
        """
        Return a cursor
        """
        cnx = self.connect()
        return cnx.cursor(*args, **kwargs)

    @property
    def server_version(self):
        """
        Version of MySQL (returned by mysql-connector-python)
        """
        cnx = self.connect()
        version = cnx.get_server_info()
        return simplify_version(version)

class MySQLConnection(MySQL):
    """
    This class represents a standard client connection to a MySQL server.
    """
    SERVER_TYPE = 'source'

    def __init__(self, conninfo):
        """
        MySQl connection constructor.

        :param str conninfo: Connection information (aka DSN)
        """
        super(MySQLConnection, self).__init__(conninfo)
        self.configuration_files = None

    def connect(self):
        """
        Connect to the MySQL server. It reuses an existing connection.
        """
        if self._check_connection():
            return self._conn
        # create a new connection if not an existing connection
        self._conn = super(MySQLConnection, self).connect()
        return self._conn

    @property
    def server_txt_version(self):
        """
        Human readable version of MySQL (returned by the server)
        """
        try:
            cur = self._cursor()
            cur.execute("SELECT version()")
            info = cur.fetchall()[0][0].split("-")[0]
            version = {"major": info.split(".")[0], "minor": info.split(".")[1], "patch": info.split(".")[2]}
            return version
        except (MysqlInterfaceError, MysqlException) as e:
            _logger.debug("Error retrieving MySQL version: {}".format(force_str(e).strip()))
            return None

    @property
    def has_backup_privileges(self):
        """
        Returns true if current user has efficient privileges,include below:
        super :
        processlist:
        replicate client:
        replicate slave :
        """
        privileges_info = """
        SELECT
        concat(Select_priv
              ,Insert_priv
              ,Update_priv
              ,Delete_priv
              ,Create_priv
              ,Drop_priv
              ,Reload_priv
              ,Shutdown_priv
              ,Process_priv
              ,File_priv
              ,Grant_priv
              ,References_priv
              ,Index_priv
              ,Alter_priv
              ,Show_db_priv
              ,Super_priv
              ,Lock_tables_priv
              ,Execute_priv
              ,Repl_slave_priv
              ,Repl_client_priv
              ,Create_view_priv
              ,Show_view_priv
              ,Create_routine_priv
              ,Alter_routine_priv
              ,Create_user_priv
              ,Event_priv
              ,Trigger_priv
              )
        FROM mysql.user
        WHERE user = CURRENT_USER
          AND host = CURRENT_HOST 
        """
        try:
            cur = self._cursor()
            cur.execute(privileges_info)
            return cur.fetchone()[0]
        except (MysqlInterfaceError, MysqlException) as e:
            _logger.debug("Error checking privileges needed for backups: {}".format(force_str(e).strip()))
            return None

    @property
    def current_binlog_info(self):
        """
        Get detailed information about the current Binlog position in MySQL.

        This method returns a dictionary containing the following data:

         * file_name
         * position
         * gtid

        :rtype: dict
        """
        try:
            cur = self._cursor()
            cur.execute("SHOW MASTER STATUS;")
            return cur.fetchone()
        except (MysqlInterfaceError, MysqlException) as e:
            _logger.debug("Error retrieving current binlog detailed information: {}".format(force_str(e).strip()))
        return None

    @property
    def current_binlog_file_name(self):
        """
        Get current WAL file from MySQL

        :return str: current WAL file in MySQL
        """
        current_binlog_info = self.current_binlog_info
        if current_binlog_info is not None:
            return current_binlog_info['file']
        return None

    @property
    def current_binlog_position(self):
        """
        Get current WAL location from MySQL

        :return str: current WAL location in MySQL
        """
        current_binlog_info = self.current_binlog_info
        if current_binlog_info is not None:
            return current_binlog_info['position']
        return None

    @property
    def current_gtid(self):
        """
        Get current WAL location from MySQL

        :return str: current WAL location in MySQL
        """
        current_binlog_info = self.current_binlog_info
        if current_binlog_info is not None:
            return current_binlog_info['gtid']
        return None

    @property
    def current_size(self):
        """
        Returns the total size of the MySQL server
        """
        if not self.has_backup_privileges:
            return None

        try:
            cur = self._cursor()
            cur.execute(
                "SELECT sum(pg_tablespace_size(oid)) "
                "FROM pg_tablespace")
            return cur.fetchone()[0]
        except (MysqlInterfaceError, MysqlException) as e:
            _logger.debug("Error retrieving MySQL total size: %s",
                          force_str(e).strip())
            return None

    def get_variable(self, name, is_global=True):
        """
        Get MySQL configuration option

        :rtype: str
        """
        if self.configuration_files:
            return self.configuration_files
        try:
            self.configuration_files = {}
            cur = self._cursor()
            cur.execute(
                "SELECT name, setting FROM pg_settings "
                "WHERE name IN ('config_file', 'hba_file', 'ident_file')")
            for cname, cpath in cur.fetchall():
                self.configuration_files[cname] = cpath
        except (MysqlInterfaceError, MysqlException) as e:
            _logger.debug("Error retrieving MySQL configuration files "
                          "location: %s", force_str(e).strip())
            self.configuration_files = {}

        return self.configuration_files

    def get_status(self, name, is_global=True):
        """
        Get MySQL global or session status

        :rtype: str
        """
        cur = self._cursor()
        if is_global:
            pass
        try:
            cur.execute(
                "SELECT name, setting FROM pg_settings "
                "WHERE name IN ('config_file', 'hba_file', 'ident_file')")
            for cname, cpath in cur.fetchall():
                self.configuration_files[cname] = cpath
        except (MysqlInterfaceError, MysqlException) as e:
            _logger.debug("Error retrieving MySQL configuration files "
                          "location: %s", force_str(e).strip())
            self.configuration_files = {}

        return self.configuration_files

    def flush_binlog(self):
        """
        Execute a FLUSH BINARY LOGS ;

        To be SURE of the switch of a xlog, we collect the xlogfile name
        before and after the switch.
        The method returns the just closed xlog file name if the current xlog
        file has changed, it returns an empty string otherwise.

        The method returns None if something went wrong during the execution
        of the pg_switch_wal command.

        :rtype: str|None
        """
        try:
            conn = self.connect()
            # Requires superuser privilege
            if not self.has_backup_privileges:
                raise BackupInefficientPrivilege()
            cur = conn.cursor()
            cur.execute('FLUSH BINARY LOGS')
        except (MysqlInterfaceError, MysqlException) as e:
            _logger.debug("Error issuing (FLUSH BINARY LOGS) command")
            return None

    def get_replication_stats(self):
        """
        Returns replication information dict
        """
        try:
            cur = self._cursor()

            if not self.has_backup_privileges:
                raise BackupInefficientPrivilege()

            '''
            Slave_IO_State:
            Master_Host:
            Master_User:
            Master_Port:
            Connect_Retry:
            Master_Log_File:
            Read_Master_Log_Pos:
            Relay_Log_File:
            Relay_Log_Pos:
            Relay_Master_Log_File:
            Slave_IO_Running:
            Slave_SQL_Running:
            Replicate_Do_DB:
            Replicate_Ignore_DB:
            Replicate_Do_Table:
            Replicate_Ignore_Table:
            Replicate_Wild_Do_Table:
            Replicate_Wild_Ignore_Table:
            Last_Errno:
            Last_Error:
            Skip_Counter:
            Exec_Master_Log_Pos:
            Relay_Log_Space:
            Until_Condition:
            Until_Log_File:
            Until_Log_Pos:
            Master_SSL_Allowed:
            Master_SSL_CA_File:
            Master_SSL_CA_Path:
            Master_SSL_Cert:
            Master_SSL_Cipher:
            Master_SSL_Key:
            Seconds_Behind_Master:
            Master_SSL_Verify_Server_Cert:
            Last_IO_Errno:
            Last_IO_Error:
            Last_SQL_Errno:
            Last_SQL_Error:
            Replicate_Ignore_Server_Ids:
            Master_Server_Id:
            Master_UUID:
            Master_Info_File:
            SQL_Delay:
            SQL_Remaining_Delay:
            Slave_SQL_Running_State:
            Master_Retry_Count:
            Master_Bind:
            Last_IO_Error_Timestamp:
            Last_SQL_Error_Timestamp:
            Master_SSL_Crl:
            Master_SSL_Crlpath:
            Retrieved_Gtid_Set:
            Executed_Gtid_Set:
            Auto_Position:
            Replicate_Rewrite_DB:
            Channel_Name:
            Master_TLS_Version:
            '''
            # Execute the query
            cur.execute(
                ""
                .format())

            # Generate a list of standby objects
            return cur.fetchall()
        except (MysqlInterfaceError, MysqlException) as e:
            _logger.debug("Error retrieving status of replica servers: {}".format(force_str(e).strip()))
            return None