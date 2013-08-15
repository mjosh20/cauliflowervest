#!/usr/bin/env python
# 
# Copyright 2011 Google Inc. All Rights Reserved.
# 
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
# 
#     http://www.apache.org/licenses/LICENSE-2.0
# 
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS-IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.
# #

"""App Engine Models for CauliflowerVest web application."""



from google.appengine.api import memcache
from google.appengine.api import users
from google.appengine.ext import db

from cauliflowervest import settings as base_settings
from cauliflowervest.server import crypto
from cauliflowervest.server import permissions
from cauliflowervest.server import settings


class Error(Exception):
  """Class for domain specific exceptions."""


class AccessError(Error):
  """There was an error accessing a passphrase or key."""
  request = None
  error_code = 400

  def __init__(self, message, request=None):
    super(AccessError, self).__init__(message)
    self.request = request


class BitLockerAccessError(AccessError):
  """There was an error accessing a BitLocker key."""


class DuplicityAccessError(AccessError):
  """There was an error accessing a Duplicity key pair."""


class FileVaultAccessError(AccessError):
  """There was an error accessing a FileVault passphrase."""


class LuksAccessError(AccessError):
  """There was an error accessing a Luks passphrase."""


class AccessDeniedError(AccessError):
  """Accessing a passphrase was denied."""
  request = None
  error_code = 403

  def __init__(self, message, request=None):
    super(AccessDeniedError, self).__init__(message)
    self.request = request


class BitLockerAccessDeniedError(AccessDeniedError):
  """There was an error accessing a BitLocker key."""


class DuplicityAccessDeniedError(AccessDeniedError):
  """There was an error accessing a Duplicity key pair."""


class FileVaultAccessDeniedError(AccessDeniedError):
  """There was an error accessing a FileVault passphrase."""


class LuksAccessDeniedError(AccessDeniedError):
  """There was an error accessing a Luks passphrase."""


# Here so that AutoUpdatingUserProperty will work without dependency cycles.
def GetCurrentUser():
  """Returns a models.User object for the currently logged in user.

  If the current logged in user is an App Engine admin, a User entity will
  be created for them with permissions.SET_REGULAR permissions and saved to the
  datastore.

  Returns:
    models.User object.
  Raises:
    AccessDeniedError: raised when no user is logged in.
  """
  user = users.get_current_user()
  if not user:
    raise AccessDeniedError('Auth Failed')

  user_entity = User.get_by_key_name(user.email())
  if not user_entity:
    user_entity = User(key_name=user.email(), user=user)
    if users.is_current_user_admin():
      # Automatically create User entities with full permissions for
      # users with admin privileges.
      for permission_type in permissions.TYPES:
        user_entity.SetPerms(permissions.SET_REGULAR, permission_type)
      user_entity.user = users.User(user.email())
      user_entity.put()

  return user_entity


class EncryptedBlobProperty(db.BlobProperty):
  """BlobProperty class that encrypts/decrypts data seamlessly on get/set."""

  # pylint: disable=g-bad-name
  def make_value_from_datastore(self, value):
    """Decrypts the blob value coming from Datastore."""
    return super(EncryptedBlobProperty, self).make_value_from_datastore(
        crypto.Decrypt(value))

  # pylint: disable=g-bad-name
  def get_value_for_datastore(self, model_instance):
    """Encrypts the blob value on it's way to Datastore."""
    raw_blob = super(
        EncryptedBlobProperty, self).get_value_for_datastore(model_instance)
    return db.Blob(crypto.Encrypt(raw_blob))


class AutoUpdatingUserProperty(db.UserProperty):
  """UserProperty that sets the current users.User if not already set."""

  # pylint: disable=g-bad-name
  def __get__(self, model_instance, model_class):
    """Returns the property value, or if None, the current user logged in."""
    value = super(AutoUpdatingUserProperty, self).__get__(
        model_instance, model_class)

    # If the value is unset, populate it with the current user.
    if not value:
      try:
        user = GetCurrentUser()
        value = user.user  # Store the underlying users.User object.
      except AccessDeniedError:
        pass

    return value


class BaseVolume(db.Model):
  """Base model for various types of volumes."""

  ESCROW_TYPE_NAME = 'base_volume'
  MUTABLE_PROPERTIES = ()

  active = db.BooleanProperty(default=True)  # is this key active or not?
  created = db.DateTimeProperty(auto_now_add=True)
  created_by = AutoUpdatingUserProperty()  # user that created the object.
  hostname = db.StringProperty()  # name of the machine with the volume.
  owner = db.StringProperty()
  volume_uuid = db.StringProperty()  # Volume UUID of the encrypted volume.

  def put(self, *args, **kwargs):  # pylint: disable=g-bad-name
    """Disallow updating an existing entity, and enforce key_name.

    Returns:
      The key of the instance (either the existing key or a new key).
    Raises:
      AccessError: an existing entity was attempted to be put, or a
          required property was empty or not set.
    """
    model_name = self.__class__.__name__
    if not self.has_key():
      raise self.ACCESS_ERR_CLS('Cannot put a %s without a key.' % model_name)
    existing_entity = self.__class__.get_by_key_name(self.key().name())
    if existing_entity:
      for prop in self.properties():
        if (getattr(self, prop) != getattr(existing_entity, prop) and
            prop not in self.MUTABLE_PROPERTIES):
          raise self.ACCESS_ERR_CLS(
              'Cannot modify property "%s" of an existing %s.' %
              (prop, model_name))
    for prop_name in self.REQUIRED_PROPERTIES:
      if not getattr(self, prop_name, None):
        raise self.ACCESS_ERR_CLS('Required property empty: %s' % prop_name)
    return super(BaseVolume, self).put(*args, **kwargs)


class FileVaultVolume(BaseVolume):
  """Model for storing FileVault Volume passphrases, with various metadata."""

  ACCESS_ERR_CLS = FileVaultAccessError
  ESCROW_TYPE_NAME = 'filevault'
  MUTABLE_PROPERTIES = ('owner',)
  REQUIRED_PROPERTIES = base_settings.FILEVAULT_REQUIRED_PROPERTIES + [
      'passphrase', 'volume_uuid']
  SEARCH_FIELDS = [
      ('created_by', 'Escrow Username'),
      ('hdd_serial', 'Hard Drive Serial Number'),
      ('hostname', 'Hostname'),
      ('serial', 'Mac Serial Number'),
      ('owner', 'Owner Username'),
      ('platform_uuid', 'Platform UUID'),
      ('volume_uuid', 'Volume UUID'),
      ]

  # NOTE(user): For self-service encryption, owner/created_by may the same.
  #   Furthermore, created_by may go away if we implement unattended encryption
  #   via machine/certificate-based auth.
  passphrase = EncryptedBlobProperty()  # passphrase to unlock encrypted volume.
  platform_uuid = db.StringProperty()  # sp_platform_uuid in facter.
  serial = db.StringProperty()  # serial number of the Mac.
  hdd_serial = db.StringProperty()  # hard drive disk serial number.


class BitLockerVolume(BaseVolume):
  """Model for storing BitLocker Volume keys."""

  ACCESS_ERR_CLS = BitLockerAccessError
  ESCROW_TYPE_NAME = 'bitlocker'
  REQUIRED_PROPERTIES = ['dn', 'hostname', 'parent_guid', 'recovery_key']
  SEARCH_FIELDS = [
      ('hostname', 'Hostname'),
      ('volume_uuid', 'Volume UUID'),
      ]

  recovery_key = EncryptedBlobProperty()
  dn = db.StringProperty()
  parent_guid = db.StringProperty()
  when_created = db.DateTimeProperty()


class DuplicityKeyPair(BaseVolume):
  """Model for storing Duplicity key pairs."""

  ACCESS_ERR_CLS = DuplicityAccessError
  ESCROW_TYPE_NAME = 'duplicity'
  REQUIRED_PROPERTIES = base_settings.DUPLICITY_REQUIRED_PROPERTIES + [
      'key_pair',
      'owner',
      'volume_uuid',
      ]

  platform_uuid = db.StringProperty()
  key_pair = EncryptedBlobProperty()


class LuksVolume(BaseVolume):
  """Model for storing Luks passphrases."""

  ACCESS_ERR_CLS = LuksAccessError
  ESCROW_TYPE_NAME = 'luks'
  REQUIRED_PROPERTIES = base_settings.LUKS_REQUIRED_PROPERTIES + [
      'passphrase',
      'hostname',
      'platform_uuid',
      'owner',
      ]
  SEARCH_FIELDS = [
      ('hostname', 'Hostname'),
      ('volume_uuid', 'Volume UUID'),
      ('created_by', 'Escrow Username'),
      ('platform_uuid', 'MrMagoo Host UUID'),
      ('hdd_serial', 'Hard Drive Serial Number'),
      ('owner', 'Device Owner')
      ]

  passphrase = EncryptedBlobProperty()
  hdd_serial = db.StringProperty()
  platform_uuid = db.StringProperty()


class User(db.Model):
  """User of the CauliflowerVest application."""

  _PERMISSION_PROPERTIES = {
      permissions.TYPE_BITLOCKER: 'bitlocker_perms',
      permissions.TYPE_DUPLICITY: 'duplicity_perms',
      permissions.TYPE_FILEVAULT: 'filevault_perms',
      permissions.TYPE_LUKS: 'luks_perms',
      }

  # key_name = user's email address.
  user = db.UserProperty()
  # Select BitLocker operational permissions from ALL_PERMISSIONS.
  bitlocker_perms = db.StringListProperty()
  # Select Duplicity operational permissions from ALL_PERMISSIONS.
  duplicity_perms = db.StringListProperty()
  # Select FileVault operational permissions from ALL_PERMISSIONS.
  filevault_perms = db.StringListProperty()
  # Select Luks operational permissions from ALL_PERMISSIONS.
  luks_perms = db.StringListProperty()

  @property
  def email(self):
    return self.user.email()

  def HasPerm(self, perm, permission_type):
    """Verifies the User has permissions.

    Args:
      perm: str, permission to verify, one of PERM_* class variables.
      permission_type: str, one of permissions.TYPE_* variables.
    Returns:
      Boolean. True if the user has the requested permission, False otherwise.
    Raises:
      ValueError: the requested permission_type was invalid or unknown.
    """
    perm_prop = self._PERMISSION_PROPERTIES.get(permission_type)
    if not perm_prop:
      raise ValueError('unknown permission_type: %s' % permission_type)

    base_perms = settings.DEFAULT_PERMISSIONS.get(permission_type, ())
    return perm in base_perms or perm in getattr(self, perm_prop, [])

  def SetPerms(self, perms, permission_type):
    """Sets the permissions to the User object.

    Args:
      perms: list of str, permissions from permissions.*.
      permission_type: str, one of permissions.TYPE_* variables.
    Raises:
      ValueError: the requested permission_type was invalid or unknown.
    """
    perm_prop = self._PERMISSION_PROPERTIES.get(permission_type)
    if not perm_prop:
      raise ValueError('unknown permission_type: %s' % permission_type)
    setattr(self, perm_prop, list(perms))


class AccessLog(db.Model):
  """Model for logging access to passphrases."""
  ip_address = db.StringProperty()
  message = db.StringProperty()
  mtime = db.DateTimeProperty(auto_now_add=True)
  query = db.StringProperty()
  successful = db.BooleanProperty(default=True)
  user = AutoUpdatingUserProperty()

  # Guaranteed unique, for pagination.
  paginate_mtime = db.StringProperty()

  def put(self):  # pylint: disable=g-bad-name
    """Override put to automatically calculate pagination properties."""
    counter = memcache.incr('AccessLogCounter', initial_value=0)
    self.paginate_mtime = '%s_%s' % (self.mtime, counter)
    super(AccessLog, self).put()

  @classmethod
  def Log(cls, request=None, **kwargs):
    """Puts a new AccessLog entity into Datastore.

    Args:
       request: a webapp Request object to fetch obtain details from.
       **kwargs: any key/value pair with a key corresponding to an existing
          AccessLog property name.
    """
    log = cls()
    for p in log.properties():
      if p in kwargs:
        setattr(log, p, kwargs[p])
    if request:
      log.query = '%s?%s' % (request.path, request.query_string)
      log.ip_address = request.remote_addr
    log.put()


class BitLockerAccessLog(AccessLog):
  """Model for logging access to BitLocker keys."""


class DuplicityAccessLog(AccessLog):
  """Model for logging access to Duplicity key pairs."""


class FileVaultAccessLog(AccessLog):
  """Model for logging access to FileVault passphrases."""


class LuksAccessLog(AccessLog):
  """Model for logging access to Luks passphrases."""