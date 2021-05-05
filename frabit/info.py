# (c) 2020 Frabit Project maintained and limited by Blylei < blylei918@gmail.com >
# GNU General Public License v3.0+ (see COPYING or https://www.gnu.org/licenses/gpl-3.0.txt)
#
# This file is part of Frabit
#

import ast
import collections
import inspect
import logging
import os

import dateutil.parser
import dateutil.tz

import frabit.compression
from frabit import binlog
from frabit.exceptions import BackupInfoBadInitialisation
from frabit.utils import fsync_dir

# Named tuple representing a Tablespace with 'name' 'oid' and 'location'
# as property.
Tablespace = collections.namedtuple('Tablespace', 'name oid location')

# Named tuple representing a file 'path' with an associated 'file_type'
TypedFile = collections.namedtuple('ConfFile', 'file_type path')

_logger = logging.getLogger(__name__)


def null_repr(obj):
    """
    Return the literal representation of an object

    :param object obj: object to represent
    :return str|None: Literal representation of an object or None
    """
    return repr(obj) if obj else None


def load_datetime_tz(time_str):
    """
    Load datetime and ensure the result is timezone-aware.

    If the parsed timestamp is naive, transform it into a timezone-aware one
    using the local timezone.

    :param str time_str: string representing a timestamp
    :return datetime: the parsed timezone-aware datetime
    """
    # dateutil parser returns naive or tz-aware string depending on the format
    # of the input string
    timestamp = dateutil.parser.parse(time_str)
    # if the parsed timestamp is naive, forces it to local timezone
    if timestamp.tzinfo is None:
        timestamp = timestamp.replace(tzinfo=dateutil.tz.tzlocal())
    return timestamp


class Field(object):

    def __init__(self, name, dump=None, load=None, default=None, doc=None):
        """
        Field descriptor to be used with a FieldListFile subclass.

        The resulting field is like a normal attribute with two optional associated function:
        to_str and from_str

        The Field descriptor can also be used as a decorator

            class C(FieldListFile):
                x = Field('x')
                @x.dump
                def x(val): return '0x%x' % val
                @x.load
                def x(val): return int(val, 16)

        :param str name: the name of this attribute
        :param callable dump: function used to dump the content to a disk
        :param callable load: function used to reload the content from disk
        :param default: default value for the field
        :param str doc: docstring of the filed
        """
        self.name = name
        self.to_str = dump
        self.from_str = load
        self.default = default
        self.__doc__ = doc

    # noinspection PyUnusedLocal
    def __get__(self, obj, objtype=None):
        if obj is None:
            return self
        if not hasattr(obj, '_fields'):
            obj._fields = {}
        return obj._fields.setdefault(self.name, self.default)

    def __set__(self, obj, value):
        if not hasattr(obj, '_fields'):
            obj._fields = {}
        obj._fields[self.name] = value

    def __delete__(self, obj):
        raise AttributeError("can't delete attribute")

    def dump(self, to_str):
        return type(self)(self.name, to_str, self.from_str, self.__doc__)

    def load(self, from_str):
        return type(self)(self.name, self.to_str, from_str, self.__doc__)


class FieldListFile(object):
    def __init__(self, **kwargs):
        """
        Represent a predefined set of keys with the associated value.

        The constructor build the object assigning every keyword argument to
        the corresponding attribute. If a provided keyword argument doesn't
        has a corresponding attribute an AttributeError exception is raised.

        The values provided to the constructor must be of the appropriate
        type for the corresponding attribute.
        The constructor will not attempt any validation or conversion on them.

        This class is meant to be an abstract base class.

        :raises: AttributeError
        """
        self._fields = {}
        self.filename = None
        for name in kwargs:
            field = getattr(type(self), name, None)
            if isinstance(field, Field):
                setattr(self, name, kwargs[name])
            else:
                raise AttributeError('unknown attribute %s' % name)

    @classmethod
    def from_meta_file(cls, filename):
        """
        Factory method that read the specified file and build an object with its content.

        :param str filename: the file to read
        """
        o = cls()
        o.load(filename)
        return o

    def save(self, filename=None, file_object=None):
        """
        Serialize the object to the specified file or file object

        If a file_object is specified it will be used.

        If the filename is not specified it uses the one memorized in the filename attribute.
        If neither the filename attribute and parameter are
        set a ValueError exception is raised.

        :param str filename: path of the file to write
        :param file file_object: a file like object to write in
        :param str filename: the file to write
        :raises: ValueError
        """
        if file_object:
            info = file_object
        else:
            filename = filename or self.filename
            if filename:
                info = open(filename + '.tmp', 'wb')
            else:
                info = None

        if not info:
            raise ValueError('either a valid filename or a file_object must be specified')
        try:
            for name, field in sorted(inspect.getmembers(type(self))):
                value = getattr(self, name, None)
                if isinstance(field, Field):
                    if callable(field.to_str):
                        value = field.to_str(value)
                    info.write(("{name}={value}\n".format(name=name, value=value)).encode('UTF-8'))
        finally:
            if not file_object:
                info.close()

        if not file_object:
            os.rename(filename + '.tmp', filename)
            fsync_dir(os.path.normpath(os.path.dirname(filename)))

    def load(self, filename=None, file_object=None):
        """
        Replaces the current object content with the one deserialized from the provided file.

        This method set the filename attribute.

        A ValueError exception is raised if the provided file contains any
        invalid line.

        :param str filename: path of the file to read
        :param file file_object: a file like object to read from
        :param str filename: the file to read
        :raises: ValueError
        """

        if file_object:
            info = file_object
        elif filename:
            info = open(filename, 'rb')
        else:
            raise ValueError(
                'either filename or file_object must be specified')

        # detect the filename if a file_object is passed
        if not filename and file_object:
            if hasattr(file_object, 'name'):
                filename = file_object.name

        # canonicalize filename
        if filename:
            self.filename = os.path.abspath(filename)
        else:
            self.filename = None
            filename = '<UNKNOWN>'  # This is only for error reporting

        with info:
            for line in info:
                line = line.decode('UTF-8')
                # skip spaces and comments
                if line.isspace() or line.rstrip().startswith('#'):
                    continue

                # parse the line of form "key = value"
                try:
                    name, value = [x.strip() for x in line.split('=', 1)]
                except ValueError:
                    raise ValueError('invalid line %s in file %s' % (
                        line.strip(), filename))

                # use the from_str function to parse the value
                field = getattr(type(self), name, None)
                if value == 'None':
                    value = None
                elif isinstance(field, Field) and callable(field.from_str):
                    value = field.from_str(value)
                setattr(self, name, value)

    def items(self):
        """
        Return a generator returning a list of (key, value) pairs.

        If a filed has a dump function defined, it will be used.
        """
        for name, field in sorted(inspect.getmembers(type(self))):
            value = getattr(self, name, None)
            if isinstance(field, Field):
                if callable(field.to_str):
                    value = field.to_str(value)
                yield (name, value)

    def __repr__(self):
        return "%s(%s)" % (self.__class__.__name__, ', '.join(['%s=%r' % x for x in self.items()]))


class BinlogInfo(FieldListFile):
    """
    Metadata of a Binlog file.
    """

    name = Field('name', doc='base name of Binlog file')
    size = Field('size', load=int, doc='Binlog file size after compression')
    time = Field('time', load=float, doc='Binlog file modification time (seconds since epoch)')
    compression = Field('compression', doc='compression type')

    @classmethod
    def from_file(cls, filename, unidentified_compression=None, **kwargs):
        """
        Factory method to generate a BinlogInfo from a binlog file.

        Every keyword argument will override any attribute from the provided
        file. If a keyword argument doesn't has a corresponding attribute
        an AttributeError exception is raised.

        :param str filename: the file to inspect
        :param str unidentified_compression: the compression to set if
            the current schema is not identifiable.
        """
        identify_compression = frabit.compression.identify_compression
        stat = os.stat(filename)
        kwargs.setdefault('name', os.path.basename(filename))
        kwargs.setdefault('size', stat.st_size)
        kwargs.setdefault('time', stat.st_mtime)
        if 'compression' not in kwargs:
            kwargs['compression'] = identify_compression(filename) \
                or unidentified_compression
        obj = cls(**kwargs)
        obj.filename = "%s.meta" % filename
        obj.orig_filename = filename
        return obj

    def to_binlog_line(self):
        """
        Format the content of this object as a binlog line.
        """
        return "%s\t%s\t%s\t%s\n" % (
            self.name,
            self.size,
            self.time,
            self.compression)

    @classmethod
    def from_binlog_line(cls, line):
        """
        Parse a line from xlog catalogue

        :param str line: a line in the wal database to parse
        :rtype: BinlogInfo
        """
        try:
            name, size, time, compression = line.split()
        except ValueError:
            # Old format compatibility (no compression)
            compression = None
            try:
                name, size, time = line.split()
            except ValueError:
                raise ValueError("cannot parse line: %r" % (line,))
        # The to_xlogdb_line method writes None values as literal 'None'
        if compression == 'None':
            compression = None
        size = int(size)
        time = float(time)
        return cls(name=name, size=size, time=time,
                   compression=compression)

    def to_json(self):
        """
        Return an equivalent dictionary that can be encoded in json
        """
        return dict(self.items())

    def relpath(self):
        """
        Returns the binlog file path relative to the server's binlog files_directory
        """
        return os.path.join(binlog.hash_dir(self.name), self.name)

    def fullpath(self, server):
        """
        Returns the Binlog file full path

        :param frabit.server.Server server: the server that owns the binlog file
        """
        return os.path.join(server.config.binlog_directory, self.relpath())


class BackupInfo(FieldListFile):

    #: Conversion to string
    EMPTY = 'EMPTY'
    STARTED = 'STARTED'
    FAILED = 'FAILED'
    WAITING_FOR_WALS = 'WAITING_FOR_WALS'
    DONE = 'DONE'
    SYNCING = 'SYNCING'
    STATUS_COPY_DONE = (WAITING_FOR_WALS, DONE)
    STATUS_ALL = (EMPTY, STARTED, WAITING_FOR_WALS, DONE, SYNCING, FAILED)
    STATUS_NOT_EMPTY = (STARTED, WAITING_FOR_WALS, DONE, SYNCING, FAILED)
    STATUS_ARCHIVING = (STARTED, WAITING_FOR_WALS, DONE, SYNCING)

    #: Status according to retention policies
    OBSOLETE = 'OBSOLETE'
    VALID = 'VALID'
    POTENTIALLY_OBSOLETE = 'OBSOLETE*'
    NONE = '-'
    RETENTION_STATUS = (OBSOLETE, VALID, POTENTIALLY_OBSOLETE, NONE)

    version = Field('version', load=int)
    # Timeline is an integer
    timeline = Field('timeline', load=int)
    begin_time = Field('begin_time', load=load_datetime_tz)
    begin_offset = Field('begin_offset', load=int)
    size = Field('size', load=int)
    end_time = Field('end_time', load=load_datetime_tz)
    begin_binlog = Field('begin_binlog')
    end_binlog = Field('end_binlog')
    end_offset = Field('end_offset', load=int)
    status = Field('status', default=EMPTY)
    server_name = Field('server_name')
    error = Field('error')
    mode = Field('mode')
    config_file = Field('config_file')
    included_files = Field('included_files', load=ast.literal_eval, dump=null_repr)
    backup_label = Field('backup_label', load=ast.literal_eval, dump=null_repr)
    copy_stats = Field('copy_stats', load=ast.literal_eval, dump=null_repr)
    binlog_file_size = Field('binlog_file_size', load=int, default=binlog.DEFAULT_XLOG_SEG_SIZE)


    def __init__(self, backup_id, **kwargs):
        """
        Stores meta information about a single backup

        :param str,None backup_id:
        """
        self.backup_version = 2
        self.backup_id = backup_id
        super(BackupInfo, self).__init__(**kwargs)

    def get_required_binlog_files(self):
        """
        Get the list of required binlog files for the current backup
        """
        return binlog.generate_binlog_names(
            self.begin_binlog, self.end_binlog,
            self.version,
            self.binlog_file_size)

    def set_attribute(self, key, value):
        """
        Set a value for a given key
        """
        setattr(self, key, value)

    def to_dict(self):
        """
        Return the backup_info content as a simple dictionary

        :return dict:
        """
        result = dict(self.items())
        result.update(backup_id=self.backup_id, server_name=self.server_name,
                      mode=self.mode, tablespaces=self.tablespaces,
                      included_files=self.included_files,
                      copy_stats=self.copy_stats)
        return result

    def to_json(self):
        """
        Return an equivalent dictionary that uses only json-supported types
        """
        data = self.to_dict()
        # Convert fields which need special types not supported by json
        if data.get('tablespaces') is not None:
            data['tablespaces'] = [list(item)
                                   for item in data['tablespaces']]
        if data.get('begin_time') is not None:
            data['begin_time'] = data['begin_time'].ctime()
        if data.get('end_time') is not None:
            data['end_time'] = data['end_time'].ctime()
        return data

    @classmethod
    def from_json(cls, server, json_backup_info):
        """
        Factory method that builds a BackupInfo object
        from a json dictionary

        :param barman.Server server: the server related to the Backup
        :param dict json_backup_info: the data set containing values from json
        """
        data = dict(json_backup_info)
        # Convert fields which need special types not supported by json
        if data.get('tablespaces') is not None:
            data['tablespaces'] = [Tablespace._make(item)
                                   for item in data['tablespaces']]
        if data.get('begin_time') is not None:
            data['begin_time'] = load_datetime_tz(data['begin_time'])
        if data.get('end_time') is not None:
            data['end_time'] = load_datetime_tz(data['end_time'])
        # Instantiate a BackupInfo object using the converted fields
        return cls(server, **data)


class LocalBackupInfo(BackupInfo):

    def __init__(self, server, info_file=None, backup_id=None, **kwargs):
        """
        Stores meta information about a single backup

        :param Server server:
        :param file,str,None info_file:
        :param str,None backup_id:
        :raise BackupInfoBadInitialisation: if the info_file content is invalid
            or neither backup_info or
        """
        # Initialises the attributes for the object
        # based on the predefined keys
        super(LocalBackupInfo, self).__init__(backup_id=backup_id, **kwargs)

        self.server = server
        self.config = server.config
        self.backup_manager = self.server.backup_manager
        self.server_name = self.config.name
        self.mode = self.backup_manager.mode
        if backup_id:
            # Cannot pass both info_file and backup_id
            if info_file:
                raise BackupInfoBadInitialisation('both info_file and backup_id parameters are set')
            self.backup_id = backup_id
            self.filename = self.get_filename()
            # Check if a backup info file for a given server and a given ID
            # already exists. If so load the values from the file.
            if os.path.exists(self.filename):
                self.load(filename=self.filename)
        elif info_file:
            if hasattr(info_file, 'read'):
                # We have been given a file-like object
                self.load(file_object=info_file)
            else:
                # Just a file name
                self.load(filename=info_file)
            self.backup_id = self.detect_backup_id()
        elif not info_file:
            raise BackupInfoBadInitialisation(
                'backup_id and info_file parameters are both unset')

    def get_list_of_files(self, target):
        """
        Get the list of files for the current backup
        """
        # Walk down the base backup directory
        if target in ('data', 'standalone', 'full'):
            for root, _, files in os.walk(self.get_basebackup_directory()):
                for f in files:
                    yield os.path.join(root, f)
        if target in 'standalone':
            # List all the WAL files for this backup
            for x in self.get_required_wal_segments():
                yield self.server.get_wal_full_path(x)
        if target in ('wal', 'full'):
            for wal_info in self.server.get_wal_until_next_backup(
                    self,
                    include_history=True):
                yield wal_info.fullpath(self.server)

    def detect_backup_id(self):
        """
        Detect the backup ID from the name of the parent dir of the info file
        """
        if self.filename:
            return os.path.basename(os.path.dirname(self.filename))
        else:
            return None

    def get_basebackup_directory(self):
        """
        Get the default filename for the backup.info file based on
        backup ID and server directory for base backups
        """
        return os.path.join(self.config.basebackups_directory,
                            self.backup_id)

    def get_filename(self):
        """
        Get the default filename for the backup.info file based on
        backup ID and server directory for base backups
        """
        return os.path.join(self.get_basebackup_directory(), 'backup.info')

    def save(self, filename=None, file_object=None):
        if not file_object:
            # Make sure the containing directory exists
            filename = filename or self.filename
            dir_name = os.path.dirname(filename)
            if not os.path.exists(dir_name):
                os.makedirs(dir_name)
        super(LocalBackupInfo, self).save(filename=filename, file_object=file_object)
