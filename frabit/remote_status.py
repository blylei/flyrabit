# (c) 2020 Frabit Project maintained and limited by Blylei < blylei918@gmail.com >
# GNU General Public License v3.0+ (see COPYING or https://www.gnu.org/licenses/gpl-3.0.txt)
#
# This file is part of Frabit
#
"""
Remote Status module

A Remote Status class implements a standard interface for
retrieving and caching the results of a remote component
(such as MySQL server, WAL archiver, etc.). It follows
the Mixin pattern.
"""

from abc import ABCMeta, abstractmethod

from frabit.utils import with_metaclass


class RemoteStatusMixin(with_metaclass(ABCMeta, object)):
    """
    Abstract base class that implements remote status capabilities
    following the Mixin pattern.
    """

    def __init__(self, *args, **kwargs):
        """
        Base constructor (Mixin pattern)
        """
        self._remote_status = None
        super(RemoteStatusMixin, self).__init__(*args, **kwargs)

    @abstractmethod
    def fetch_remote_status(self):
        """
        Retrieve status information from the remote component

        The implementation of this method must not raise any exception in case
        of errors, but should set the missing values to None in the resulting
        dictionary.

        :rtype: dict[str, None|str]
        """

    def get_remote_status(self):
        """
        Get the status of the remote component

        This method does not raise any exception in case of errors,
        but set the missing values to None in the resulting dictionary.

        :rtype: dict[str, None|str]
        """
        if self._remote_status is None:
            self._remote_status = self.fetch_remote_status()
        return self._remote_status

    def reset_remote_status(self):
        """
        Reset the cached result
        """
        self._remote_status = None
